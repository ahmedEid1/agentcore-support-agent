# agent/

The deployable agent: `main.py` (Strands Agents SDK + Amazon Bedrock AgentCore Runtime), its runtime
dependencies (`requirements.txt`), the uv project used for local development (`pyproject.toml`, `uv.lock`),
the backend Lambdas and tool schema (`lambda/`), the product catalog the knowledge base indexes
(`product_catalog.txt`), and `setup_permissions.py`, which grants the runtime role access to the Gateway,
Knowledge Base, Memory, Code Interpreter and Browser.

See the repository [README](../README.md) for the architecture and how to build, deploy and test it.
