"""Minimal example agent on Husk's own engine, using the @husk.node decorators.

Run:
    uv run --group examples python examples/husk_thread.py

The graph has two nodes (planner -> answerer). Each `agent.invoke(...)`:
- creates a thread_id and emits OTel spans per node (so the run appears in Husk),
- writes a state snapshot after every node to ~/.husk/husk-demo.sqlite.

The backend's /api/replay endpoint re-imports THIS file and resumes the agent
from a snapshot (`agent.replay_from`) with a modified state: resuming at a node
re-runs only that node and its successors, so the upstream nodes emit no spans
and consume no tokens. The Studio's "Run from here" button wires that up.

All of the OTel/snapshot/replay wiring lives in `HuskAgent`; here we just write
plain `(state) -> delta` functions and decorate them.
"""

from __future__ import annotations

import logging
import os
import time

from opentelemetry import trace

from husk_studio_shared.agent import HuskAgent

log = logging.getLogger(__name__)

# Assigned to a module global so the backend can resolve husk.graph_module to it.
agent = HuskAgent("husk-demo")


@agent.node
def planner(state: dict) -> dict:
    tracer = trace.get_tracer("husk-demo")
    with tracer.start_as_current_span("planner") as span:
        span.set_attribute("gen_ai.system", "openai")
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", "gpt-4o-mini")
        span.set_attribute("gen_ai.usage.input_tokens", 24)
        span.set_attribute("gen_ai.usage.output_tokens", 32)
        span.add_event("gen_ai.user.message", {"content": f"Plan for topic: {state.get('topic', '?')}"})
        plan = f"1. Research {state.get('topic')}\n2. Summarize\n3. Format final answer"
        span.add_event("gen_ai.choice", {"finish_reason": "stop", "message.content": plan})
        time.sleep(0.08)
        return {"plan": plan}


@agent.node
def answerer(state: dict) -> dict:
    tracer = trace.get_tracer("husk-demo")
    with tracer.start_as_current_span("answerer") as span:
        span.set_attribute("gen_ai.system", "openai")
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", "gpt-4o")
        span.set_attribute("gen_ai.usage.input_tokens", 64)
        span.set_attribute("gen_ai.usage.output_tokens", 96)
        span.add_event(
            "gen_ai.user.message",
            {"content": f"plan={state.get('plan')}\ntopic={state.get('topic')}"},
        )
        topic = state.get("topic", "")
        plan = state.get("plan", "")
        if "rome" in topic.lower():
            answer = "Rome is the capital of Italy with ~2.87M people."
        elif "tokyo" in topic.lower():
            answer = "Tokyo is the capital of Japan with ~14M people."
        else:
            answer = f"{topic.capitalize()} — answered using plan: {plan[:40]}…"
        span.add_event("gen_ai.choice", {"finish_reason": "stop", "message.content": answer})
        time.sleep(0.12)
        return {"answer": answer}


def main() -> None:
    result = agent.invoke({"topic": "Rome"})
    log.info(f"Thread:  {result['thread_id']}")
    log.info(f"State:   {result['state']}")
    base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:7654").rstrip("/")
    log.info(f"Open {base}/runs to see the run, then 'Modify and replay'.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
