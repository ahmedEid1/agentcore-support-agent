"""Builds the AWS backend for the AI Support Agent (us-east-1).

Creates, or reuses when re-run, everything main.py needs before `agentcore deploy`:
  - Lambdas order-tracker and refund-processor (Python 3.12, basic logging role)
  - REST API CustomerSupportAPI, stage prod: GET /orders/{order_id}, /customers/{customer_id} and
    /customers/{customer_id}/orders -> order-tracker. Methods are IAM-authorised and their
    operationName becomes the MCP tool name.
  - AgentCore Gateway CustomerSupportGateway (MCP, NONE inbound auth: main.py connects without signing) with
    targets order-tracker (the REST API, SigV4 via the gateway role) and refund-processor (the Lambda)
  - An S3 bucket with product_catalog.txt and the managed Knowledge Base CustomerSupportKB, synced
  - AgentCore Memory CustomerSupportMemory (semantic + user-preference strategies)

Target names use hyphens: the Gateway API allows [0-9a-zA-Z-] only, so the Lambdas
order_tracker/refund_processor become targets order-tracker/refund-processor (tools: order-tracker___get_order...).
Every resource is recorded in state.json so teardown_backend.py deletes exactly these.

Run with an AWS profile for us-east-1, e.g.:  AWS_PROFILE=<your-profile> python build_backend.py
"""
import io
import json
import pathlib
import time
import zipfile

import boto3
from botocore.config import Config

REGION = "us-east-1"
HERE = pathlib.Path(__file__).resolve().parent
AGENT_DIR = HERE.parent / "agent"
STATE_FILE = HERE / "state.json"
state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

ROUTES = [  # (path, operationName = tool name, path parameter, tool description)
    ("/orders/{order_id}", "get_order", "order_id",
     "Get one order by order ID (e.g. ORD-001): status, items, total, tracking number, carrier and estimated delivery."),
    ("/customers/{customer_id}/orders", "get_customer_orders", "customer_id",
     "List all orders of a customer by customer ID (e.g. CUST-123)."),
    ("/customers/{customer_id}", "get_customer", "customer_id",
     "Get a customer's profile by customer ID: name, email, loyalty tier and points balance."),
]


def save(**items):
    state.update(items)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def wait(what, check, timeout=900, every=5):
    """Poll check() until it returns something truthy; check() raises on a terminal failure."""
    start = time.time()
    while True:
        result = check()
        if result:
            return result
        if time.time() - start > timeout:
            raise TimeoutError(f"Timed out waiting for {what}")
        time.sleep(every)


def list_all(fn, key, **kwargs):
    token = None
    while True:
        resp = fn(**kwargs, **({"nextToken": token} if token else {}))
        yield from resp.get(key, [])
        token = resp.get("nextToken")
        if not token:
            return


config = Config(retries={"max_attempts": 10, "mode": "standard"})
session = boto3.Session(region_name=REGION)
ACCOUNT = session.client("sts").get_caller_identity()["Account"]
iam = session.client("iam", config=config)
lam = session.client("lambda", config=config)
apigw = session.client("apigateway", config=config)
ctl = session.client("bedrock-agentcore-control", config=config)
s3 = session.client("s3", config=config)
kb_api = session.client("bedrock-agent", config=config)
kb_rt = session.client("bedrock-agent-runtime", config=config)
print(f"Account {ACCOUNT}, region {REGION}", flush=True)
save(account=ACCOUNT, region=REGION)


# ── IAM roles ────────────────────────────────────────────────────────────────
def ensure_role(name, service, conditions=None, managed=(), inline=None):
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole",
        **({"Condition": conditions} if conditions else {})}]}
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]  # the lab denies iam:UpdateAssumeRolePolicy, so an
        created = False                                   # existing role keeps the trust policy it was created with
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="AI Support Agent backend")["Role"]["Arn"]
        created = True
    for policy_arn in managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=policy_arn)
    if inline:
        iam.put_role_policy(RoleName=name, PolicyName=f"{name}-access", PolicyDocument=json.dumps(inline))
    roles = state.get("roles", [])
    if name not in roles:
        save(roles=roles + [name])
    if created:
        time.sleep(12)  # a new role takes a few seconds before other services can assume it
    print(f"Role {name}: {'created' if created else 'reused'}", flush=True)
    return arn


