"""Reproducible Fake-backend comparison against the last pre-improvement commit.

Run from the repository root: LANGCHAIN_TRACING_V2=false .venv/bin/python -B scripts/evaluate.py
The baseline source is loaded read-only with git show; no checkout or source changes.
"""

import asyncio
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.deps import SupervisorDeps  # noqa: E402
from agent.fake_llm import FastLLM, QualityLLM  # noqa: E402
from agent.graph import build_graph  # noqa: E402
from tests.test_improvements import StubMCP  # noqa: E402

BASELINE = "2c6ad9a"
CASES = [
    ("natural-language", "로그인 응답이 느려요"),
    ("multi-symptom", "payments-service와 gateway-service CPU 급증 및 timeout 장애"),
    ("unknown-target", "billing-service 장애 원인 확인해줘"),
]


def old_module(path):
    source = subprocess.check_output(
        ["git", "show", f"{BASELINE}:{path}"], cwd=ROOT, text=True)
    namespace = {"__name__": "agent._evaluation_baseline",
                 "__package__": "agent", "__file__": str(ROOT / path)}
    exec(compile(source, f"{BASELINE}:{path}", "exec"), namespace)
    return namespace


async def run_one(builder, fast_class, quality_class, query, label):
    mcp = StubMCP()
    deps = SupervisorDeps(fast_class("fast-fake", 0.00015),
                          quality_class("quality-fake", 0.005),
                          mcp, None, "v2")
    graph = builder(deps, InMemorySaver()) if label == "after" else builder(deps)
    started = time.perf_counter()
    result = await graph.ainvoke(
        {"query": query, "thread_id": f"eval-{label}", "history": [],
         "events": [], "cost_usd": 0.0},
        config={"configurable": {"thread_id": f"eval-{label}"}})
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    runbooks = [item.get("id") or item["data"].splitlines()[0]
                for item in result.get("collected", [])
                if item.get("source") == "runbook"]
    return {"route": result.get("route"),
            "target_status": result.get("target_status"),
            "monitoring_calls": mcp.calls,
            "runbooks": runbooks,
            "validation_attempts": result.get("validation_attempts", 0),
            "interrupted": bool(result.get("__interrupt__")),
            "elapsed_ms": elapsed,
            "estimated_cost_usd": result.get("cost_usd", 0.0)}


async def main(output: Path):
    old_graph = old_module("agent/graph.py")
    old_fake = old_module("agent/fake_llm.py")
    rows = []
    for name, query in CASES:
        before = await run_one(old_graph["build_graph"], old_fake["FastLLM"],
                               old_fake["QualityLLM"], query, "before")
        after = await run_one(build_graph, FastLLM, QualityLLM, query, "after")
        rows.append({"case": name, "query": query,
                     "before": before, "after": after})
    payload = {"baseline_commit": BASELINE, "backend": "fake",
               "monitoring_fixture": "tests.test_improvements.StubMCP",
               "cases": rows}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(rendered)
    print(f"Saved: {output}")
    print("\nBefore/After (Fake, fixed MCP fixture)")
    for row in rows:
        before, after = row["before"], row["after"]
        print(f"{row['case']}: {before['elapsed_ms']} → {after['elapsed_ms']} ms; "
              f"monitoring {len(before['monitoring_calls'])} → "
              f"{len(after['monitoring_calls'])}; "
              f"runbooks {len(before['runbooks'])} → {len(after['runbooks'])}; "
              f"estimated USD {before['estimated_cost_usd']} → "
              f"{after['estimated_cost_usd']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=ROOT / "doc" / "evaluation_results.json")
    args = parser.parse_args()
    asyncio.run(main(args.output))
