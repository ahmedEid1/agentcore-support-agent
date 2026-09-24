"""
AI customer-support agent on Amazon Bedrock AgentCore Runtime (Strands Agents SDK).

One agent, five capabilities:
  - Gateway tools over MCP: order tracking (API Gateway target) and refunds (Lambda target)
  - search_knowledge_base: RAG over a Bedrock Knowledge Base (product catalog and policies)
  - calculate_loyalty_discount: exact arithmetic in the AgentCore Code Interpreter sandbox
  - browser: live web pages through the AgentCore Browser
  - long-term memory: AgentCore Memory (customer facts and preferences), recalled and saved by a hook

Run locally:      uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
Deploy:           agentcore configure ... && agentcore deploy   (see the README)
Invoke deployed:  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from contextlib import ExitStack

import boto3
from bedrock_agentcore.memory import MemoryClient
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.tools.code_interpreter_client import code_session
from mcp.client.streamable_http import streamable_http_client
from strands import Agent, tool
from strands.hooks import AfterInvocationEvent, HookProvider, HookRegistry, MessageAddedEvent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from strands_tools.browser import AgentCoreBrowser

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")
logger.setLevel(logging.INFO)  # this agent's own lines (e.g. memory recall) at INFO; libraries stay at WARNING

app = BedrockAgentCoreApp()  # the ASGI app AgentCore Runtime serves (/invocations, /ping)

# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── Configuration ─────────────────────────────────────────────────────────────
# The resource IDs are printed by infra_setup/build_backend.py. Set them as environment variables
# (locally or on the runtime) or replace the placeholders. The Gateway uses the NONE inbound
# authorizer, so the MCP connection below is unsigned.
REGION      = os.environ.get("AWS_REGION", "us-east-1")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "<gateway-url>")  # https://<alias>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
KB_ID       = os.environ.get("KB_ID", "<kb-id>")              # 10-character Knowledge Base ID
MEMORY_ID   = os.environ.get("MEMORY_ID", "<memory-id>")      # AgentCore Memory ID
MODEL_ID    = "global.amazon.nova-2-lite-v1:0"


def _configured(value: str) -> bool:
    """False for an empty value or a leftover "<placeholder>"."""
    return bool(value) and not value.startswith("<")


model = BedrockModel(model_id=MODEL_ID)
memory_client = MemoryClient(region_name=REGION)
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ── Long-term memory ──────────────────────────────────────────────────────────

def get_namespaces(mem_client: MemoryClient, memory_id: str) -> dict:
    """Return a dict mapping strategy type → namespace template string,
    e.g. {"SEMANTIC": "cs_agent/{actorId}/facts", "USER_PREFERENCE": "cs_agent/{actorId}/preferences"}."""
    namespaces = {}
    for strategy in mem_client.get_memory_strategies(memory_id):
        strategy_type = strategy.get("type") or strategy.get("memoryStrategyType")
        templates = strategy.get("namespaceTemplates") or strategy.get("namespaces") or []
        if strategy_type and templates:
            namespaces[strategy_type] = templates[0]
    return namespaces


class MemoryHook(HookProvider):
    """Long-term memory: recall customer context before each user message, save each completed turn."""

    CONTEXT_HEADER = "Customer Context:\n"

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_id = memory_id
        self.memory_client = memory_client
        self.original_query = None  # the user's text before the context block is prepended
        try:
            self.namespaces = get_namespaces(memory_client, memory_id)
        except Exception as e:  # memory unavailable: answer without it rather than fail the turn
            logger.warning("Memory namespaces unavailable, continuing without memory: %s", e)
            self.namespaces = {}

    @staticmethod
    def _is_plain_text(message: dict) -> bool:
        """True for a message whose first block is text (tool results and tool calls are not)."""
        content = message.get("content") or []
        return bool(content) and isinstance(content[0], dict) and "text" in content[0]

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        messages = event.agent.messages
        if not messages:
            return
        message = messages[-1]
        if message.get("role") != "user" or not self._is_plain_text(message):
            return  # assistant turns and toolResult messages are not customer queries

        user_query = message["content"][0]["text"]
        self.original_query = user_query
        memories = []
        for strategy_type, namespace in self.namespaces.items():
            try:
                records = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=namespace.replace("{actorId}", self.actor_id),
                    query=user_query,
                    top_k=5,
                )
            except Exception as e:
                logger.warning("Memory retrieval failed for %s: %s", strategy_type, e)
                continue
            for record in records:
                text = (record.get("content") or {}).get("text", "").strip()
                if text:
                    memories.append(f"[{strategy_type}] {text}")

        # Counts only: the memories themselves are customer data and stay out of the logs
        counts = {t: sum(m.startswith(f"[{t}]") for m in memories) for t in self.namespaces}
        logger.info("Memory recall for %s (session %s): %s", self.actor_id, self.session_id, counts)
        if memories:
            message["content"][0]["text"] = self.CONTEXT_HEADER + "\n".join(memories) + f"\n\n{user_query}"

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        customer_query = agent_response = None
        for message in reversed(event.agent.messages):
            if not self._is_plain_text(message):
                continue
            text = message["content"][0]["text"]
            if message.get("role") == "assistant" and agent_response is None:
                agent_response = text
            elif message.get("role") == "user":
                customer_query = text
                break

        # Persist what the customer actually said, not the retrieved memories prepended to it.
        if customer_query and customer_query.startswith(self.CONTEXT_HEADER) and self.original_query:
            customer_query = self.original_query
        if not (customer_query and agent_response):
            return
        try:
            self.memory_client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")],
            )
        except Exception as e:
            logger.warning("Saving the interaction to memory failed: %s", e)

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── Knowledge base (RAG) ──────────────────────────────────────────────────────
# The docstring is the tool description the model reads to decide when to call it.

@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not _configured(KB_ID):
        return "Knowledge base not configured."
    try:
        resp = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
        )
    except Exception as e:
        logger.warning("Knowledge base search failed: %s", e)
        return "The knowledge base is temporarily unavailable; please try again shortly."

    results = resp.get("retrievalResults", [])
    chunks = [r["content"]["text"] for r in results if r.get("content", {}).get("text")]
    if not chunks:
        return f"No relevant information found in the knowledge base for: {query}"
    return "\n---\n".join(chunks)


# ── Loyalty discount (Code Interpreter) ───────────────────────────────────────

EARN_RATES = {"standard": 1, "device": 2, "fresh": 5}
TIER_RATES = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}

@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    # Validate and normalise the model's arguments before they go anywhere near a code string.
    tier_name = str(tier).strip().capitalize()
    category = str(product_category).strip().lower()
    if tier_name not in TIER_RATES:
        return json.dumps({"error": f"Unknown tier '{tier}'. Use Silver, Gold or Platinum."})
    if category not in EARN_RATES:
        return json.dumps({"error": f"Unknown product category '{product_category}'. Use standard, device or fresh."})
    try:
        points = int(loyalty_points)
        total = round(float(order_total), 2)
    except (TypeError, ValueError):
        return json.dumps({"error": "loyalty_points must be an integer and order_total a number."})
    if points < 0 or total <= 0:
        return json.dumps({"error": "loyalty_points must be >= 0 and order_total > 0."})

    # Self-contained script: every rule and input is embedded, because the sandbox has no network
    # access and no state from earlier calls. ({{ }} are literal braces in the generated Python.)
    code = f"""
