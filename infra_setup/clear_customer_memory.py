"""Deletes a customer's long-term memory records (both strategies) so the memory test starts clean.
    AWS_PROFILE=<your-profile> python clear_customer_memory.py CUST-123
"""
import json
import pathlib
import sys

import boto3

state = json.loads((pathlib.Path(__file__).resolve().parent / "state.json").read_text())
actor = sys.argv[1] if len(sys.argv) > 1 else "CUST-123"
dp = boto3.client("bedrock-agentcore", region_name=state["region"])
for kind in ("facts", "preferences"):
    namespace = f"cs_agent/{actor}/{kind}"
    ids, token = [], None
    while True:
        resp = dp.list_memory_records(memoryId=state["memory_id"], namespace=namespace, maxResults=100,
                                      **({"nextToken": token} if token else {}))
        ids += [r["memoryRecordId"] for r in resp.get("memoryRecordSummaries", [])]
        token = resp.get("nextToken")
        if not token:
            break
    for record_id in ids:
        dp.delete_memory_record(memoryId=state["memory_id"], memoryRecordId=record_id)
    print(f"{namespace}: deleted {len(ids)} records")
