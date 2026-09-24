"""Offline checks for agent/main.py. No AWS or model calls are made.

Covers everything that can be verified without an AWS account: get_namespaces, MemoryHook
(retrieval, tool-result guard, save), the KB tool (guard, Retrieve call, joining), the loyalty
calculator (sandbox path, the live test-5 numbers, fallback, input validation) and the entrypoint
(tool list, Gateway degradation, session IDs).

    cd agent && uv run python ../offline_checks/test_main_offline.py
"""

import asyncio
import json
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from botocore.stub import Stubber  # noqa: E402
from strands import Agent  # noqa: E402
from strands.hooks import AfterInvocationEvent, MessageAddedEvent  # noqa: E402

import main  # noqa: E402


class FakeMemoryClient:
    def __init__(self, fail=False):
        self.fail, self.events, self.queries = fail, [], []

    def get_memory_strategies(self, memory_id):
        if self.fail:
            raise RuntimeError("memory not found")
        return [
            {"type": "SEMANTIC", "namespaceTemplates": ["cs_agent/{actorId}/facts"]},
            {"type": "USER_PREFERENCE", "namespaces": ["cs_agent/{actorId}/preferences"]},  # legacy field
        ]

    def retrieve_memories(self, memory_id, namespace, query, top_k):
        self.queries.append((namespace, top_k))
        store = {"cs_agent/CUST-123/facts": ["The customer's name is Jane."],
                 "cs_agent/CUST-123/preferences": ["Prefers concise responses.", "  "]}
        return [{"content": {"text": t}} for t in store.get(namespace, [])]

    def create_event(self, memory_id, actor_id, session_id, messages):
        self.events.append((actor_id, session_id, messages))


def check(name, condition):
    assert condition, name
    print("  ok -", name)


# ── get_namespaces ────────────────────────────────────────────────────────────
print("get_namespaces")
check("namespaceTemplates and legacy namespaces", main.get_namespaces(FakeMemoryClient(), "m") ==
      {"SEMANTIC": "cs_agent/{actorId}/facts", "USER_PREFERENCE": "cs_agent/{actorId}/preferences"})

# ── MemoryHook ────────────────────────────────────────────────────────────────
print("MemoryHook")
mem = FakeMemoryClient()
hook = main.MemoryHook("CUST-123", "s-B", mem, "mem-id")
agent = Agent(model=main.model, hooks=[hook], callback_handler=None)


def add(message):
    agent.messages.append(message)
    agent.hooks.invoke_callbacks(MessageAddedEvent(agent=agent, message=message))


add({"role": "user", "content": [{"text": "Do you remember my name and communication preference?"}]})
text = agent.messages[-1]["content"][0]["text"]
check("context prepended with strategy tags", text == (
    "Customer Context:\n[SEMANTIC] The customer's name is Jane.\n[USER_PREFERENCE] Prefers concise responses."
    "\n\nDo you remember my name and communication preference?"))
check("namespaces resolved for the actor, top_k=5",
      mem.queries == [("cs_agent/CUST-123/facts", 5), ("cs_agent/CUST-123/preferences", 5)])
