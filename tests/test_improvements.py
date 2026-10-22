"""Deterministic regression checks; no network or paid model calls."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from agent.answer_validation import grounded_issues, validate_report
from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph
from agent.runbook_retrieval import RunbookRetriever
from agent.system_registry import SystemRegistry


class StubMCP:
    def __init__(self, fail_target=None):
        self.fail_target = fail_target
        self.calls = []

    async def find_prompt(self, name):
        return "ops"

    async def get_prompt(self, name, arguments, server):
        return "모니터링 후 관련 런북을 조회하세요."

    async def call_tool(self, name, arguments):
        target = arguments["system_name"]
        self.calls.append(target)
        if target == self.fail_target:
            raise RuntimeError("simulated monitoring failure")
        alerts = {
            "auth-service": ["p95 latency 2.4s"],
            "payments-service": ["CPU 91%", "responding slow"],
            "gateway-service": ["upstream timeout", "packet loss"],
        }.get(target, [])
        return {"system_name": target, "status": "degraded",
                "cpu_percent": 91.0, "memory_percent": 55.0,
                "alerts": alerts, "last_checked": "fixed-time"}

    async def find_resource_template(self, name):
        return "ops", "runbook://{incident_type}"

    async def read_resource(self, uri, server):
        doc_id = uri.rsplit("/", 1)[-1]
        return (Path(__file__).resolve().parents[1] / "data" / "runbooks" /
                f"{doc_id}.md").read_text(encoding="utf-8")


class RejectingQuality(QualityLLM):
    def judge(self, draft, evidence):
        return {"passed": False, "issues": ["반려"], "feedback": "반려"}, {"usd": 0}


class StubEmbedder:
    """Tiny deterministic vectors; tests retrieval wiring, not model quality."""

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            lowered = text.lower()
            vector = [float("cpu" in lowered),
                      float("timeout" in lowered or "packet loss" in lowered),
                      float("disk" in lowered)]
            vectors.append(vector if any(vector) else [0.0, 0.0, 1.0])
        return vectors, {"tokens": len(texts), "usd": 0.0, "requests": 1}


def make_graph(mcp=None, quality=None, retriever=None):
    mcp = mcp or StubMCP()
    deps = SupervisorDeps(FastLLM("fake-fast", 0.00015),
                          quality or QualityLLM("fake-quality", 0.005),
                          mcp, None, "v2", retriever)
    return build_graph(deps, InMemorySaver()), mcp


def invoke(graph, query, thread):
    return asyncio.run(graph.ainvoke(
        {"query": query, "thread_id": thread, "history": [], "events": [],
         "cost_usd": 0.0}, config={"configurable": {"thread_id": thread}}))


class ImprovementTests(unittest.TestCase):
    def test_embedding_search_caches_documents_and_handles_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "cpu.md").write_text("CPU spike", encoding="utf-8")
            (directory / "network.md").write_text("network timeout", encoding="utf-8")
            embedder = StubEmbedder()
            retriever = RunbookRetriever(directory=directory, embedder=embedder)
            first, usage = retriever.search("CPU timeout", [], None)
            self.assertEqual({item["id"] for item in first}, {"cpu", "network"})
            self.assertEqual(usage["requests"], 2)  # 문서 배치 + 질문
            self.assertEqual(len(embedder.calls[0]), 2)

            again, usage = retriever.search("CPU timeout", [], None)
            self.assertEqual({item["id"] for item in again}, {"cpu", "network"})
            self.assertEqual(usage["requests"], 1)  # 질문만 새로 임베딩

            (directory / "cpu.md").write_text("disk full", encoding="utf-8")
            changed, usage = retriever.search("CPU timeout", [], None)
            self.assertEqual([item["id"] for item in changed], ["network"])
            self.assertEqual(usage["requests"], 2)
            self.assertEqual(embedder.calls[-2], ["disk full"])

    def test_graph_uses_embedding_matches_for_mcp_resource_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "cpu-spike.md").write_text("CPU spike", encoding="utf-8")
            (directory / "network-timeout.md").write_text("network timeout", encoding="utf-8")
            retriever = RunbookRetriever(directory=directory, embedder=StubEmbedder())
            graph, _ = make_graph(retriever=retriever)
            result = invoke(graph, "payments-service와 gateway-service CPU 급증 및 timeout 장애",
                            "embedding-graph-test")
            self.assertEqual({item["id"] for item in result["runbook_matches"]},
                             {"cpu-spike", "network-timeout"})
            self.assertTrue({"cpu-spike", "network-timeout"}.issubset(
                {item["id"] for item in result["collected"]
                 if item["source"] == "runbook"}))
            event = next(item for item in result["events"]
                         if item["type"] == "RunbookSearch")
            self.assertEqual(event["method"], "embedding")
            self.assertEqual(event["embedding_requests"], 2)

    def test_registry_and_natural_language(self):
        fast = FastLLM("fake", 0)
        catalog = SystemRegistry().to_prompt_catalog()
        self.assertEqual(fast.resolve_target("로그인 응답이 느려요", catalog)[0]["target_systems"],
                         ["auth-service"])
        self.assertEqual(fast.resolve_target("billing-service 장애", catalog)[0]["status"],
                         "unknown")

    def test_interrupt_resume_preserves_original_query(self):
        graph, mcp = make_graph()
        first = invoke(graph, "어떤 서비스가 장애인지 확인해줘", "clarify-test")
        self.assertTrue(first["__interrupt__"])
        self.assertEqual(mcp.calls, [])
        second = asyncio.run(graph.ainvoke(
            Command(resume="auth-service"),
            config={"configurable": {"thread_id": "clarify-test"}}))
        self.assertEqual(second["query"], "어떤 서비스가 장애인지 확인해줘")
        self.assertEqual(second["target_systems"], ["auth-service"])
        self.assertIn("auth-service", mcp.calls)

    def test_sqlite_checkpoint_survives_graph_rebuild(self):
        async def scenario(path):
            mcp = StubMCP()
            deps = SupervisorDeps(FastLLM("fake-fast", 0),
                                  QualityLLM("fake-quality", 0), mcp, None, "v2")
            config = {"configurable": {"thread_id": "persist-test"}}
            async with AsyncSqliteSaver.from_conn_string(path) as saver:
                graph = build_graph(deps, saver)
                paused = await graph.ainvoke(
                    {"query": "어떤 서비스가 장애인지 확인해줘",
                     "thread_id": "session-1", "history": [], "events": [],
                     "cost_usd": 0.0}, config=config)
                self.assertTrue(paused["__interrupt__"])
            async with AsyncSqliteSaver.from_conn_string(path) as saver:
                graph = build_graph(deps, saver)
                completed = await graph.ainvoke(Command(resume="auth-service"),
                                               config=config)
                self.assertEqual(completed["target_systems"], ["auth-service"])
                self.assertIn("auth-service", mcp.calls)

        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(scenario(str(Path(folder) / "checkpoint.sqlite")))

    def test_multi_runbook_retrieval(self):
        graph, _ = make_graph()
        result = invoke(graph, "payments-service와 gateway-service CPU 급증 및 timeout 장애",
                        "multi-test")
        self.assertEqual(result["target_systems"],
                         ["payments-service", "gateway-service"])
        self.assertGreaterEqual(len(result["runbook_matches"]), 2)
        self.assertTrue({"cpu-spike", "network-timeout"}.issubset(
            {item["id"] for item in result["runbook_matches"]}))

    def test_partial_failure_keeps_successful_evidence(self):
        graph, _ = make_graph(StubMCP(fail_target="payments-service"))
        result = invoke(graph, "payments-service와 gateway-service CPU 급증 및 timeout 장애",
                        "failure-test")
        self.assertTrue(any(item["data"]["system_name"] == "gateway-service"
                            for item in result["collected"]
                            if item["source"] == "monitoring"))
        self.assertTrue(any(event["type"] == "TaskFailed" for event in result["events"]))
        self.assertIn("payments-service", result["final"])
        self.assertIn("실패", result["final"])

    def test_never_force_pass(self):
        graph, _ = make_graph(quality=RejectingQuality("reject", 0))
        result = invoke(graph, "auth-service 로그인 응답 지연 장애", "reject-test")
        self.assertEqual(result["validation_attempts"], 3)
        self.assertFalse(result["validation_passed"])
        self.assertTrue(result["final"].startswith("검증 미통과"))

    def test_deterministic_numeric_and_source_checks(self):
        evidence = {"target_systems": ["auth-service"],
                    "monitoring": [{"system_name": "auth-service",
                                    "cpu_percent": 80.0}],
                    "runbooks": [{"id": "high-latency", "text": "# latency"}],
                    "failed_tasks": []}
        draft = ("확인된 사실 auth-service CPU 99%\n원인 추정\n권장 조치\n"
                 "진단 한계")
        issues = grounded_issues(draft, evidence)
        self.assertTrue(any("99%" in issue for issue in issues))
        self.assertTrue(any("high-latency" in issue for issue in issues))
        verdict, _ = validate_report(draft, evidence, QualityLLM("fake", 0))
        self.assertFalse(verdict["passed"])


if __name__ == "__main__":
    unittest.main()
