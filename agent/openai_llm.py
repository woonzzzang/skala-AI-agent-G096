"""LLM_BACKEND=openai — 실제 OpenAI API 백엔드 (fake_llm/ollama_llm과 동일 인터페이스).

Wave 10a 구도를 실제 상용 API로: fast(기본 gpt-4o-mini)는 분류·계획·단순 대화,
quality(기본 gpt-4o)는 최종 답변 작성 전담. ollama_llm.py와 마찬가지로 실모델이라
결정론은 아니다 — gate(W6)·replan(W7)·validator(W8)가 연출이 아니라 실제 방어선으로
동작한다. Ollama와 달리 실제 과금이 발생하는 원격 API이므로, 이 백엔드로 실행하면
OpenAI 계정에 실제 비용이 청구된다(run_openai.sh 실행 전 반드시 인지할 것).

route/plan은 기존 JSON 모드, 대상 판별·Runbook 랭킹·Judge는 Chat Completions의
`response_format=json_schema` 구조화 출력을 사용한다. 출력이 구조화되어도 실제 등록
시스템/문서 ID와 수치는 별도 코드에서 검증한다.

비용(cost_usd)은 OpenAI 응답의 `usage.total_tokens`(실측) × 가상 단가다 — 실제 청구
금액과는 별개로, W10a의 모델 분리 효과를 fake/ollama와 같은 잣대로 비교하기 위함이다.
(OpenAI는 입력/출력 토큰 단가가 실제로는 다르지만, 세 백엔드의 인터페이스를
`usd_per_1k_tokens` 하나로 통일해두기 위해 여기서도 단일 블렌디드 단가로 단순화한다.)

사전 조건: 유효한 `OPENAI_API_KEY` 환경변수(.env).
"""
import json
import os
import re

import httpx
from langsmith import traceable

from .llm_contracts import JUDGE_SCHEMA, RUNBOOK_RANK_SCHEMA, TARGET_SCHEMA
from .task_planner import normalize_plan

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


ROUTE_INSTRUCTIONS = (
    'JSON 객체로만 응답하라. 스키마: '
    '{"route": "chat" 또는 "incident", "needs_analysis": true 또는 false}'
)

PLAN_INSTRUCTIONS = (
    'JSON 객체로만 응답하라. 스키마: {"needs_analysis": boolean, '
    '"tasks": [{"agent": "research"|"analysis", "target": string(선택)}]}'
)


class OpenAILLM:
    def __init__(self, model: str, usd_per_1k_tokens: float):
        self.name = f"openai:{model}"
        self.model = model
        self.usd_per_1k = usd_per_1k_tokens
        self.api_key = os.environ.get("OPENAI_API_KEY", "")

    def _chat(self, system: str, user: str, json_mode: bool = False,
              schema: dict | None = None, schema_name: str = "result") -> tuple[str, dict]:
        if not self.api_key:
            raise RuntimeError(
                "OPENAI_API_KEY가 설정되지 않았습니다 — .env 또는 환경변수로 지정하세요."
            )
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0,
        }
        if schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema_name, "strict": True, "schema": schema}}
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        resp = httpx.post(OPENAI_URL, json=payload, headers=headers, timeout=120.0)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        tokens = data.get("usage", {}).get("total_tokens", 0)
        usage = {"model": self.name, "tokens": tokens,
                 "usd": round(tokens / 1000 * self.usd_per_1k, 6)}
        return content, usage


