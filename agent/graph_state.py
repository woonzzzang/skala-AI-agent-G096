"""LangGraph state contract shared by nodes and graph wiring."""

from typing import TypedDict


class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    history: list[dict]
    route: str
    needs_analysis: bool
    target_systems: list[str]
    target_status: str
    target_reason: str
    clarification_question: str
    clarification_answer: str
    tasks: list[dict]
    gate_removed: int
    collected: list[dict]
    runbook_matches: list[dict]
    runbook_search_status: str
    replan_rounds: int
    retry_count: int
    parallel_elapsed: float
    draft: str
    validation_attempts: int
    validation_feedback: str
    validation_passed: bool
    validation_issues: list[str]
    final: str
    last_prompt: str
    cost_usd: float
    events: list[dict]


def cost(state: AgentState, usage: dict) -> float:
    return round(state.get("cost_usd", 0.0) + usage.get("usd", 0.0), 6)
