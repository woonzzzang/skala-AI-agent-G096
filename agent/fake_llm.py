"""Wave 4/10a — 결정론적 Fake LLM 2종 (fast/quality) + 실측 비용 계산.

실제 LLM 호출 없이도 파이프라인 전체가 결정론적으로 재현되도록, 프롬프트 내용에 따라
정해진 응답을 돌려준다. 각 호출은 LangSmith에 run_type="llm"으로 기록되고, 문자열
길이 기반 토큰 추정치로 비용을 계산해 상태에 누적한다 (Wave 10a의 모델 분리 근거).
"""
import re

from langsmith import traceable

from .task_planner import normalize_plan


class FakeLLM:
    def __init__(self, name: str, usd_per_1k_tokens: float):
        self.name = name
        self.usd_per_1k = usd_per_1k_tokens

    def _usage(self, prompt: str, completion: str) -> dict:
        tokens = (len(prompt) + len(completion)) // 4 + 1
        return {"model": self.name, "tokens": tokens,
                "usd": round(tokens / 1000 * self.usd_per_1k, 6)}


class FastLLM(FakeLLM):
    """분류·계획 등 짧고 정형적인 단계 전담 (저비용)."""

    @traceable(run_type="llm", name="fast_llm.route")
    def route(self, query: str) -> tuple[dict, dict]:
        """Wave 5 — 쿼리 라우팅. 장애 관련이면 incident 파이프라인, 아니면 chat 직행."""
        incident = bool(re.search(
            r"장애|진단|다운|지연|급증|오류|실패|incident|티켓|로그인.*(느|안)|서비스.*(느|안)|시스템.*(느|안)|결제.*(느|안)",
            query, re.IGNORECASE))
        result = {"route": "incident" if incident else "chat", "needs_analysis": False}
        return result, self._usage(query, str(result))

    @traceable(
            run_type="llm",
            name="fast_llm.resolve_target",
    )
    def resolve_target(
        self,
        query: str,
        systems: list[dict],
    ) -> tuple[dict, dict]:
        """
        테스트용 대상 시스템 판별, 자연어 의미 추론은 실제 LLM이 담당
        """
        known = {
            item["name"].casefold(): item["name"]
            for item in systems
            if isinstance(item.get("name"), str)
        }
        normalized_query = query.casefold()

        # 1. 사용자가 등록된 시스템명을 직접 입력한 경우 확인 
        explicit_targets = [
            original_name
            for normalized_name, original_name in known.items()
            if re.search(
                rf"(?<![a-z0-9_-]){re.escape(normalized_name)}(?![a-z0-9_-])",
                normalized_query,
            )
        ]

        # 2. 사용자가 미등록 시스템명을 직접 입력한 경우 확인 
        mentioned_systems = re.findall(
            r"(?<![a-z0-9_-])[a-z][a-z0-9-]+-(?:service|\d+)(?![a-z0-9-])",
            normalized_query,
        )

        unknown_names = [
            name
            for name in dict.fromkeys(mentioned_systems)
            if name not in known
        ]

        # Fake backend only: use rare character pairs in descriptions as a
        # deterministic stand-in for semantic inference (no fixed stop-word list).
        def grams(text: str) -> set[str]:
            words = re.findall(r"[가-힣a-z0-9]+", text.casefold())
            return {word[index:index + width]
                    for word in words for width in (2, 3, 4)
                    for index in range(len(word) - width + 1)}

        descriptions = {
            item["name"]: grams(item.get("description", ""))
            for item in systems if isinstance(item.get("name"), str)
        }
        query_pairs = grams(normalized_query)
        scores = {
            name: sum(
                1 for pair in (pairs & query_pairs)
                if sum(pair in other for other in descriptions.values()) == 1)
            for name, pairs in descriptions.items()
        }
        best = max(scores.values(), default=0)
        inferred = [name for name, score in scores.items() if score == best] if best else []

        if unknown_names:
            result = {
                "status": "unknown",
                "target_systems": [],
                "candidates": unknown_names,
                "reason": "사용자가 언급한 시스템이 등록된 시스템 목록에 없습니다.",
                "clarification_question": (
                    "해당 시스템은 현재 조회할 수 없습니다. "
                    "조회할 다른 시스템명을 알려주세요."
                ),
            }
        elif explicit_targets:
            result = {
                "status": "resolved",
                "target_systems": explicit_targets,
                "candidates": [],
                "reason": "등록된 시스템명을 직접 명시했습니다.",
                "clarification_question": "",
            }
        elif len(inferred) == 1:
            result = {
                "status": "resolved", "target_systems": inferred,
                "candidates": [], "reason": "등록된 시스템 설명과 표현이 일치합니다.",
                "clarification_question": "",
            }
        elif len(inferred) > 1:
            result = {
                "status": "ambiguous", "target_systems": [],
                "candidates": inferred,
                "reason": "여러 시스템 설명과 일치해 하나로 확정할 수 없습니다.",
                "clarification_question": "어느 시스템을 조사할까요?",
            }
        else:
            result = {
                "status": "missing",
                "target_systems": [],
                "candidates": [],
                "reason": "질문에서 대상 시스템을 특정할 수 없습니다.",
                "clarification_question": "어느 시스템에 관한 문제인지 좀 더 자세히 알려주세요.",
            }

        return result, self._usage(query + str(systems), str(result))

        

    @traceable(run_type="llm", name="fast_llm.plan")
    def plan(self, query: str, procedure_prompt: str,
             targets: list[str]) -> tuple[dict, dict]:
        """Wave 4 — Plan/Task 분해. MCP Prompt가 준 표준 절차를 따라 계획을 세운다.

        Wave 6 재현: needs_analysis=False라고 스스로 판단해 놓고도 analysis task를
        끼워 넣는 '모델의 자기모순'을 결정론적으로 재현한다 → gate 노드가 걸러낸다.
        """
        result = normalize_plan(
            {"needs_analysis": False, "tasks": [{"agent": "analysis"}]}, targets)
        return result, self._usage(query + procedure_prompt, str(result))

    @traceable(run_type="llm", name="fast_llm.rank_runbooks")
    def rank_runbooks(self, query: str, documents: list[dict]) -> tuple[dict, dict]:
        """Deterministic lexical test double; real backends do semantic ranking."""
        def terms(text: str) -> set[str]:
            return set(re.findall(r"[a-z0-9]+|[가-힣]{2,}", text.casefold()))

        query_terms = terms(query)
        doc_terms = {d["id"]: terms(d["id"] + " " + d["title"] + " " + d["excerpt"])
                     for d in documents}
        matches = []
        for document in documents:
            doc_id = document["id"]
            distinctive = {term for term in doc_terms[doc_id]
                           if sum(term in other for other in doc_terms.values()) == 1}
            overlap = distinctive & query_terms
            if overlap:
                matches.append({"id": doc_id,
                                "score": round(min(1.0, 0.55 + 0.1 * len(overlap)), 2),
                                "reason": "일치 표현: " + ", ".join(sorted(overlap))})
        result = {"matches": matches}
        return result, self._usage(query + str(documents), str(result))

    @traceable(run_type="llm", name="fast_llm.analyze")
    def analyze(self, collected: list[dict]) -> tuple[str, dict]:
        """Wave 6 보완 — 여러 시스템의 monitoring 결과 사이에 공통 패턴이 있는지 비교."""
        mon = [c["data"] for c in collected if c["source"] == "monitoring"]
        degraded = [d["system_name"] for d in mon if d.get("status") != "healthy"]
        if len(degraded) >= 2:
            summary = (f"{', '.join(degraded)} 복수 시스템이 동시에 저하됨 — "
                       f"공유 의존성 등 공통 원인이 의심됨")
        elif degraded:
            summary = f"{degraded[0]}만 단독 저하 — 연관 시스템은 정상, 국소 이슈로 추정"
        else:
            summary = "모니터링된 시스템 모두 healthy — 패턴상 이상 징후 없음"
        return summary, self._usage(str(collected), summary)

    @traceable(run_type="llm", name="fast_llm.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        """Wave 5의 direct 경로 + Wave 12 증명용 — 히스토리에서 이름을 기억해낸다.

        fake 백엔드는 실제 네트워크 호출이 없어 토큰 단위로 쪼갤 대상이 없다 —
        ollama_llm.py와 인터페이스(on_token 콜백)만 맞추고 결과를 한 번에 반환한다."""
        for msg in reversed(history):
            m = re.search(r"제 이름은 (\S+?)입니다", msg.get("content", ""))
            if m and re.search(r"이름이 뭐", query):
                reply = f"{m.group(1)}님이라고 말씀하셨어요. 무엇을 도와드릴까요?"
                if on_token is not None:
                    on_token(reply)
                return reply, self._usage(query, reply)
        m = re.search(r"제 이름은 (\S+?)입니다", query)
        if m:
            reply = f"안녕하세요, {m.group(1)}님! 무엇을 도와드릴까요?"
        else:
            reply = "안녕하세요! 무엇을 도와드릴까요?"
        if on_token is not None:
            on_token(reply)
        return reply, self._usage(query, reply)


class QualityLLM(FakeLLM):
    """최종 답변 작성 전담 (고품질·고비용)."""

    @traceable(run_type="llm", name="quality_llm.write")
    async def write(self, query: str, collected: list[dict], attempt: int,
              feedback: str | None, on_token=None) -> tuple[str, dict]:
        """Wave 8 재현: 1차 초안은 의도적으로 저품질(너무 짧음) → validator가 반려하면
        피드백을 반영한 구조화된 2차 초안을 내놓는다.

        fake 백엔드는 실제 네트워크 호출이 없어 토큰 단위로 쪼갤 대상이 없다 —
        ollama_llm.py와 인터페이스(on_token 콜백)만 맞추고 결과를 한 번에 반환한다."""
        if attempt == 1:
            draft = "재시작하면 됩니다."
            if on_token is not None:
                on_token(draft)
            return draft, self._usage(query, draft)

        mon = [c["data"] for c in collected if c["source"] == "monitoring"]
        run = [c for c in collected if c["source"] == "runbook"]
        failures = [c["data"] for c in collected if c["source"] == "failure"]
        lines = ["확인된 사실"]
        lines.extend(
            f"- {d['system_name']}: 상태 {d['status']}, CPU {d['cpu_percent']}%, "
            f"메모리 {d['memory_percent']}%, 알림 {len(d['alerts'])}건 "
            f"(조회: {d.get('last_checked', '시각 미상')})"
            for d in mon)
        if not mon:
            lines.append("- 확인된 모니터링 결과가 없습니다.")
        lines.extend(["원인 추정", "- 관련 Runbook을 참고해 원인을 추가 확인해야 합니다.",
                      "권장 조치"])
        lines.extend(f"- Runbook {c['id']}: 문서의 진단 절차를 확인하고 조치하세요."
                     for c in run)
        if not run:
            lines.append("- 관련 Runbook을 찾지 못해 일반적인 원인 단정은 보류합니다.")
        lines.extend(["진단 한계"])
        lines.extend(f"- {d['target']} {d['kind']} 조회 실패: {d['error']}"
                     for d in failures)
        if not failures:
            lines.append("- 조회 실패는 없습니다. 조치 전 실제 환경에서 재확인하세요.")
        draft = "\n".join(lines)
        if on_token is not None:
            on_token(draft)
        return draft, self._usage(query + str(feedback), draft)

    @traceable(run_type="llm", name="quality_llm.judge")
    def judge(self, draft: str, evidence: dict) -> tuple[dict, dict]:
        """Deterministic test double; real providers use an LLM judge."""
        from .answer_validation import grounded_issues
        issues = grounded_issues(draft, evidence)
        if "원인 추정" in draft and "단정" not in draft and not evidence["runbooks"]:
            issues.append("근거 없는 원인 단정을 피해야 합니다.")
        result = {"passed": not issues, "issues": issues,
                  "feedback": " ".join(issues)}
        return result, self._usage(draft + str(evidence), str(result))
