"""Grounded report validation: deterministic checks plus a model judge."""

import re
from typing import Any

SECTIONS = ("확인된 사실", "원인 추정", "권장 조치", "진단 한계")
MEASURE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|ms|s|건)")


def evidence_for_judge(targets: list[str], collected: list[dict],
                       tasks: list[dict]) -> dict[str, Any]:
    return {
        "target_systems": targets,
        "monitoring": [item["data"] for item in collected
                       if item.get("source") == "monitoring"],
        "runbooks": [{"id": item.get("id"), "text": item.get("data")}
                     for item in collected if item.get("source") == "runbook"],
        "failed_tasks": [{"kind": task.get("kind"), "target": task.get("target"),
                          "error": task.get("error", "")}
                         for task in tasks if task.get("status") == "failed"],
    }


def grounded_issues(draft: str, evidence: dict[str, Any]) -> list[str]:
    issues = [f"'{section}' 항목이 없습니다." for section in SECTIONS
              if section not in draft]
    for target in evidence["target_systems"]:
        if target not in draft:
            issues.append(f"확정된 시스템 {target}명이 보고서에 없습니다.")
    if not evidence["monitoring"]:
        issues.append("확인된 모니터링 결과가 없어 진단 보고서를 검증할 수 없습니다.")
    for failure in evidence["failed_tasks"]:
        if failure["target"] not in draft or "실패" not in draft:
            issues.append(f"{failure['target']} 조회 실패를 진단 한계에 밝혀야 합니다.")

    supported = " ".join(
        [str(item["text"]) for item in evidence["runbooks"]]
        + [" ".join(map(str, item.get("alerts", [])))
           for item in evidence["monitoring"]])
    allowed = {match.replace(" ", "") for match in MEASURE.findall(supported)}
    for item in evidence["monitoring"]:
        for key, value in item.items():
            if key.endswith("_percent") and isinstance(value, (int, float)):
                allowed.add(f"{value}%")
        if isinstance(item.get("alerts"), list):
            allowed.add(f"{len(item['alerts'])}건")
    for value in MEASURE.findall(draft):
        if value.replace(" ", "") not in allowed:
            issues.append(f"근거에 없는 수치 {value}가 포함됐습니다.")
    for runbook in evidence["runbooks"]:
        if runbook["id"] and runbook["id"] not in draft:
            issues.append(f"참고한 Runbook {runbook['id']} 출처가 빠졌습니다.")
    return list(dict.fromkeys(issues))


def validate_report(draft: str, evidence: dict[str, Any], judge: Any
                    ) -> tuple[dict, dict]:
    rules = grounded_issues(draft, evidence)
    try:
        decision, usage = judge.judge(draft, evidence)
        if not isinstance(decision, dict) or type(decision.get("passed")) is not bool:
            raise ValueError("Judge가 올바른 판정 형식을 반환하지 않았습니다.")
        judge_issues = decision.get("issues", [])
        if not isinstance(judge_issues, list):
            judge_issues = ["Judge 반려 사유 형식이 잘못됐습니다."]
        if not decision["passed"]:
            judge_issues = [str(item) for item in judge_issues] or [
                str(decision.get("feedback") or "Judge가 반려했습니다.")]
    except Exception as exc:
        usage, judge_issues = {"usd": 0.0}, [f"Judge 검증 실패: {exc}"]
    issues = list(dict.fromkeys(rules + judge_issues))
    return {"passed": not issues, "issues": issues,
            "feedback": " ".join(issues) if issues else ""}, usage


def unverified_fallback(evidence: dict[str, Any], feedback: str) -> str:
    lines = ["검증 미통과: 보고서 초안을 신뢰할 수 없어 확인된 사실만 제공합니다.",
             "확인된 사실"]
    for item in evidence["monitoring"]:
        lines.append(f"- {item['system_name']}: 상태 {item['status']}, "
                     f"CPU {item['cpu_percent']}%, 메모리 {item['memory_percent']}% "
                     f"(조회: {item.get('last_checked', '시각 미상')})")
    if not evidence["monitoring"]:
        lines.append("- 확인된 모니터링 결과가 없습니다.")
    lines.append("진단 한계")
    for item in evidence["failed_tasks"]:
        lines.append(f"- {item['target']} {item['kind']} 조회 실패: {item['error']}")
    lines.append(f"- 검증 사유: {feedback}")
    return "\n".join(lines)
