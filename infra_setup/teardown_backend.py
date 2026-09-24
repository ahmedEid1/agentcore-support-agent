"""Deletes the backend that build_backend.py created, using the IDs recorded in state.json.

Run `uv run agentcore destroy` in agent/ first (it removes the runtime and its role).
Dry run by default; pass --yes to delete. Safe to re-run: missing resources are skipped.
    AWS_PROFILE=<your-profile> python teardown_backend.py --yes
"""
import json
import pathlib
import sys
import time

import boto3
from botocore.exceptions import ClientError

HERE = pathlib.Path(__file__).resolve().parent
state = json.loads((HERE / "state.json").read_text())
DO_IT = "--yes" in sys.argv
session = boto3.Session(region_name=state["region"])
if session.client("sts").get_caller_identity()["Account"] != state["account"]:
    sys.exit(f"Refusing: these credentials are not for account {state['account']} (check AWS_PROFILE).")
ctl = session.client("bedrock-agentcore-control")
kb_api = session.client("bedrock-agent")
s3 = session.resource("s3")
apigw = session.client("apigateway")
lam = session.client("lambda")
iam = session.client("iam")


def step(label, fn):
    if not DO_IT:
        print(f"[dry run] would delete {label}")
        return
    try:
        fn()
        print(f"deleted {label}")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ResourceNotFoundException", "NotFoundException", "NoSuchEntity", "NoSuchBucket"):
            print(f"already gone: {label}")
        else:
            print(f"FAILED {label}: {code} {e.response['Error'].get('Message', '')}")


if gw := state.get("gateway_id"):
    try:
        targets = ctl.list_gateway_targets(gatewayIdentifier=gw).get("items", [])
    except ClientError:
        targets = []
    for t in targets:
        step(f"gateway target {t['name']}",
             lambda t=t: ctl.delete_gateway_target(gatewayIdentifier=gw, targetId=t["targetId"]))
    if targets and DO_IT:
        time.sleep(15)  # targets must be gone before the gateway can be deleted
    step(f"gateway {gw}", lambda: ctl.delete_gateway(gatewayIdentifier=gw))
if mem := state.get("memory_id"):
    step(f"memory {mem}", lambda: ctl.delete_memory(memoryId=mem))
if kb := state.get("knowledge_base_id"):
    if ds := state.get("data_source_id"):
        step(f"data source {ds}", lambda: kb_api.delete_data_source(knowledgeBaseId=kb, dataSourceId=ds))
    step(f"knowledge base {kb}", lambda: kb_api.delete_knowledge_base(knowledgeBaseId=kb))
if bucket := state.get("bucket"):
    def empty_and_delete():
        b = s3.Bucket(bucket)
        b.object_versions.delete()
        b.delete()
    step(f"S3 bucket {bucket}", empty_and_delete)
if api := state.get("rest_api_id"):
    step(f"REST API {api}", lambda: apigw.delete_rest_api(restApiId=api))
for fn in state.get("lambdas", []):
    step(f"Lambda {fn}", lambda fn=fn: lam.delete_function(FunctionName=fn))
for role in state.get("roles", []):
    def delete_role(role=role):
        for p in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
        for name in iam.list_role_policies(RoleName=role)["PolicyNames"]:
            iam.delete_role_policy(RoleName=role, PolicyName=name)
        iam.delete_role(RoleName=role)
    step(f"IAM role {role}", delete_role)
if not DO_IT:
    print("\nDry run only. Re-run with --yes to delete.")
