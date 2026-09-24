"""Lists the Gateway's MCP tools and calls tools on both targets (the Gateway uses the NONE inbound authorizer)."""
import asyncio
import json
import pathlib

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = json.loads((pathlib.Path(__file__).resolve().parent / "state.json").read_text())["gateway_url"]
CALLS = [
    ("order-tracker___get_order", {"order_id": "ORD-001"}),
    ("order-tracker___get_customer", {"customer_id": "CUST-123"}),
    ("order-tracker___get_customer_orders", {"customer_id": "CUST-123"}),
    ("refund-processor___get_return_label", {"order_id": "ORD-002"}),
]


async def main():
    async with streamable_http_client(url=URL) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print("tools:", sorted(t.name for t in tools))
            for name, args in CALLS:
                result = await session.call_tool(name, args)
                text = " ".join(c.text for c in result.content if getattr(c, "text", None))
                print(f"{name}{args}: is_error={result.is_error} {text[:220]}")


asyncio.run(main())
