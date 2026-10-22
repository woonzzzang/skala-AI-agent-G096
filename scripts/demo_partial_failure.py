"""Run the real chat UI with one deliberately failed Monitoring lookup.

For a presentation/demo only: payments-service fails, while other systems use
the normal MCP server and monitoring.json. This does not change monitoring data.
"""

import os
from contextlib import asynccontextmanager

import uvicorn

os.environ["LLM_BACKEND"] = "fake"
os.environ["RUNBOOK_SEARCH_MODE"] = "llm"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from server import app  # noqa: E402  (environment is set before server import)


normal_lifespan = app.router.lifespan_context


@asynccontextmanager
async def demo_lifespan(application):
    async with normal_lifespan(application):
        mcp = application.state.deps.mcp
        normal_call_tool = mcp.call_tool

        async def fail_one_monitoring_call(name, arguments):
            if (name == "get_monitoring_status"
                    and arguments.get("system_name") == "payments-service"):
                raise RuntimeError("DEMO: payments-service Monitoring 조회 실패")
            return await normal_call_tool(name, arguments)

        mcp.call_tool = fail_one_monitoring_call
        yield


app.router.lifespan_context = demo_lifespan


if __name__ == "__main__":
    print("DEMO ONLY: payments-service Monitoring 조회만 의도적으로 실패시킵니다.")
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")
