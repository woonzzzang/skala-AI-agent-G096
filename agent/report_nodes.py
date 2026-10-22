"""Draft, grounded validation, and final user-facing report nodes."""

from langgraph.config import get_stream_writer
from langsmith import traceable

from .answer_validation import evidence_for_judge, unverified_fallback, validate_report
from .deps import SupervisorDeps
from .graph_state import AgentState, cost


@traceable(name="write_node")
async def write_node(state: AgentState, deps: SupervisorDeps) -> dict:
    attempt = state.get("validation_attempts", 0) + 1
    writer = get_stream_writer()
    collected = state.get("collected", []) + [
        {"source": "failure", "data": {"target": task.get("target"),
         "kind": task.get("kind"), "error": task.get("error", "")}}
        for task in state.get("tasks", []) if task.get("status") == "failed"
    ]
    draft, usage = await deps.quality_llm.write(
        state["query"], collected, attempt, state.get("validation_feedback"),
        on_token=lambda piece: writer(
            {"type": "AnswerToken", "attempt": attempt, "delta": piece}))
    return {"draft": draft, "last_prompt": usage.get("prompt"),
            "cost_usd": cost(state, usage),
            "events": state["events"] + [{"type": "DraftWritten", "attempt": attempt,
                                          "length": len(draft)}]}


@traceable(name="validate_node")
async def validate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Rules and independent model judgement; never force acceptance."""
    attempts = state.get("validation_attempts", 0) + 1
    evidence = evidence_for_judge(state.get("target_systems", []),
                                  state.get("collected", []), state.get("tasks", []))
    result, usage = validate_report(state["draft"], evidence, deps.quality_llm)
    event = {"type": "Validation", "attempt": attempts,
             "verdict": "합격" if result["passed"] else "불합격",
             "issues": result["issues"]}
    return {"validation_attempts": attempts,
            "validation_feedback": result["feedback"],
            "validation_passed": result["passed"],
            "validation_issues": result["issues"],
            "cost_usd": cost(state, usage), "events": state["events"] + [event]}


@traceable(name="finalize_node")
async def finalize_node(state: AgentState, deps: SupervisorDeps) -> dict:
    verified = state.get("validation_passed", False)
    evidence = evidence_for_judge(state.get("target_systems", []),
                                  state.get("collected", []), state.get("tasks", []))
    final = state["draft"] if verified else unverified_fallback(
        evidence, state.get("validation_feedback", "검증 미통과"))
    return {"final": final,
            "events": state["events"] + [{"type": "Finalize",
                                          "pipeline": "incident-v2", "verified": verified}]}
