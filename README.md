# API review gate

Local reference and CloudShell deployment package for a serverless check that catches breaking OpenAPI changes before merge.

The tested path is the Python package in `src/apireview`. It does not call AWS. CloudFormation, Step Functions, IAM, and AgentCore files under `cloud/` are deployment assets. They were assembled and contract-checked locally. They were not deployed.

Read [guide.md](guide.md) for the architecture, the local evidence, and the later CloudShell steps.

```bash
bash scripts/run_local_tests.sh
bash scripts/run_local_demo.sh
```

Python 3.11 or newer is enough. `pytest` is the only test dependency. There is no AWS credential and no account id in this tree.
