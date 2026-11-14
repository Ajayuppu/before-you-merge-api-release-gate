# UNPUBLISHED DRAFT

Do not publish this file. It is a short draft for a later Medium post. It has no screenshots. Add screenshots only after a real AWS run, using blur rather than solid black coverings, and only for images you captured yourself.

# Before You Merge: Catch Breaking API Changes with Amazon Bedrock, AgentCore, Amazon Q Developer, and a Serverless AWS Release Gate

A pull request can change an API in a way that still looks reasonable in review. `amount` moves from a JSON number to a string. `next_cursor` disappears from the orders page. Old clients fail in production, after the diff looked small.

This project is a release gate for that moment. A developer submits the old OpenAPI document, the candidate document, and a declarative fixture. The fixture is JSON, not code. The gate returns a job. A deterministic diff and a fixed harness decide the verdict. A different person can approve only when that exact evidence passed.

The broken orders example is deliberate. The candidate changes `amount` to a string and removes required `next_cursor`. The local run reports both breaks, fails the harness, and rejects approval with `job is not waiting for review (BLOCKED)`. The corrected candidate restores both fields and may add optional `currency`. That job reaches `APPROVED` once, and a replay does not approve it again.

Bedrock can explain the findings when you enable it. The explanation is not the gate. AgentCore, if you add it later, is allowed three tools: read artifacts, request the fixed spec check, and read test results. There is no approve tool. Amazon Q Developer stays in the IDE. It is not a runtime API in this design. The harness Lambda, not the code interpreter, performs the controlled call to the versioned mock. A SANDBOX interpreter can still reach AWS APIs such as S3, so it is not an air gap.

The full walkthrough is in `guide.md`. Deployed and validated live in us-east-1 on 14 November 2026. The suite is 68 or more tests. AgentCore Gateway, Policy, and Code Interpreter were validated: gateway-read PASS, approve denied by policy, another identity denied, and code-interpreter diff PASS. AgentCore Runtime was not deployed. The workflow uses `SPEC_CHECK_MODE=in-process`. This draft still has no screenshots.

When you do deploy, capture the console at full window size. Blur account ids and credentials. Do not hide them behind black strips. Publish only after those pictures are real.