class OpenAIFastLLM(OpenAILLM):
    """분류·계획·단순 대화 전담 (저비용 모델, 기본 gpt-4o-mini)."""

    @traceable(run_type="llm", name="openai_fast.route")
    def route(self, query: str) -> tuple[dict, dict]:
        system = ("당신은 ICT 운영 지원 라우터다. 사용자 요청을 분류하라. "
                  "장애·성능 저하·지연·진단·모니터링·티켓 등 운영 이슈면 route=incident, "
                  "인사·잡담·일반 질문이면 route=chat. "
                  "needs_analysis는 패턴 비교/통계 분석이 꼭 필요할 때만 true. "
                  + ROUTE_INSTRUCTIONS)
        content, usage = self._chat(system, query, json_mode=True)
        try:
            raw = json.loads(content)
            route = raw.get("route") if raw.get("route") in ("chat", "incident") else None
            result = {"route": route or "chat",
                      "needs_analysis": bool(raw.get("needs_analysis", False))}
        except (json.JSONDecodeError, AttributeError):
            # JSON 모드는 "유효한 JSON"만 보장할 뿐 스키마까지 강제하진 않으므로,
            # 필드 누락·오탐 시 코드 레벨 fallback을 둔다(W6 정신, ollama_llm.py와 동일).
            incident = bool(re.search(
                r"장애|진단|다운|지연|급증|incident|티켓|로그인.*느|서비스.*느|시스템.*느|결제.*느",
                query, re.IGNORECASE))
            result = {"route": "incident" if incident else "chat",
                      "needs_analysis": False}
        return result, usage

    @traceable(run_type="llm", name="openai_fast.resolve_target")
    def resolve_target(self, query: str, systems: list[dict]) -> tuple[dict, dict]:
        system = ("당신은 장애 조사 대상 식별기다. 다음 등록 시스템 목록 중에서만 선택하라. "
                  "명확한 근거가 없으면 missing, 둘 이상 가능하지만 확정할 수 없으면 ambiguous, "
                  "명시한 이름이 목록에 없으면 unknown이다. 추측해서 시스템을 확정하지 마라. "
                  "JSON 스키마에 맞춰 답하라.")
        user = f"등록 시스템(설명 포함): {json.dumps(systems, ensure_ascii=False)}\n사용자 입력: {query}"
        content, usage = self._chat(system, user, schema=TARGET_SCHEMA,
                                    schema_name="target_resolution")
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            result = {"status": "missing", "target_systems": [],
                      "reason": "대상 판별 결과를 읽을 수 없습니다.",
                      "clarification_question": "어느 시스템을 조사할까요?"}
        return result, usage

    @traceable(run_type="llm", name="openai_fast.rank_runbooks")
    def rank_runbooks(self, query: str, documents: list[dict]) -> tuple[dict, dict]:
        system = ("당신은 장애 대응 Runbook 검색기다. 제공된 문서의 의미와 모니터링 알림을 "
                  "비교해 관련 문서만 최대 3개 선택하라. 복합 장애면 여러 개를 선택하라. "
                  "각 관련도는 0~1이며 0.55 미만인 문서는 제외한다. 관련 문서가 없으면 "
                  "matches를 빈 배열로 반환한다. 문서 ID는 제공된 것만 사용하라.")
        user = f"검색 질의:\n{query}\n\n문서 목록:\n{json.dumps(documents, ensure_ascii=False)}"
        content, usage = self._chat(system, user, schema=RUNBOOK_RANK_SCHEMA,
                                    schema_name="runbook_ranking")
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            result = {"matches": []}
        return result, usage

    @traceable(run_type="llm", name="openai_fast.plan")
    def plan(self, query: str, procedure_prompt: str,
             targets: list[str]) -> tuple[dict, dict]:
        system = ("당신은 ICT 운영 Supervisor의 planning 노드다. 표준 진단 절차에 따라 "
                  "실행 계획(tasks)을 JSON으로 세워라. "
                  f"조사 대상은 검증된 {targets}뿐이다. 다른 시스템을 추가하지 마라. "
                  "패턴 분석이 꼭 필요하다고 판단되면 agent=analysis task를 추가해도 된다. "
                  + PLAN_INSTRUCTIONS)
        user = f"## 표준 절차\n{procedure_prompt}\n\n## 사용자 요청\n{query}"
        content, usage = self._chat(system, user, json_mode=True)

        # 코드 레벨 정규화 — JSON 형식은 지켜져도 내용은 흔들릴 수 있으므로
        # (대상 시스템 오타, task 누락/중복) 실행 가능한 형태로 보정한다.
        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            raw = {"needs_analysis": False, "tasks": []}
        return normalize_plan(raw if isinstance(raw, dict) else {}, targets), usage

    @traceable(run_type="llm", name="openai_fast.analyze")
    def analyze(self, collected: list[dict]) -> tuple[str, dict]:
        system = ("당신은 ICT 운영 패턴 분석가다. 여러 시스템의 모니터링 결과를 비교해 "
                  "공통 원인이 의심되는 패턴이 있는지 한두 문장으로 짧게 답하라. "
                  "패턴이 없으면 '특이 패턴 없음'이라고만 답하라.")
        mon = [c["data"] for c in collected if c["source"] == "monitoring"]
        facts = "\n".join(
            f"- {d['system_name']}: 상태={d['status']}, CPU {d['cpu_percent']}%, "
            f"알림 {d['alerts']}" for d in mon) or "(모니터링 데이터 없음)"
        return self._chat(system, facts)

    @traceable(run_type="llm", name="openai_fast.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        """ollama_llm.py와 인터페이스(on_token 콜백)만 맞춘다 — OpenAI Chat Completions를
        아직 stream=True로 호출하지 않아 토큰 단위 중계는 하지 않고 완성된 응답을 한 번에
        전달한다."""
        system = ("당신은 간결한 한국어 비서다. 한두 문장으로 답하라. "
                  "대화 이력에서 사용자가 밝힌 이름이 있으면 정확히 기억해 답하라.")
        rendered = "\n".join(f"{m['role']}: {m['content']}" for m in history[-10:])
        user = (f"## 지금까지의 대화\n{rendered or '(없음)'}\n\n"
                f"## 사용자의 새 메시지\n{query}")
        content, usage = self._chat(system, user)
        usage["prompt"] = f"[SYSTEM]\n{system}\n\n[USER]\n{user}"
        if on_token is not None:
            on_token(content)
        return content, usage


class OpenAIQualityLLM(OpenAILLM):
    """최종 답변 작성 전담 (고품질 모델, 기본 gpt-4o)."""

    @traceable(run_type="llm", name="openai_quality.write")
    async def write(self, query: str, collected: list[dict], attempt: int,
              feedback: str | None, on_token=None) -> tuple[str, dict]:
        """ollama_llm.py와 인터페이스(on_token 콜백)만 맞춘다 — OpenAI Chat Completions를
        아직 stream=True로 호출하지 않아 토큰 단위 중계는 하지 않고 완성된 응답을 한 번에
        전달한다."""
        system = ("당신은 ICT 운영 보고 작성자다. '확인된 사실', '원인 추정', "
                  "'권장 조치', '진단 한계' 네 항목으로 한국어 보고서를 작성하라. "
                  "조회된 시스템만 확정 대상으로 쓰고 사용자 질문에 언급된 미등록 "
                  "시스템은 사실로 단정하지 마라. 수치는 근거 값을 그대로 사용하고, "
                  "사용한 모든 Runbook ID와 실패한 조회를 명시하라. 근거 없으면 모른다고 하라.")
        facts = []
        for c in collected:
            if c["source"] == "monitoring":
                d = c["data"]
                facts.append(f"- 모니터링 {d['system_name']}: 상태={d['status']}, "
                             f"CPU {d['cpu_percent']}%, 메모리 {d['memory_percent']}%, "
                             f"알림 {len(d['alerts'])}건 {d['alerts']}")
            elif c["source"] == "runbook":
                facts.append(f"- Runbook {c['id']}:\n{c['data'].strip()}")
            elif c["source"] == "analysis":
                facts.append(f"- 패턴 분석: {c['data'].strip()}")
            elif c["source"] == "failure":
                facts.append(f"- 조회 실패: {c['data']}")
        user = f"## 사용자 요청\n{query}\n\n## 수집된 사실\n" + "\n".join(facts)
        if feedback:
            user += f"\n\n## 이전 초안에 대한 검증 반려 사유 (반드시 반영)\n{feedback}"
        content, usage = self._chat(system, user)
        usage["prompt"] = f"[SYSTEM]\n{system}\n\n[USER]\n{user}"
        if on_token is not None:
            on_token(content)
        return content, usage

    @traceable(run_type="llm", name="openai_quality.judge")
    def judge(self, draft: str, evidence: dict) -> tuple[dict, dict]:
        system = ("당신은 독립적인 장애 보고서 검증자다. 보고서의 모든 확정 사실, "
                  "수치, 원인과 조치의 근거를 제공된 모니터링과 Runbook에 대조하라. "
                  "추정은 추정으로 표시하고, 미확인 대상·조회 실패·출처 누락은 반려하라. "
                  "모호하거나 근거가 부족하면 passed=false로 판정하라.")
        user = (f"근거: {json.dumps(evidence, ensure_ascii=False)}\n\n"
                f"검증할 보고서:\n{draft}")
        content, usage = self._chat(system, user, schema=JUDGE_SCHEMA,
                                    schema_name="report_judgement")
        return json.loads(content), usage