import json, math

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}          # points per $1 spent
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

loyalty_points = {points}
tier = {json.dumps(tier_name)}
order_total = {total!r}
product_category = {json.dumps(category)}

# 1. Points: 100 points = $1, redeemed in blocks of 500 (the minimum), capped at 50% of the order
max_points_by_cap = int(order_total * 0.5 * 100) // 500 * 500
points_redeemed = min(loyalty_points // 500 * 500, max_points_by_cap)
points_discount = round(points_redeemed / 100, 2)

# 2. Tier discount, applied to the subtotal after points
subtotal_after_points = round(order_total - points_discount, 2)
tier_discount_pct = round(tier_rates[tier] * 100, 2)
tier_discount = round(subtotal_after_points * tier_rates[tier], 2)

# 3. Totals and points
final_total = round(subtotal_after_points - tier_discount, 2)
total_savings = round(points_discount + tier_discount, 2)
points_earned = math.floor(final_total * earn_rates[product_category])
remaining_points = loyalty_points - points_redeemed

print(json.dumps({{
    "tier": tier,
    "order_total": order_total,
    "product_category": product_category,
    "points_redeemed": points_redeemed,
    "points_discount": points_discount,
    "subtotal_after_points": subtotal_after_points,
    "tier_discount_pct": tier_discount_pct,
    "tier_discount": tier_discount,
    "final_total": final_total,
    "total_savings": total_savings,
    "points_earned": points_earned,
    "remaining_points": remaining_points,
    "new_points_balance": remaining_points + points_earned,
}}))
"""

    try:
        with code_session(REGION) as code_client:
            response = code_client.invoke("executeCode", {
                "code": code,
                "language": "python",
                "clearContext": True,  # fresh interpreter: no state leaks between customers
            })
            for event in response["stream"]:
                result = event["result"]
                if result.get("isError"):
                    raise RuntimeError(f"sandbox error: {json.dumps(result)[:300]}")
                stdout = (result.get("structuredContent") or {}).get("stdout") or "".join(
                    block.get("text", "") for block in result.get("content", []) if block.get("type") == "text"
                )
                try:
                    breakdown = json.loads(stdout.strip().splitlines()[-1])
                except (ValueError, IndexError):
                    return json.dumps(result)  # unexpected shape: pass the raw result through
                breakdown["calculated_by"] = "AgentCore Code Interpreter"
                return json.dumps(breakdown)
        raise RuntimeError("the sandbox returned no result")

    except Exception as e:
        # Fallback: tier discount only (no points redemption), clearly flagged as partial.
        logger.warning("Code Interpreter unavailable, using tier-only fallback: %s", e)
        rate = TIER_RATES[tier_name]
        tier_discount = round(total * rate, 2)
        return json.dumps({
            "tier": tier_name,
            "order_total": total,
            "points_redeemed": 0,
            "tier_discount_pct": round(rate * 100, 2),
            "tier_discount": tier_discount,
            "final_total": round(total - tier_discount, 2),
            "remaining_points": points,
            "calculated_by": "fallback",
            "note": "The calculator sandbox is unavailable, so only the tier discount was applied. "
                    "Points were not redeemed; the customer can redeem them once the calculator is back.",
        })


# ── Agent entrypoint ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the customer support assistant of an online store. Be friendly, accurate and
concise, and base every fact on your tools; never invent order, refund or policy details.

Tools and when to use them:
- Order tools (get_order, get_customer_orders, get_customer): order status, tracking number,
  carrier and delivery dates, the customer's orders, and their profile (name, tier, points).
- Refund tools (initiate_refund, check_refund_status, get_return_label): returns and refunds.
  Before initiating a refund, look up the order to confirm it belongs to the current customer
  and to get the amount, then call initiate_refund with the order_id, the reason and the amount.
  Report the refund ID, status and timeline, and offer a prepaid return label.
- search_knowledge_base: product specifications, return and refund policies, warranties,
  the loyalty programme (tiers, benefits, earning and redeeming points) and order status meanings.
- calculate_loyalty_discount: ALWAYS use it for discounts, points redemption or final prices;
  never do this arithmetic yourself. If the customer doesn't give their tier or points, get them
  with get_customer. Report the points redeemed, the tier discount percentage, the final total
  and the remaining points. If the result says it is a fallback, explain that points were not redeemed.
- browser: only when the customer asks about a specific web page or needs live information from
  the web. Report what you found on the page.

Security: only act on orders that belong to the current customer; if an order belongs to someone
else, say you can't help with it.

A message may start with a "Customer Context:" block recalled from earlier conversations
(name, preferences, past issues). Use it naturally to personalise the answer and follow the
customer's stated preferences (e.g. concise answers), without mentioning the block itself."""


def _response_text(result) -> str:
    """Text of the first text content block of the agent's final message."""
    for block in result.message.get("content", []):
        if "text" in block:
            return block["text"]
    return str(result)


@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    payload = payload or {}
    user_input = str(payload.get("prompt", "")).strip()
    if not user_input:
        return "Please send your question in the 'prompt' field."
    actor_id = str(payload.get("customer_id") or "anonymous")
    session_id = str(payload.get("session_id") or uuid.uuid4())

    try:
        memory_hook = MemoryHook(actor_id, session_id, memory_client, MEMORY_ID)
        agent_core_browser = AgentCoreBrowser(region=REGION)
        tools = [search_knowledge_base, calculate_loyalty_discount, agent_core_browser.browser]

        with ExitStack() as stack:
            # The MCP session must stay open while the agent runs, so the agent is built inside it.
            try:
                mcp_client = MCPClient(lambda: streamable_http_client(url=GATEWAY_URL))
                stack.enter_context(mcp_client)
                gateway_tools = mcp_client.list_tools_sync()
                tools.extend(gateway_tools)
            except Exception as e:  # Gateway down or misconfigured: keep the other capabilities working
                logger.warning("Gateway tools unavailable, continuing without them: %s", e)

            agent = Agent(
                model=model,
                tools=tools,
                hooks=[memory_hook],
                system_prompt=SYSTEM_PROMPT + f"\n\nThe current customer's ID is {actor_id}.",
            )
            result = agent(user_input)
            return _response_text(result)

    except Exception as e:
        logger.exception("Invocation failed")
        return f"Sorry, I ran into a technical problem ({type(e).__name__}). Please try again in a moment."


def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str, help='JSON, e.g. \'{"prompt": "Hello", "customer_id": "CUST-123"}\'')
    args = parser.parse_args()
    print(asyncio.run(invoke(json.loads(args.payload))))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main()     # local test: one request from the command line
    else:
        app.run()  # AgentCore Runtime: serve HTTP on port 8080
