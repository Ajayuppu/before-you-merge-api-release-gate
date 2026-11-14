# API review gate

Local reference and CloudShell deployment package for a serverless check that catches breaking OpenAPI changes before merge.

Deployed and validated live in us-east-1 on 14 November 2026. The suite is 68 or more tests. AgentCore Gateway, Policy, and Code Interpreter were validated: gateway-read PASS, approve denied by policy, another identity denied, and code-interpreter diff PASS. AgentCore Runtime was not deployed. The workflow uses `SPEC_CHECK_MODE=in-process`.

Read [guide.md](guide.md) for the architecture, the local evidence, and the later CloudShell steps. The finished article is [docs/ARTICLE.md](docs/ARTICLE.md).

```bash
bash scripts/run_local_tests.sh
bash scripts/run_local_demo.sh
```

Python 3.11 or newer is enough. `pytest` is the only test dependency. There is no AWS credential and no account id in this tree.