# ── Lambdas ──────────────────────────────────────────────────────────────────
def ensure_function(name, source_file, handler, role_arn):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(AGENT_DIR / "lambda" / source_file, source_file)
    code = buf.getvalue()
    try:
        arn = lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]
        lam.update_function_code(FunctionName=name, ZipFile=code)
        action = "updated"
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(8):
            try:
                arn = lam.create_function(
                    FunctionName=name, Runtime="python3.12", Role=role_arn, Handler=handler,
                    Code={"ZipFile": code}, Timeout=15, MemorySize=128,
                    Description="AI Support Agent backend")["FunctionArn"]
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "assume" not in str(e) or attempt == 7:
                    raise
                time.sleep(5)  # role not assumable yet
        action = "created"
    lam.get_waiter("function_active_v2").wait(FunctionName=name)
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    functions = state.get("lambdas", [])
    if name not in functions:
        save(lambdas=functions + [name])
    print(f"Lambda {name}: {action}", flush=True)
    return arn


# ── REST API ─────────────────────────────────────────────────────────────────
def ensure_rest_api(order_arn):
    api_id = next((a["id"] for page in apigw.get_paginator("get_rest_apis").paginate()
                   for a in page["items"] if a["name"] == "CustomerSupportAPI"), None)
    if not api_id:
        api_id = apigw.create_rest_api(
            name="CustomerSupportAPI", description="Order tracking API for the AI Support Agent",
            endpointConfiguration={"types": ["REGIONAL"]})["id"]
    save(rest_api_id=api_id)

    def resources():
        return {r["path"]: r for page in apigw.get_paginator("get_resources").paginate(restApiId=api_id)
                for r in page["items"]}

    for full_path in ("/orders", "/orders/{order_id}", "/customers", "/customers/{customer_id}",
                      "/customers/{customer_id}/orders"):
        existing = resources()
        if full_path not in existing:
            parent_path, part = full_path.rsplit("/", 1)
            apigw.create_resource(restApiId=api_id, parentId=existing[parent_path or "/"]["id"], pathPart=part)
    existing = resources()
    uri = f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/{order_arn}/invocations"
    for path, op_name, param, _ in ROUTES:
        rid = existing[path]["id"]
        try:  # re-create the method so its settings always match this script
            apigw.delete_method(restApiId=api_id, resourceId=rid, httpMethod="GET")
        except apigw.exceptions.NotFoundException:
            pass
        apigw.put_method(restApiId=api_id, resourceId=rid, httpMethod="GET", authorizationType="AWS_IAM",
                         operationName=op_name, requestParameters={f"method.request.path.{param}": True})
        apigw.put_integration(restApiId=api_id, resourceId=rid, httpMethod="GET", type="AWS_PROXY",
                              integrationHttpMethod="POST", uri=uri)
        # The Gateway reads the API through its OpenAPI export, which needs a declared response per operation
        apigw.put_method_response(restApiId=api_id, resourceId=rid, httpMethod="GET", statusCode="200",
                                  responseModels={"application/json": "Empty"})
    try:
        lam.add_permission(FunctionName="order-tracker", StatementId="apigateway-invoke",
                           Action="lambda:InvokeFunction", Principal="apigateway.amazonaws.com",
                           SourceArn=f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{api_id}/*/GET/*")
    except lam.exceptions.ResourceConflictException:
        pass
    apigw.create_deployment(restApiId=api_id, stageName="prod", description="AI Support Agent tools")
    print(f"REST API CustomerSupportAPI {api_id}: deployed to stage prod (3 GET methods, AWS_IAM)", flush=True)
    return api_id


# ── Gateway ──────────────────────────────────────────────────────────────────
def ensure_gateway(role_arn):
    gw_id = next((g["gatewayId"] for g in list_all(ctl.list_gateways, "items")
                  if g["name"] == "CustomerSupportGateway"), None)
    if not gw_id:
        gw_id = ctl.create_gateway(
            name="CustomerSupportGateway", description="MCP tools for the AI Support Agent",
            roleArn=role_arn, protocolType="MCP", authorizerType="NONE")["gatewayId"]
    save(gateway_id=gw_id)

    def ready():
        g = ctl.get_gateway(gatewayIdentifier=gw_id)
        if g["status"] in ("FAILED", "UPDATE_UNSUCCESSFUL"):
            raise RuntimeError(f"Gateway {g['status']}: {g.get('statusReasons')}")
        return g if g["status"] == "READY" else None

    g = wait("gateway", ready)
    save(gateway_url=g["gatewayUrl"])
    print(f"Gateway CustomerSupportGateway {gw_id}: READY {g['gatewayUrl']}", flush=True)
    return gw_id


def ensure_target(gw_id, name, target_config, creds):
    existing = {t["name"]: t["targetId"] for t in list_all(ctl.list_gateway_targets, "items", gatewayIdentifier=gw_id)}
    if name in existing:
        tid = existing[name]
        ctl.update_gateway_target(gatewayIdentifier=gw_id, targetId=tid, name=name,
                                  targetConfiguration=target_config, credentialProviderConfigurations=creds)
    else:
        tid = ctl.create_gateway_target(gatewayIdentifier=gw_id, name=name, targetConfiguration=target_config,
                                        credentialProviderConfigurations=creds)["targetId"]

    def ready():
        t = ctl.get_gateway_target(gatewayIdentifier=gw_id, targetId=tid)
        if "FAIL" in t["status"] or "UNSUCCESSFUL" in t["status"]:
            raise RuntimeError(f"Target {name} {t['status']}: {t.get('statusReasons')}")
        return t["status"] == "READY"

    wait(f"target {name}", ready)
    print(f"Gateway target {name} {tid}: READY", flush=True)
    return tid


# ── Knowledge Base ───────────────────────────────────────────────────────────
def ensure_knowledge_base(role_arn, bucket):
    kb_id = next((k["knowledgeBaseId"] for k in list_all(kb_api.list_knowledge_bases, "knowledgeBaseSummaries")
                  if k["name"] == "CustomerSupportKB"), None)
    if not kb_id:
        kb_id = kb_api.create_knowledge_base(
            name="CustomerSupportKB", description="Product catalog and support policies",
            roleArn=role_arn,
            knowledgeBaseConfiguration={"type": "MANAGED",
                                        "managedKnowledgeBaseConfiguration": {"embeddingModelType": "MANAGED"}},
        )["knowledgeBase"]["knowledgeBaseId"]
    save(knowledge_base_id=kb_id)

    def kb_ready():
        kb = kb_api.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
        if kb["status"] == "FAILED":
            raise RuntimeError(f"Knowledge base FAILED: {kb.get('failureReasons')}")
        return kb["status"] == "ACTIVE"

    wait("knowledge base", kb_ready)
    print(f"Knowledge base CustomerSupportKB {kb_id}: ACTIVE", flush=True)

    ds_id = next((d["dataSourceId"] for d in list_all(kb_api.list_data_sources, "dataSourceSummaries",
                                                       knowledgeBaseId=kb_id)), None)
    if not ds_id:
        ds_id = kb_api.create_data_source(
            knowledgeBaseId=kb_id, name="product-catalog", dataDeletionPolicy="DELETE",
            description="product_catalog.txt in S3",
            dataSourceConfiguration={
                "type": "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
                "managedKnowledgeBaseConnectorConfiguration": {"connectorParameters": {
                    "type": "S3", "version": "1",
                    "connectionConfiguration": {"bucketName": bucket, "bucketOwnerAccountId": ACCOUNT},
                    "deletionProtectionConfiguration": {"enableDeletionProtection": False},
                }},
            },
        )["dataSource"]["dataSourceId"]
    save(data_source_id=ds_id)

    def ds_ready():
        ds = kb_api.get_data_source(knowledgeBaseId=kb_id, dataSourceId=ds_id)["dataSource"]
        if ds["status"] in ("DELETE_UNSUCCESSFUL",) or "FAIL" in ds["status"]:
            raise RuntimeError(f"Data source {ds['status']}: {ds.get('failureReasons')}")
        return ds["status"] == "AVAILABLE"

    wait("data source", ds_ready)
    job_id = kb_api.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)["ingestionJob"]["ingestionJobId"]

    def synced():
        job = kb_api.get_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id)["ingestionJob"]
        if job["status"] in ("FAILED", "STOPPED"):
            raise RuntimeError(f"Ingestion {job['status']}: {job.get('failureReasons')}")
        return job if job["status"] == "COMPLETE" else None

    job = wait("ingestion", synced, every=10)
    print(f"Data source product-catalog {ds_id}: synced {job.get('statistics')}", flush=True)
    hits = kb_rt.retrieve(knowledgeBaseId=kb_id,
                          retrievalQuery={"text": "What is the return policy for electronics?"})["retrievalResults"]
    print(f"KB test ({len(hits)} results): {hits[0]['content']['text'][:160]!r}" if hits else "KB test: no results",
          flush=True)
    return kb_id


# ── Memory ───────────────────────────────────────────────────────────────────
def ensure_memory():
    mem_id = next((m["id"] for m in list_all(ctl.list_memories, "memories")
                   if m["id"].startswith("CustomerSupportMemory-")), None)
    if not mem_id:
        mem_id = ctl.create_memory(
            name="CustomerSupportMemory", description="Customer facts and preferences",
            eventExpiryDuration=30,
            memoryStrategies=[
                {"semanticMemoryStrategy": {"name": "customer_facts",
                                            "namespaceTemplates": ["cs_agent/{actorId}/facts"]}},
                {"userPreferenceMemoryStrategy": {"name": "customer_preferences",
                                                  "namespaceTemplates": ["cs_agent/{actorId}/preferences"]}},
            ],
        )["memory"]["id"]
    save(memory_id=mem_id)

    def active():
        m = ctl.get_memory(memoryId=mem_id)["memory"]
        if m["status"] == "FAILED":
            raise RuntimeError(f"Memory FAILED: {m.get('failureReason')}")
        return m["status"] == "ACTIVE"

    wait("memory", active, every=10)
    print(f"Memory CustomerSupportMemory {mem_id}: ACTIVE", flush=True)
    return mem_id


if __name__ == "__main__":
    lambda_role = ensure_role("csai-lambda-role", "lambda.amazonaws.com",
                              managed=["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"])
    order_arn = ensure_function("order-tracker", "order_tracker.py", "order_tracker.lambda_handler", lambda_role)
    refund_arn = ensure_function("refund-processor", "refund_processor.py", "refund_processor.lambda_handler", lambda_role)
    api_id = ensure_rest_api(order_arn)

    gateway_role = ensure_role(
        "csai-gateway-role", "bedrock-agentcore.amazonaws.com",
        conditions={"StringEquals": {"aws:SourceAccount": ACCOUNT},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:*"}},
        inline={"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": refund_arn},
            {"Effect": "Allow", "Action": "execute-api:Invoke",
             "Resource": f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{api_id}/prod/*/*"},
        ]})
    gw_id = ensure_gateway(gateway_role)
    ensure_target(
        gw_id, "order-tracker",
        {"mcp": {"apiGateway": {"restApiId": api_id, "stage": "prod", "apiGatewayToolConfiguration": {
            "toolFilters": [{"filterPath": path, "methods": ["GET"]} for path, *_ in ROUTES],
            "toolOverrides": [{"path": path, "method": "GET", "name": op, "description": desc}
                              for path, op, _, desc in ROUTES]}}}},
        [{"credentialProviderType": "GATEWAY_IAM_ROLE"}])  # SigV4 with the gateway role (no explicit provider for API Gateway targets)
    tool_schema = json.loads((AGENT_DIR / "lambda" / "lambda_schema").read_text())
    ensure_target(gw_id, "refund-processor",
                  {"mcp": {"lambda": {"lambdaArn": refund_arn, "toolSchema": {"inlinePayload": tool_schema}}}},
                  [{"credentialProviderType": "GATEWAY_IAM_ROLE"}])

    bucket = state.get("bucket") or f"csai-kb-{ACCOUNT}"
    try:
        s3.create_bucket(Bucket=bucket)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    save(bucket=bucket)
    s3.upload_file(str(AGENT_DIR / "product_catalog.txt"), bucket, "product_catalog.txt")
    print(f"S3 bucket {bucket}: product_catalog.txt uploaded", flush=True)
    kb_role = ensure_role(
        "csai-kb-role", "bedrock.amazonaws.com",
        conditions={"StringEquals": {"aws:SourceAccount": ACCOUNT},
                    "ArnLike": {"AWS:SourceArn": f"arn:aws:bedrock:{REGION}:{ACCOUNT}:knowledge-base/*"}},
        inline={"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}"},
            {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/*"},
            {"Effect": "Allow", "Action": ["bedrock:ListFoundationModels", "bedrock:ListCustomModels"], "Resource": "*"},
        ]})
    kb_id = ensure_knowledge_base(kb_role, bucket)
    mem_id = ensure_memory()

    print("\nSet these for agent/main.py (environment variables, or the placeholders at its top):", flush=True)
    print(f'GATEWAY_URL={state["gateway_url"]}')
    print(f'KB_ID={kb_id}')
    print(f'MEMORY_ID={mem_id}')
