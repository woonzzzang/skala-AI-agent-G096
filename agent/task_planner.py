"""Turn a model's plan into tasks for registry-validated systems only."""

from typing import Any


def normalize_plan(raw: dict[str, Any], targets: list[str]) -> dict[str, Any]:
    """The model may request analysis, but may not invent monitoring targets."""
    verified = list(dict.fromkeys(targets))
    if not verified:
        raise ValueError("확정된 조사 대상 시스템이 없습니다.")

    tasks = [
        {"id": f"task-{index}", "agent": "research", "kind": "monitoring",
         "target": target, "status": "pending"}
        for index, target in enumerate(verified)
    ]
    proposed = raw.get("tasks", [])
    if not isinstance(proposed, list):
        proposed = []
    has_analysis = any(
        isinstance(task, dict) and task.get("agent") == "analysis"
        for task in proposed
    )
    needs_analysis = raw.get("needs_analysis") is True
    if needs_analysis or has_analysis:
        tasks.append({"id": "task-analysis", "agent": "analysis",
                      "kind": "pattern-analysis", "status": "pending"})
    return {"needs_analysis": needs_analysis, "tasks": tasks}
