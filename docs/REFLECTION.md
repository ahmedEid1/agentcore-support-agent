# Reflection: AI Support Agent

## Design decision

The loyalty tool `calculate_loyalty_discount` turns the model's arguments into a Python script and runs it in the AgentCore Code Interpreter. Those arguments come from an LLM reading customer text, so I don't trust them. Before anything goes into the script, the tool checks tier and category against fixed lists and converts the amounts to numbers. That stops a crafted value from injecting code. The script also runs with `clearContext=True`, which means nothing carries over between customers. In Test 5 it returned 4,000 points redeemed, a 10% Gold discount and a final total of $99.00.

## Challenge

After the first deployment, the browser test (Test 6) failed. The runtime logs showed a PermissionError on Playwright's Node.js driver. I had deployed from Windows, and the starter toolkit zips the dependencies with Windows file modes, which stripped the driver's execute permission. I rewrote the toolkit's cached `dependencies.zip` to make the driver executable (mode 755), and the next deployment returned the live page title. Next time I'd build the package on Linux, for example in CI.

## Production consideration

Right now the Gateway uses the NONE inbound authorizer, so anyone who finds its URL can call the order and refund tools directly. The agent also trusts whatever `customer_id` the caller sends. For production I'd put a JWT authorizer (Amazon Cognito) on the Gateway and the Runtime. Then the customer ID would come from a verified token instead of the request.

I built and debugged this project with the help of an AI assistant (Claude).
