"""Resolve only registered systems and pause the graph for clarification."""

from langgraph.types import interrupt
from langsmith import traceable

from .deps import SupervisorDeps
from .graph_state import AgentState, cost
from .system_registry import SystemRegistry


@traceable(name="resolve_target_node")
async def resolve_target_node(state: AgentState, deps: SupervisorDeps) -> dict:
    registry = SystemRegistry()  # Reload current monitoring.json for each resolution.
    result, usage = deps.fast_llm.resolve_target(
        query=state.get("clarification_answer") or state["query"],
        systems=registry.to_prompt_catalog(),
    )
    raw_targets = result.get("target_systems", [])
    if not isinstance(raw_targets, list):
        raw_targets = []
    validated_targets = registry.validate_candidates(raw_targets)
    status = result.get("status", "missing")
    if status not in {"resolved", "missing", "unknown", "ambiguous"}:
        status = "missing"
    if status == "resolved":
        if not raw_targets:
            status = "missing"
        elif len(validated_targets) != len(raw_targets):
            status = "unknown"
    if status != "resolved":
        validated_targets = []
    event = {"type": "TargetResolution", "status": status,
             "targets": validated_targets}
    return {
        "target_systems": validated_targets,
        "target_status": status,
        "target_reason": result.get("reason", ""),
        "clarification_question": ("" if status == "resolved" else
                                   result.get("clarification_question") or
                                   "어느 시스템을 조사할까요?"),
        "cost_usd": cost(state, usage),
        "events": state.get("events", []) + [event],
    }


@traceable(name="clarify_target_node")
async def clarify_target_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """No tool or LLM calls before interrupt: this node restarts on resume."""
    answer = interrupt({"type": "TargetClarificationRequired",
                        "question": state["clarification_question"],
                        "original_query": state["query"]})
    return {"clarification_answer": str(answer).strip(),
            "events": state["events"] + [{"type": "TargetClarificationAnswered"}]}
