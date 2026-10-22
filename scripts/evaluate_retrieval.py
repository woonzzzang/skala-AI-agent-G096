"""Compare the same fixed Runbook questions with LLM ranking and embeddings.

Run from the project root: PYTHONPATH=. .venv/bin/python scripts/evaluate_retrieval.py
Requires OPENAI_API_KEY and makes paid API calls. Prints JSON; does not write files.
"""

import json
import os
import time

from dotenv import load_dotenv

load_dotenv()
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from agent.embeddings import OpenAIEmbedder  # noqa: E402
from agent.openai_llm import OpenAIFastLLM  # noqa: E402
from agent.runbook_retrieval import RunbookRetriever  # noqa: E402


CASES = [
    ("login_latency", "auth-service 로그인 응답이 느려요",
     ["p95 latency 2.4s"], {"high-latency"}),
    ("cpu_and_timeout", "payments-service와 gateway-service CPU 급증 및 timeout 장애",
     ["CPU 91%", "upstream timeout", "packet loss"],
     {"cpu-spike", "network-timeout"}),
    ("disk_full", "gateway-service 디스크 용량이 부족해요",
     ["disk usage 96%"], {"disk-full"}),
    ("memory_leak", "auth-service 메모리가 계속 증가하다 OOM이 나요",
     ["memory 94%", "OOM"], {"memory-leak"}),
    ("db_down", "payments-service DB 연결이 안 돼요",
     ["DB connection refused"], {"db-down"}),
]


def main():
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY가 필요합니다. 실제 API 비용이 발생합니다.")
    ranker = OpenAIFastLLM(os.environ.get("OPENAI_FAST_MODEL", "gpt-4o-mini"),
                           usd_per_1k_tokens=0.00015)
    methods = {
        "llm": RunbookRetriever(),
        "embedding": RunbookRetriever(embedder=OpenAIEmbedder(os.environ.get(
            "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"))),
    }
    output = {"models": {"llm": ranker.model,
                         "embedding": methods["embedding"].embedder.model},
              "cases": [], "summary": {}}
    for name, query, alerts, expected in CASES:
        collected = [{"source": "monitoring", "data": {
            "system_name": query.split()[0], "status": "degraded", "alerts": alerts}}]
        row = {"name": name, "expected": sorted(expected)}
        for method, retriever in methods.items():
            start = time.perf_counter()
            matches, usage = retriever.search(query, collected, ranker)
            selected = {item["id"] for item in matches}
            row[method] = {"selected": sorted(selected),
                           "scores": {item["id"]: item["score"] for item in matches},
                           "latency_ms": round((time.perf_counter() - start) * 1000, 1),
                           "embedding_requests": usage.get("requests", 0),
                           "tokens": usage.get("tokens", 0),
                           "estimated_usd": usage.get("usd", 0.0),
                           "tp": len(selected & expected),
                           "fp": len(selected - expected),
                           "fn": len(expected - selected)}
        output["cases"].append(row)
    for method in methods:
        results = [row[method] for row in output["cases"]]
        tp = sum(item["tp"] for item in results)
        fp = sum(item["fp"] for item in results)
        fn = sum(item["fn"] for item in results)
        output["summary"][method] = {
            "micro_precision": round(tp / (tp + fp), 3) if tp + fp else 0,
            "micro_recall": round(tp / (tp + fn), 3) if tp + fn else 0,
            "total_latency_ms": round(sum(item["latency_ms"] for item in results), 1),
            "embedding_requests": sum(item["embedding_requests"] for item in results),
            "tokens": sum(item["tokens"] for item in results),
            "estimated_usd": round(sum(item["estimated_usd"] for item in results), 8),
        }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