add({"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "x", "input": {}}}]})
add({"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "status": "success", "content": [{"text": "{}"}]}}]})
add({"role": "assistant", "content": [{"text": "Yes, Jane. You prefer concise answers."}]})
check("tool results and assistant messages trigger no retrieval", len(mem.queries) == 2)
asyncio.run(agent.hooks.invoke_callbacks_async(AfterInvocationEvent(agent=agent)))
check("saves the original query and the final answer", mem.events == [("CUST-123", "s-B", [
    ("Do you remember my name and communication preference?", "USER"),
    ("Yes, Jane. You prefer concise answers.", "ASSISTANT")])])
check("memory unavailable does not break the hook",
      main.MemoryHook("CUST-123", "s", FakeMemoryClient(fail=True), "bad").namespaces == {})

# ── search_knowledge_base ─────────────────────────────────────────────────────
print("search_knowledge_base")
main.KB_ID = "<kbid>"  # a placeholder, as in a fresh checkout
check("guard when KB_ID is a placeholder", main.search_knowledge_base("anything") == "Knowledge base not configured.")
main.KB_ID = "ABCDEFGHIJ"
request = {"knowledgeBaseId": "ABCDEFGHIJ", "retrievalQuery": {"text": "Platinum benefits"}}
with Stubber(main._bedrock_runtime) as stub:
    stub.add_response("retrieve", {"retrievalResults": [
        {"content": {"text": "Platinum: Free same-day shipping, 15% discount, priority customer support"}},
        {"content": {"text": "Platinum: 5000 or more points"}}]}, request)
    stub.add_response("retrieve", {"retrievalResults": []}, request)
    joined = main.search_knowledge_base("Platinum benefits")
    empty = main.search_knowledge_base("Platinum benefits")
check("chunks joined with ---", joined == "Platinum: Free same-day shipping, 15% discount, priority customer support"
      "\n---\nPlatinum: 5000 or more points")
check("empty result message", empty.startswith("No relevant information found"))
check("docstring says when to call it", "loyalty program" in main.search_knowledge_base.tool_spec["description"])

# ── calculate_loyalty_discount ────────────────────────────────────────────────
print("calculate_loyalty_discount")
sent = []


@contextmanager
def local_sandbox(region):
    """Stands in for code_session: runs the tool-generated script in a separate Python process
    and wraps stdout in the InvokeCodeInterpreter result shape."""
    class Client:
        def invoke(self, method, params):
            sent.append((method, params))
            out = subprocess.run([sys.executable, "-c", params["code"]], capture_output=True, text=True, check=True).stdout
            return {"stream": [{"result": {"content": [{"type": "text", "text": out}],
                                           "structuredContent": {"stdout": out, "stderr": "", "exitCode": 0},
                                           "isError": False}}]}
    yield Client()


@contextmanager
def broken_sandbox(region):
    raise RuntimeError("AccessDeniedException")
    yield


main.code_session = local_sandbox
gold = json.loads(main.calculate_loyalty_discount(4250, "Gold", 150.0, "standard"))
print("   Test 5:", gold)
check("executeCode, python, clearContext=True",
      sent[0][0] == "executeCode" and sent[0][1]["language"] == "python" and sent[0][1]["clearContext"] is True)
check("Test 5 numbers (4000 pts, 10%, $99.00, 250 left)",
      (gold["points_redeemed"], gold["tier_discount_pct"], gold["final_total"], gold["remaining_points"]) == (4000, 10.0, 99.0, 250))
check("savings, points earned, new balance",
      (gold["total_savings"], gold["points_earned"], gold["new_points_balance"]) == (51.0, 99, 349))
plat = json.loads(main.calculate_loyalty_discount(6000, "platinum", 80, "device"))
check("50% cap and device earn rate", (plat["points_redeemed"], plat["final_total"], plat["points_earned"], plat["remaining_points"])
      == (4000, 34.0, 68, 2000))
check("below the 500-point minimum", json.loads(main.calculate_loyalty_discount(499, "Silver", 20, "fresh"))["points_redeemed"] == 0)

main.code_session = broken_sandbox
fb = json.loads(main.calculate_loyalty_discount(4250, "Gold", 150.0))
check("fallback: tier-only discount with all breakdown fields",
      (fb["points_redeemed"], fb["tier_discount_pct"], fb["final_total"], fb["remaining_points"], fb["calculated_by"])
      == (0, 10.0, 135.0, 4250, "fallback"))
check("invalid tier rejected before any code is built",
      "error" in json.loads(main.calculate_loyalty_discount(100, 'Gold"); import os #', 50)))
check("invalid total rejected", "error" in json.loads(main.calculate_loyalty_discount(100, "Gold", -5)))

# ── invoke() entrypoint ───────────────────────────────────────────────────────
print("invoke")
main.memory_client = FakeMemoryClient()
captured = {}


class StubAgent:
    def __init__(self, model, tools, hooks, system_prompt):
        captured.update(tools=tools, hooks=hooks, system_prompt=system_prompt)

    def __call__(self, prompt):
        return SimpleNamespace(message={"role": "assistant", "content": [{"text": f"answer to: {prompt}"}]})


class FakeMCPClient:
    fail = False

    def __init__(self, transport_factory):
        self.transport_factory = transport_factory

    def __enter__(self):
        if FakeMCPClient.fail:
            raise ConnectionError("gateway unreachable")
        return self

    def __exit__(self, *exc):
        return False

    def list_tools_sync(self):
        return ["order_tracker___get_order", "refund_processor___initiate_refund"]


main.Agent, main.MCPClient = StubAgent, FakeMCPClient
out = asyncio.run(main.invoke({"prompt": "Can you track order ORD-001?", "customer_id": "CUST-123", "session_id": "t1"}))
check("returns the first text block", out == "answer to: Can you track order ORD-001?")
names = [getattr(t, "tool_name", t) for t in captured["tools"]]
check("KB, loyalty, browser + Gateway tools", names == ["search_knowledge_base", "calculate_loyalty_discount", "browser",
                                                       "order_tracker___get_order", "refund_processor___initiate_refund"])
check("customer ID in the system prompt", captured["system_prompt"].endswith("The current customer's ID is CUST-123."))
check("memory hook scoped to customer and session",
      (captured["hooks"][0].actor_id, captured["hooks"][0].session_id) == ("CUST-123", "t1"))

FakeMCPClient.fail = True
out = asyncio.run(main.invoke({"prompt": "What are the Platinum benefits?", "customer_id": "CUST-123"}))
check("Gateway down: still answers with the 3 local tools", out.startswith("answer to:") and len(captured["tools"]) == 3)
uuid.UUID(captured["hooks"][0].session_id)
check("session_id generated when missing", True)
check("missing prompt handled", asyncio.run(main.invoke({})).startswith("Please send"))

print("\nAll offline checks passed.")
