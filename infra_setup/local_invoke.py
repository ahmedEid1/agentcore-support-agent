"""Runs main.invoke() once on this PC (same code path as the runtime, without the HTTP server).
    AWS_PROFILE=<your-profile> python local_invoke.py '{"prompt": "Can you track order ORD-001?", "customer_id": "CUST-123"}'
"""
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agent"))
import main  # noqa: E402  (module-level setup only; app.run() stays behind __main__)

print(asyncio.run(main.invoke(json.loads(sys.argv[1]))))
