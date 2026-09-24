"""Runs the project's 6 test scenarios against the deployed runtime with `agentcore invoke`.

Saves each terminal transcript to ../test_outputs/, then the runtime's CloudWatch log lines for the
test window (they show which tools the agent called) to ../test_outputs/runtime_logs.txt.
    AWS_PROFILE=<your-profile> python run_tests.py            # all tests
    AWS_PROFILE=<your-profile> python run_tests.py 3 6        # only tests 3 and 6
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import uuid

import boto3
import yaml

HERE = pathlib.Path(__file__).resolve().parent
AGENT_DIR = HERE.parent / "agent"
OUT = HERE.parent / "test_outputs"
OUT.mkdir(exist_ok=True)
AGENTCORE = AGENT_DIR / ".venv" / ("Scripts/agentcore.exe" if os.name == "nt" else "bin/agentcore")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CUSTOMER = "CUST-123"

TESTS = [
    (1, "order_tracking", [("t1", "Can you track order ORD-001?")]),
    (2, "refund", [("t2", "I want to return my Kindle Paperwhite (ORD-002). Please initiate a refund.")]),
    (3, "rag_platinum", [("t3", "What are the benefits of the Platinum loyalty tier?")]),
    (4, "memory", [("s-A", "Hi, I am Jane. I prefer concise responses."),
                   ("s-B", "Do you remember my name and communication preference?")]),
    (5, "loyalty_discount", [("t5", "I am a Gold member with 4250 points. Calculate my discount on a $150 standard order.")]),
    (6, "browser", [("t6", "Go to https://www.udacity.com and tell me the page title.")]),
]
MEMORY_WAIT = 90  # seconds between the two memory sessions: extraction finishes in ~20 s,
                  # but the semantic index can lag a little behind it


def invoke(prompt, memory_session):
    payload = {"prompt": prompt, "customer_id": CUSTOMER, "session_id": memory_session}
    runtime_session = f"{memory_session}-{uuid.uuid4()}"  # runtime session IDs need 33+ characters
    cmd = [str(AGENTCORE), "invoke", json.dumps(payload), "--session-id", runtime_session]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "AGENTCORE_SUPPRESS_RECOMMENDATION": "1", "COLUMNS": "160"}
    done = subprocess.run(cmd, cwd=AGENT_DIR, env=env, capture_output=True, text=True, encoding="utf-8")
    shown = "agentcore invoke '" + json.dumps(payload) + f"' --session-id {runtime_session}"
    return f"$ {shown}\n{ANSI.sub('', done.stdout + done.stderr).rstrip()}\n(exit code {done.returncode})\n"


def runtime_log_lines(start_ms):
    config = yaml.safe_load((AGENT_DIR / ".bedrock_agentcore.yaml").read_text())
    agent_id = config["agents"][config["default_agent"]]["bedrock_agentcore"]["agent_id"]
    logs = boto3.client("logs", region_name="us-east-1")
    group = f"/aws/bedrock-agentcore/runtimes/{agent_id}-DEFAULT"
    lines, token = [], None
    while True:
        kwargs = {"logGroupName": group, "startTime": start_ms, **({"nextToken": token} if token else {})}
        resp = logs.filter_log_events(**kwargs)
        lines += [e["message"].rstrip() for e in resp["events"]]
        token = resp.get("nextToken")
        if not token:
            return group, lines


if __name__ == "__main__":
    wanted = {int(a) for a in sys.argv[1:]} or {n for n, *_ in TESTS}
    start_ms = int(time.time() * 1000) - 5000
    for number, name, turns in TESTS:
        if number not in wanted:
            continue
        transcript = []
        for i, (session, prompt) in enumerate(turns):
            if i:
                transcript.append(f"# waited {MEMORY_WAIT} s for AgentCore Memory to extract facts and preferences\n")
                time.sleep(MEMORY_WAIT)
            transcript.append(invoke(prompt, session))
        text = f"Test {number}: {name.replace('_', ' ')}\n\n" + "\n".join(transcript)
        (OUT / f"test{number}_{name}.txt").write_text(text, encoding="utf-8")
        print(text, flush=True)
    time.sleep(15)  # let the runtime flush its logs to CloudWatch
    group, lines = runtime_log_lines(start_ms)
    suffix = "" if wanted == {n for n, *_ in TESTS} else "_tests_" + "_".join(map(str, sorted(wanted)))
    (OUT / f"runtime_logs{suffix}.txt").write_text(f"CloudWatch log group {group}\n\n" + "\n".join(lines),
                                                  encoding="utf-8")
    tool_lines = [l for l in lines if "Tool #" in l or "tool" in l.lower() and "___" in l]
    print(f"{len(lines)} runtime log lines saved; tool calls seen:\n" + "\n".join(tool_lines[:40]))
