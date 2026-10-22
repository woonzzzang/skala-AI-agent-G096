"""LLM_BACKEND=ollama — 로컬 Ollama 실모델 백엔드 (fake_llm과 동일 인터페이스).

Wave 10a 구도를 실물로: fast(기본 qwen3:0.6b)는 분류·계획·단순 대화, quality(기본
qwen3:8b)는 최종 답변 작성 전담. Ollama의 구조화 출력(format=<JSON Schema>)과
temperature=0으로 변동을 줄이지만 실모델이므로 결정론은 아니다 — 그래서 gate(W6)·
replan(W7)·validator(W8)가 연출이 아니라 실제 방어선으로 동작한다.
비용(cost_usd)은 '가상 단가 × 실측 토큰 수(prompt_eval_count+eval_count)'다 — 로컬
추론이라 실제 과금은 0이지만, W10a의 모델 분리 효과를 같은 잣대로 비교하기 위함이다.

사전 조건: `ollama serve` 실행 중 + `ollama pull qwen3:0.6b qwen3:8b`.
"""
import json
import re

import httpx
from langsmith import traceable

from .llm_contracts import JUDGE_SCHEMA, RUNBOOK_RANK_SCHEMA, TARGET_SCHEMA
from .task_planner import normalize_plan

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"


ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": ["chat", "incident"]},
        "needs_analysis": {"type": "boolean"},
    },
    "required": ["route", "needs_analysis"],
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "needs_analysis": {"type": "boolean"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string",
                              "enum": ["research", "analysis"]},
                    "target": {"type": "string"},
                },
                "required": ["agent"],
            },
        },
    },
    "required": ["needs_analysis", "tasks"],
}


