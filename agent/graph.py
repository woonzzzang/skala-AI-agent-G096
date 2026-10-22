"""v2 Supervisor — 실제 langgraph.StateGraph (Wave 4·5·6·7·8·10b·15 통합).

route → (chat 직행 | resolve_target → [clarify_target] → incident 파이프라인)
incident: plan → gate → research(MCP Monitoring) → replan_check(Runbook 검색)
          → research(MCP Runbook 원문) → analysis → write → validate → finalize

각 노드는 SupervisorDeps를 functools.partial로 전달받는다(Wave 13).
주요 노드는 @traceable로 LangSmith에 계층 트리로 기록된다.
"""
import asyncio
import time
from functools import partial

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langsmith import traceable

from .deps import SupervisorDeps
from .graph_state import AgentState, cost as _cost
from .report_nodes import write_node, validate_node, finalize_node
from .retry import with_retry
from .runbook_retrieval import RunbookRetriever
from .target_nodes import resolve_target_node, clarify_target_node


@traceable(name="route_node")
async def route_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 — 모든 쿼리가 무거운 파이프라인을 탈 필요는 없다."""
    result, usage = deps.fast_llm.route(state["query"])
    event = {"type": "RouteDecided", "route": result["route"]}
    return {"route": result["route"], "needs_analysis": result["needs_analysis"],
            "cost_usd": _cost(state, usage),
            "events": state.get("events", []) + [event]}

@traceable(name="chat_answer_node")
async def chat_answer_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 direct 경로 — 히스토리 기반 응답 (Wave 12 세션 증명 지점).

    토큰 단위 스트리밍 — get_stream_writer()는 /chat/stream처럼 custom 모드로 소비될
    때만 실제로 전송되고, /chat(ainvoke)처럼 스트리밍하지 않는 호출에서는 자동으로
    no-op이 된다."""
    writer = get_stream_writer()
    reply, usage = await deps.fast_llm.chat(
        state["query"], state.get("history", []),
        on_token=lambda piece: writer({"type": "AnswerToken", "delta": piece}))
    return {"final": reply, "last_prompt": usage.get("prompt"),
            "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "Finalize", "pipeline": "chat-direct"}]}


@traceable(name="plan_node")
async def plan_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 4 — Plan/Task 분해. Wave 2의 MCP Prompt가 표준 절차를 깔아준다.

    resource와 마찬가지로 이름을 어느 서버가 들고 있는지 미리 가정하지 않고,
    find_prompt()로 연결된 서버들을 먼저 조회해 찾은 뒤에만 get_prompt를 보낸다."""
    targets = state["target_systems"]
    if not targets:
        raise ValueError("확정된 조사 대상 없이 Planning을 시작할 수 없습니다.")
    prompt_server = await deps.mcp.find_prompt("incident_analysis_prompt")
    procedure = await deps.mcp.get_prompt(
        "incident_analysis_prompt",
        {"system_name": ", ".join(targets), "symptom": state["query"]},
        server=prompt_server,
    )
    result, usage = deps.fast_llm.plan(state["query"], procedure, targets)
    event = {"type": "PlanCreated", "task_count": len(result["tasks"]),
             "targets": targets}
    return {"tasks": result["tasks"], "needs_analysis": result["needs_analysis"],
            "cost_usd": _cost(state, usage), "events": state["events"] + [event]}


@traceable(name="gate_node")
async def gate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 — 권위 게이트. needs_analysis=False인데 섞여 들어온 analysis task를
    프롬프트가 아니라 코드로 강제 제거한다."""
    tasks = state["tasks"]
    if not state.get("needs_analysis", False):
        kept = [t for t in tasks if t["agent"] != "analysis"]
        removed = len(tasks) - len(kept)
    else:
        kept, removed = tasks, 0
    event = {"type": "GateApplied", "removed_analysis_tasks": removed}
    return {"tasks": kept, "gate_removed": removed,
            "events": state["events"] + [event]}


@traceable(name="research_node")
async def research_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 15 가드 + Wave 10b 병렬 + Wave 9 재시도 + Wave 2 Tool/Resource 실호출."""
    tasks = state["tasks"]
    research_indices = [i for i, t in enumerate(tasks)
                        if t["agent"] == "research" and t["status"] == "pending"]
    # Wave 15 — 객체가 아니라 '실행할 목록'을 검사한다 (not research_agent 버그의 수정판)
    if not research_indices:
        return {"events": state["events"] +
                [{"type": "ResearchSkipped", "reason": "pending research task 0개"}]}

    retry_total = state.get("retry_count", 0)

    # URI를 코드에 하드코딩하지 않는다. 여러 MCP 서버가 연결돼 있고 그중 어느 서버가
    # "get_incident_runbook" resource를 제공하는지 모른다고 가정하고, 먼저 연결된
    # 서버들을 조회(list)해 유효한 서버를 찾은 뒤에만 그 서버로 요청한다.
    needs_runbook = any(tasks[i]["kind"] != "monitoring" for i in research_indices)
    runbook_server = runbook_uri_template = None
    runbook_lookup_error = None
    if needs_runbook:
        try:
            runbook_server, runbook_uri_template = await deps.mcp.find_resource_template(
                "get_incident_runbook")
        except Exception as exc:
            runbook_lookup_error = str(exc)

    async def run_one(idx: int) -> dict:
        task = tasks[idx]
        try:
            if task["kind"] == "monitoring":
                data, retries = await with_retry(
                    deps.mcp.call_tool, "get_monitoring_status",
                    {"system_name": task["target"]})
                if data.get("status") == "unknown":
                    raise ValueError(f"Monitoring에 {task['target']} 대상이 없습니다.")
                return {"idx": idx, "source": "monitoring", "data": data,
                        "retries": retries, "ok": True}
            if runbook_lookup_error:
                raise RuntimeError(runbook_lookup_error)
            uri = runbook_uri_template.format(incident_type=task["target"])
            data = await deps.mcp.read_resource(uri, server=runbook_server)
            if not data or "Runbook Not Found" in data:
                raise ValueError(f"{task['target']} Runbook을 찾을 수 없습니다.")
            return {"idx": idx, "source": "runbook", "data": data,
                    "retries": 0, "ok": True}
        except Exception as exc:
            return {"idx": idx, "source": task["kind"], "error": str(exc),
                    "retries": 0, "ok": False}

    started = time.perf_counter()
    results = await asyncio.gather(*(run_one(i) for i in research_indices))
    elapsed = round(time.perf_counter() - started, 2)

    new_tasks = [dict(t) for t in tasks]
    collected = list(state.get("collected", []))
    events = list(state["events"])
    for r in results:
        task = new_tasks[r["idx"]]
        task["status"] = "done" if r["ok"] else "failed"
        if r["ok"]:
            item = {"source": r["source"], "data": r["data"]}
            if r["source"] == "runbook":
                item.update({"id": task["target"], "score": task.get("score")})
            collected.append(item)
        else:
            task["error"] = r["error"]
        retry_total += r["retries"]
        events.append({"type": "TaskCompleted" if r["ok"] else "TaskFailed",
                       "id": task["id"], "source": r["source"],
                       "target": task["target"], "retries": r["retries"],
                       **({"error": r["error"]} if not r["ok"] else {})})
    return {"tasks": new_tasks, "collected": collected, "retry_count": retry_total,
            "parallel_elapsed": round(state.get("parallel_elapsed", 0) + elapsed, 2),
            "events": events}


@traceable(name="replan_check_node")
async def replan_check_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Search once after monitoring; execute all relevant Runbook tasks next."""
    rounds = state.get("replan_rounds", 0)
    if rounds >= 1:
        found = any(c["source"] == "runbook" for c in state.get("collected", []))
        verdict = {"type": "ReplanCheck", "round": rounds, "is_sufficient": True,
                   "runbook_found": found}
        return {"events": state["events"] + [verdict]}
    try:
        matches, usage = RunbookRetriever().search(
            state["query"], state.get("collected", []), deps.fast_llm)
        search_status = "matched" if matches else "no_match"
        error = ""
    except Exception as exc:
        matches, usage, search_status, error = [], {"usd": 0.0}, "error", str(exc)
    gap_tasks = [
        {"id": f"task-runbook-{match['id']}", "agent": "research",
         "kind": "runbook", "target": match["id"], "score": match["score"],
         "status": "pending"}
        for match in matches
    ]
    events = state["events"] + [{"type": "RunbookSearch", "status": search_status,
                                  "matches": matches, "error": error},
                                 {"type": "ReplanCheck", "round": rounds,
                                  "is_sufficient": not bool(gap_tasks)}]
    return {"tasks": state["tasks"] + gap_tasks, "runbook_matches": matches,
            "runbook_search_status": search_status, "replan_rounds": rounds + 1,
            "cost_usd": _cost(state, usage), "events": events}


@traceable(name="analysis_node")
async def analysis_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 보완 — needs_analysis=True가 정당한 경우(모델의 자기모순이 아니라
    실제로 패턴 비교가 필요하다고 판단한 경우)를 위한 실행 경로. gate가 살려둔
    agent=='analysis' task가 있을 때만 수집된 monitoring 결과들을 비교 분석한다."""
    tasks = state["tasks"]
    pending = [t for t in tasks if t["agent"] == "analysis" and t["status"] == "pending"]
    if not pending:
        return {"events": state["events"] +
                [{"type": "AnalysisSkipped", "reason": "analysis task 0개"}]}
    summary, usage = deps.fast_llm.analyze(state.get("collected", []))
    new_tasks = [dict(t) for t in tasks]
    for t in new_tasks:
        if t["agent"] == "analysis" and t["status"] == "pending":
            t["status"] = "done"
    collected = state.get("collected", []) + [{"source": "analysis", "data": summary}]
    event = {"type": "AnalysisCompleted", "length": len(summary)}
    return {"tasks": new_tasks, "collected": collected,
            "cost_usd": _cost(state, usage), "events": state["events"] + [event]}


def _after_route(state: AgentState) -> str:
    return "chat_answer" if state["route"] == "chat" else "resolve_target"


def _after_target(state: AgentState) -> str:
    return "plan" if state["target_status"] == "resolved" else "clarify_target"


def _after_replan_check(state: AgentState) -> str:
    last = state["events"][-1]
    return "analysis" if last.get("is_sufficient") else "research"


def _after_validate(state: AgentState) -> str:
    return ("finalize" if state.get("validation_passed") or
            state.get("validation_attempts", 0) >= 3 else "write")


def build_graph(deps: SupervisorDeps, checkpointer=None):
    g = StateGraph(AgentState)
    node = lambda fn: partial(fn, deps=deps)   # Wave 13 — 명시적 의존성 주입
    g.add_node("route", node(route_node))
    g.add_node("resolve_target", node(resolve_target_node))
    g.add_node("clarify_target", node(clarify_target_node))
    g.add_node("chat_answer", node(chat_answer_node))
    g.add_node("plan", node(plan_node))
    g.add_node("gate", node(gate_node))
    g.add_node("research", node(research_node))
    g.add_node("replan_check", node(replan_check_node))
    g.add_node("analysis", node(analysis_node))
    g.add_node("write", node(write_node))
    g.add_node("validate", node(validate_node))
    g.add_node("finalize", node(finalize_node))

    g.set_entry_point("route")
    g.add_conditional_edges("route", _after_route,
                            {"chat_answer": "chat_answer", "resolve_target": "resolve_target"})
    g.add_conditional_edges("resolve_target", _after_target,
                            {"plan": "plan", "clarify_target": "clarify_target"})
    g.add_edge("clarify_target", "resolve_target")
    g.add_edge("chat_answer", END)
    g.add_edge("plan", "gate")
    g.add_edge("gate", "research")
    g.add_edge("research", "replan_check")
    g.add_conditional_edges("replan_check", _after_replan_check,
                            {"analysis": "analysis", "research": "research"})
    g.add_edge("analysis", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", _after_validate,
                            {"finalize": "finalize", "write": "write"})
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