class OllamaLLM:
    def __init__(self, model: str, usd_per_1k_tokens: float):
        self.name = f"ollama:{model}"
        self.model = model
        self.usd_per_1k = usd_per_1k_tokens

    def _chat(self, system: str, user: str, schema: dict | None = None) -> tuple[str, dict]:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "options": {"temperature": 0.0},
        }
        if schema is not None:
            payload["format"] = schema
        resp = httpx.post(OLLAMA_URL, json=payload, timeout=300.0)
        resp.raise_for_status()
        data = resp.json()
        content = data["message"]["content"]
        tokens = (data.get("prompt_eval_count") or 0) + (data.get("eval_count") or 0)
        usage = {"model": self.name, "tokens": tokens,
                 "usd": round(tokens / 1000 * self.usd_per_1k, 6)}
        return content, usage

    async def _chat_stream(self, system: str, user: str, on_token=None) -> tuple[str, dict]:
        """사용자 대면 텍스트(최종 답변류) 전용 — Ollama NDJSON 스트림을 토큰 단위로
        받아 on_token(조각)을 즉시 호출해 SSE로 실시간 중계할 수 있게 한다. 분류/계획
        등 JSON 스키마 출력(route/plan/analyze)은 부분 출력이 의미가 없어 그대로 _chat을
        쓴다."""
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": True,
            "options": {"temperature": 0.0},
        }
        chunks: list[str] = []
        usage = {"model": self.name, "tokens": 0, "usd": 0.0}
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream("POST", OLLAMA_URL, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    piece = data.get("message", {}).get("content", "")
                    if piece:
                        chunks.append(piece)
                        if on_token is not None:
                            on_token(piece)
                    if data.get("done"):
                        tokens = ((data.get("prompt_eval_count") or 0)
                                  + (data.get("eval_count") or 0))
                        usage = {"model": self.name, "tokens": tokens,
                                 "usd": round(tokens / 1000 * self.usd_per_1k, 6)}
        usage["prompt"] = f"[SYSTEM]\n{system}\n\n[USER]\n{user}"
        return "".join(chunks), usage


class OllamaFastLLM(OllamaLLM):
    """분류·계획·단순 대화 전담 (저사양 소형 모델)."""

    @traceable(run_type="llm", name="ollama_fast.route")
    def route(self, query: str) -> tuple[dict, dict]:
        system = ("당신은 ICT 운영 지원 라우터다. 사용자 요청을 분류하라. "
                  "장애·성능 저하·지연·진단·모니터링·티켓 등 운영 이슈면 route=incident, "
                  "인사·잡담·일반 질문이면 route=chat. "
                  "needs_analysis는 패턴 비교/통계 분석이 꼭 필요할 때만 true.")
        content, usage = self._chat(system, query, schema=ROUTE_SCHEMA)
        try:
            raw = json.loads(content)
            route = raw.get("route") if raw.get("route") in ("chat", "incident") else None
            result = {"route": route or "chat",
                      "needs_analysis": bool(raw.get("needs_analysis", False))}
        except (json.JSONDecodeError, AttributeError):
            # 소형 모델의 형식 붕괴 대비 — 코드 레벨 fallback (W6 정신)
            incident = bool(re.search(
                r"장애|진단|다운|지연|급증|incident|티켓|로그인.*느|서비스.*느|시스템.*느|결제.*느",
                query, re.IGNORECASE))
            result = {"route": "incident" if incident else "chat",
                      "needs_analysis": False}
        return result, usage

    @traceable(run_type="llm", name="ollama_fast.resolve_target")
    def resolve_target(self, query: str, systems: list[dict]) -> tuple[dict, dict]:
        system = ("당신은 장애 조사 대상 식별기다. 등록 시스템 목록 중에서만 선택하라. "
                  "명확한 근거가 없으면 missing, 여러 후보가 가능하면 ambiguous, "
                  "명시한 이름이 목록에 없으면 unknown이다. 임의로 확정하지 마라.")
        user = f"등록 시스템(설명 포함): {json.dumps(systems, ensure_ascii=False)}\n사용자 입력: {query}"
        content, usage = self._chat(system, user, schema=TARGET_SCHEMA)
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            result = {"status": "missing", "target_systems": [],
                      "reason": "대상 판별 결과를 읽을 수 없습니다.",
                      "clarification_question": "어느 시스템을 조사할까요?"}
        return result, usage

    @traceable(run_type="llm", name="ollama_fast.rank_runbooks")
    def rank_runbooks(self, query: str, documents: list[dict]) -> tuple[dict, dict]:
        system = ("당신은 장애 대응 Runbook 검색기다. 모니터링 알림과 문서 의미가 맞는 "
                  "문서만 최대 3개 고르라. 복합 장애면 여러 문서를 고르라. "
                  "관련도 0~1 중 0.55 미만은 제외하고, 없으면 빈 matches를 반환하라.")
        user = f"검색 질의:\n{query}\n\n문서 목록:\n{json.dumps(documents, ensure_ascii=False)}"
        content, usage = self._chat(system, user, schema=RUNBOOK_RANK_SCHEMA)
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            result = {"matches": []}
        return result, usage

    @traceable(run_type="llm", name="ollama_fast.plan")
    def plan(self, query: str, procedure_prompt: str,
             targets: list[str]) -> tuple[dict, dict]:
        system = ("당신은 ICT 운영 Supervisor의 planning 노드다. 표준 진단 절차에 따라 "
                  "실행 계획(tasks)을 JSON으로 세워라. "
                  f"조사 대상은 검증된 {targets}뿐이다. 다른 시스템을 추가하지 마라. "
                  "패턴 분석이 꼭 필요하다고 판단되면 agent=analysis task를 추가해도 된다.")
        user = f"## 표준 절차\n{procedure_prompt}\n\n## 사용자 요청\n{query}"
        content, usage = self._chat(system, user, schema=PLAN_SCHEMA)

        # 소형 모델 출력에 대한 코드 레벨 정규화 — 스키마는 강제돼도 내용은 흔들릴 수
        # 있으므로(대상 시스템 오타, task 누락/중복) 실행 가능한 형태로 보정한다.
        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            raw = {"needs_analysis": False, "tasks": []}
        return normalize_plan(raw if isinstance(raw, dict) else {}, targets), usage

    @traceable(run_type="llm", name="ollama_fast.analyze")
    def analyze(self, collected: list[dict]) -> tuple[str, dict]:
        system = ("당신은 ICT 운영 패턴 분석가다. 여러 시스템의 모니터링 결과를 비교해 "
                  "공통 원인이 의심되는 패턴이 있는지 한두 문장으로 짧게 답하라. "
                  "패턴이 없으면 '특이 패턴 없음'이라고만 답하라.")
        mon = [c["data"] for c in collected if c["source"] == "monitoring"]
        facts = "\n".join(
            f"- {d['system_name']}: 상태={d['status']}, CPU {d['cpu_percent']}%, "
            f"알림 {d['alerts']}" for d in mon) or "(모니터링 데이터 없음)"
        return self._chat(system, facts)

    @traceable(run_type="llm", name="ollama_fast.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        system = ("당신은 간결한 한국어 비서다. 한두 문장으로 답하라. "
                  "대화 이력에서 사용자가 밝힌 이름이 있으면 정확히 기억해 답하라.")
        rendered = "\n".join(f"{m['role']}: {m['content']}" for m in history[-10:])
        user = (f"## 지금까지의 대화\n{rendered or '(없음)'}\n\n"
                f"## 사용자의 새 메시지\n{query}")
        return await self._chat_stream(system, user, on_token=on_token)


class OllamaQualityLLM(OllamaLLM):
    """최종 답변 작성 전담 (상대적으로 큰 모델)."""

    @traceable(run_type="llm", name="ollama_quality.write")
    async def write(self, query: str, collected: list[dict], attempt: int,
              feedback: str | None, on_token=None) -> tuple[str, dict]:
        system = ("당신은 ICT 운영 보고 작성자다. '확인된 사실', '원인 추정', "
                  "'권장 조치', '진단 한계' 네 항목으로 한국어 보고서를 작성하라. "
                  "조회된 시스템만 확정 대상으로 쓰고, 근거 없는 수치를 만들지 마라. "
                  "사용한 모든 Runbook ID와 실패한 조회를 표시하라. "
                  "Runbook ID만 나열하거나 '문서를 확인하세요'로 끝내지 말고, "
                  "실제로 적용할 각 Runbook 원문에서 구체적인 점검 절차를 적어도 하나씩 "
                  "권장 조치에 설명하고 해당 ID와 단계 번호를 붙여라. "
                  "각 Runbook은 해당 증상이 확인된 시스템에만 적용하라. 예를 들어 "
                  "CPU가 낮게 측정된 시스템에 CPU 급증 Runbook의 조치를 권하지 마라. "
                  "조회에 실패한 시스템의 증상은 미확인이므로 우선 재조회만 제안하고, "
                  "다른 시스템에 그 Runbook을 억지로 적용하지 마라. "
                  "조회 실패한 대상은 실제 지표를 확인하기 전까지 상태나 조치 조건이 "
                  "충족됐다고 단정하지 마라. 제안한 절차를 이미 수행한 것처럼 쓰지 마라. "
                  "근거 없으면 모른다고 하라.")
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
        return await self._chat_stream(system, user, on_token=on_token)

    @traceable(run_type="llm", name="ollama_quality.judge")
    def judge(self, draft: str, evidence: dict) -> tuple[dict, dict]:
        system = ("당신은 장애 보고서의 근거 검증자다. 다음 경우에만 반려하라: "
                  "(1) '확인된 사실'의 시스템·수치·알림이 제공된 모니터링과 다름, "
                  "(2) 조회하지 못한 대상을 확인했다고 주장하거나 실패를 진단 한계에 누락, "
                  "(3) 실제 실행하지 않은 조치를 완료했다고 주장, "
                  "(4) 인용한 Runbook의 출처가 없거나 제안한 절차가 원문에 없음, "
                  "(5) Runbook ID만 언급하고 구체적인 점검 절차를 설명하지 않음, "
                  "(6) 증상과 무관한 시스템에 Runbook을 적용함. 예를 들어 CPU가 "
                  "낮게 측정된 시스템에 CPU 급증 대응 절차를 권하면 반려하라. "
                  "'권장 조치'는 앞으로 할 일을 제안하는 항목이므로 아직 실행되지 않아도 "
                  "정상이다. 조회 실패를 명시한 보고서는 그 이유만으로 반려하지 마라. "
                  "조건부 조치는 조건이 아직 확인되지 않았어도 조건을 밝히면 허용한다. "
                  "추정이라고 밝힌 원인은 확정 사실로 취급하지 마라. 검색됐지만 "
                  "사용하지 않은 Runbook은 인용할 필요가 없다. "
                  "반려할 때는 위반한 보고서 문장을 인용해 모순을 설명하라. "
                  "위반이 없으면 passed=true, issues=[]로 답하라. 한국어로 판정하라.")
        user = (f"근거: {json.dumps(evidence, ensure_ascii=False)}\n\n"
                f"검증할 보고서:\n{draft}")
        content, usage = self._chat(system, user, schema=JUDGE_SCHEMA)
        return json.loads(content), usage
