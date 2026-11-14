"""Build the CloudFormation template for a later CloudShell deploy.

This script does not call AWS. It writes cloud/template.deploy.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from apireview.mocks import mock_body_text  # noqa: E402

ASL_PATH = ROOT / "cloud" / "statemachine.asl.json"
OUT_PATH = ROOT / "cloud" / "template.deploy.json"

PLACEHOLDERS = (
    "ValidateFunctionArn",
    "DiffFunctionArn",
    "AnalyzeFunctionArn",
    "SpecCheckFunctionArn",
    "HarnessFunctionArn",
    "SealFunctionArn",
    "RegisterWaitFunctionArn",
    "FinalizeFunctionArn",
    "ExpireFunctionArn",
)

FUNCTIONS = (
    ("Submit", "submit", "apireview.handlers.lambda_handlers.submit_handler", 15, "SubmitRole"),
    ("Status", "status", "apireview.handlers.lambda_handlers.status_handler", 10, "StatusRole"),
    ("Review", "review", "apireview.handlers.lambda_handlers.review_handler", 15, "ReviewRole"),
    ("ReviewContext", "review-context", "apireview.handlers.lambda_handlers.review_context_handler", 10, "ReviewRole"),
    ("Publisher", "publisher", "apireview.handlers.lambda_handlers.publisher_handler", 20, "PublisherRole"),
    ("Dispatcher", "dispatcher", "apireview.handlers.lambda_handlers.dispatcher_handler", 30, "DispatcherRole"),
    ("Validate", "validate", "apireview.handlers.lambda_handlers.validate_handler", 15, "WorkflowRole"),
    ("Diff", "diff", "apireview.handlers.lambda_handlers.diff_handler", 15, "WorkflowRole"),
    ("Analyze", "analyze", "apireview.handlers.lambda_handlers.analyze_handler", 30, "AnalyzeRole"),
    ("SpecCheck", "spec-check", "apireview.handlers.lambda_handlers.spec_check_handler", 60, "WorkflowRole"),
    ("Harness", "harness", "apireview.handlers.lambda_handlers.harness_handler", 20, "HarnessRole"),
    ("Seal", "seal", "apireview.handlers.lambda_handlers.seal_handler", 15, "WorkflowRole"),
    ("RegisterWait", "register-wait", "apireview.handlers.lambda_handlers.register_wait_handler", 15, "RegisterRole"),
    ("Finalize", "finalize", "apireview.handlers.lambda_handlers.finalize_handler", 15, "WorkflowRole"),
    ("Expire", "expire", "apireview.handlers.lambda_handlers.expire_handler", 15, "WorkflowRole"),
)


def sub_escape(definition: str) -> str:
    sentinels: dict[str, str] = {}
    for name in PLACEHOLDERS:
        token = "${" + name + "}"
        marker = f"__PLACEHOLDER_{name}__"
        if token not in definition:
            raise SystemExit(f"state machine is missing {token}")
        definition = definition.replace(token, marker)
        sentinels[marker] = token
    # Fn::Sub only treats ${...} specially; escape any other ${ as ${! and leave plain $ alone.
    definition = definition.replace("${", "${!")
    for marker, token in sentinels.items():
        definition = definition.replace(marker, token)
    return definition


def assume(service: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Action": "sts:AssumeRole",
                "Effect": "Allow",
                "Principal": {"Service": service},
            }
        ],
    }


def logs_and_trace() -> list[dict]:
    return [
        {
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Effect": "Allow",
            "Resource": {"Fn::Sub": "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/${ProjectName}*:*"},
        },
        {
            "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"],
            "Effect": "Allow",
            "Resource": "*",
        },
    ]


def role(statements: list[dict]) -> dict:
    return {
        "Type": "AWS::IAM::Role",
        "Properties": {
            "AssumeRolePolicyDocument": assume("lambda.amazonaws.com"),
            "Policies": [{"PolicyName": "api-review", "PolicyDocument": {"Version": "2012-10-17", "Statement": statements + logs_and_trace()}}],
        },
    }


def lambda_uri(logical: str) -> dict:
    return {
        "Fn::Sub": [
            "arn:${AWS::Partition}:apigateway:${AWS::Region}:lambda:path/2015-03-31/functions/${Arn}/invocations",
            {"Arn": {"Fn::GetAtt": [logical, "Arn"]}},
        ]
    }


def proxy_method(logical: str) -> dict:
    return {
        "consumes": ["application/json"],
        "produces": ["application/json"],
        "responses": {
            "200": {"description": "OK"},
            "201": {"description": "Created"},
            "202": {"description": "Accepted"},
            "400": {"description": "Rejected"},
            "403": {"description": "Denied"},
            "409": {"description": "Conflict"},
        },
        "security": [{"sigv4": []}],
        "x-amazon-apigateway-auth": {"type": "AWS_IAM"},
        "x-amazon-apigateway-integration": {
            "httpMethod": "POST",
            "type": "aws_proxy",
            "uri": lambda_uri(logical),
        },
    }


def mock_method(body: str) -> dict:
    return {
        "produces": ["application/json"],
        "responses": {"200": {"description": "Fixed order page"}},
        "security": [{"sigv4": []}],
        "x-amazon-apigateway-auth": {"type": "AWS_IAM"},
        "x-amazon-apigateway-integration": {
            "requestTemplates": {"application/json": '{"statusCode": 200}'},
            "responses": {
                "default": {
                    "responseTemplates": {"application/json": body},
                    "statusCode": "200",
                }
            },
            "type": "mock",
        },
    }


def security_definitions() -> dict:
    return {
        "sigv4": {
            "in": "header",
            "name": "Authorization",
            "type": "apiKey",
            "x-amazon-apigateway-authtype": "awsSigv4",
        }
    }


def build_template() -> dict:
    resources: dict[str, dict] = {}
    resources["ArtifactBucket"] = {
        "Type": "AWS::S3::Bucket",
        "Properties": {
            "BucketEncryption": {"ServerSideEncryptionConfiguration": [{"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]},
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "VersioningConfiguration": {"Status": "Enabled"},
        },
    }
    resources["JobTable"] = {
        "Type": "AWS::DynamoDB::Table",
        "Properties": {
            "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"}],
            "BillingMode": "PAY_PER_REQUEST",
            "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
            "StreamSpecification": {"StreamViewType": "NEW_IMAGE"},
            "TableName": {"Fn::Sub": "${ProjectName}-jobs"},
        },
    }
    resources["WorkerDlq"] = {"Type": "AWS::SQS::Queue", "Properties": {"QueueName": {"Fn::Sub": "${ProjectName}-worker-dlq"}}}
    resources["EventBridgeDlq"] = {"Type": "AWS::SQS::Queue", "Properties": {"QueueName": {"Fn::Sub": "${ProjectName}-eventbridge-dlq"}}}
    resources["WorkQueue"] = {
        "Type": "AWS::SQS::Queue",
        "Properties": {
            "QueueName": {"Fn::Sub": "${ProjectName}-work"},
            "RedrivePolicy": {"deadLetterTargetArn": {"Fn::GetAtt": ["WorkerDlq", "Arn"]}, "maxReceiveCount": 5},
            "VisibilityTimeout": 182,
        },
    }
    resources["ReviewBus"] = {"Type": "AWS::Events::EventBus", "Properties": {"Name": {"Fn::Sub": "${ProjectName}"}}}
    resources["SubmitRule"] = {
        "Type": "AWS::Events::Rule",
        "Properties": {
            "EventBusName": {"Ref": "ReviewBus"},
            "EventPattern": {"detail-type": ["JobSubmitted"], "source": ["api.review"]},
            "Name": {"Fn::Sub": "${ProjectName}-submitted"},
            "State": "ENABLED",
            "Targets": [
                {
                    "Arn": {"Fn::GetAtt": ["WorkQueue", "Arn"]},
                    "DeadLetterConfig": {"Arn": {"Fn::GetAtt": ["EventBridgeDlq", "Arn"]}},
                    "Id": "WorkQueue",
                }
            ],
        },
    }
    resources["QueuePolicy"] = {
        "Type": "AWS::SQS::QueuePolicy",
        "Properties": {
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Action": "sqs:SendMessage",
                        "Condition": {"ArnEquals": {"aws:SourceArn": {"Fn::GetAtt": ["SubmitRule", "Arn"]}}},
                        "Effect": "Allow",
                        "Principal": {"Service": "events.amazonaws.com"},
                        "Resource": {"Fn::GetAtt": ["WorkQueue", "Arn"]},
                    },
                    {
                        "Action": "sqs:SendMessage",
                        "Condition": {"ArnEquals": {"aws:SourceArn": {"Fn::GetAtt": ["SubmitRule", "Arn"]}}},
                        "Effect": "Allow",
                        "Principal": {"Service": "events.amazonaws.com"},
                        "Resource": {"Fn::GetAtt": ["EventBridgeDlq", "Arn"]},
                    },
                ],
            },
            "Queues": [{"Ref": "WorkQueue"}, {"Ref": "EventBridgeDlq"}],
        },
    }
    table_arn = {"Fn::GetAtt": ["JobTable", "Arn"]}
    bucket_arn = {"Fn::GetAtt": ["ArtifactBucket", "Arn"]}
    resources["SubmitRole"] = role(
        [
            {"Action": ["s3:PutObject"], "Effect": "Allow", "Resource": {"Fn::Sub": "${ArtifactBucket.Arn}/artifacts/*"}},
            {"Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:TransactWriteItems"], "Effect": "Allow", "Resource": table_arn},
        ]
    )
    resources["StatusRole"] = role([{"Action": ["dynamodb:GetItem"], "Effect": "Allow", "Resource": table_arn}])
    resources["ReviewRole"] = role(
        [
            {"Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"], "Effect": "Allow", "Resource": table_arn},
            {"Action": ["s3:GetObject"], "Effect": "Allow", "Resource": {"Fn::Sub": "${ArtifactBucket.Arn}/artifacts/*"}},
            {
                "Action": ["states:SendTaskSuccess"],
                "Effect": "Allow",
                "Resource": {"Fn::Sub": "arn:${AWS::Partition}:states:${AWS::Region}:${AWS::AccountId}:stateMachine:${ProjectName}"},
            },
        ]
    )
    resources["PublisherRole"] = role(
        [
            {
                "Action": ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"],
                "Effect": "Allow",
                "Resource": {"Fn::GetAtt": ["JobTable", "StreamArn"]},
            },
            {"Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"], "Effect": "Allow", "Resource": table_arn},
            {"Action": ["events:PutEvents"], "Effect": "Allow", "Resource": {"Fn::GetAtt": ["ReviewBus", "Arn"]}},
        ]
    )
    resources["DispatcherRole"] = role(
        [
            {
                "Action": ["sqs:ChangeMessageVisibility", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ReceiveMessage"],
                "Effect": "Allow",
                "Resource": {"Fn::GetAtt": ["WorkQueue", "Arn"]},
            },
            {"Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"], "Effect": "Allow", "Resource": table_arn},
            {
                "Action": ["states:DescribeExecution", "states:StartExecution"],
                "Effect": "Allow",
                "Resource": [
                    {"Fn::Sub": "arn:${AWS::Partition}:states:${AWS::Region}:${AWS::AccountId}:stateMachine:${ProjectName}"},
                    {"Fn::Sub": "arn:${AWS::Partition}:states:${AWS::Region}:${AWS::AccountId}:execution:${ProjectName}:*"},
                ],
            },
        ]
    )
    resources["WorkflowRole"] = role(
        [
            {"Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"], "Effect": "Allow", "Resource": table_arn},
            {"Action": ["s3:GetObject"], "Effect": "Allow", "Resource": {"Fn::Sub": "${ArtifactBucket.Arn}/artifacts/*"}},
        ]
    )
    resources["AnalyzeRole"] = role(
        [
            {"Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"], "Effect": "Allow", "Resource": table_arn},
            {"Action": ["s3:GetObject"], "Effect": "Allow", "Resource": {"Fn::Sub": "${ArtifactBucket.Arn}/artifacts/*"}},
            {
                "Action": ["bedrock:InvokeModel"],
                "Effect": "Allow",
                "Resource": [
                    {"Fn::Sub": "arn:${AWS::Partition}:bedrock:${AWS::Region}::foundation-model/amazon.nova-2-lite-v1:0"},
                    {"Fn::Sub": "arn:${AWS::Partition}:bedrock:${AWS::Region}:${AWS::AccountId}:inference-profile/${BedrockModelId}"},
                ],
            },
        ]
    )
    resources["HarnessRole"] = role(
        [
            {"Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"], "Effect": "Allow", "Resource": table_arn},
            {"Action": ["s3:GetObject"], "Effect": "Allow", "Resource": {"Fn::Sub": "${ArtifactBucket.Arn}/artifacts/*"}},
            {
                "Action": ["execute-api:Invoke"],
                "Effect": "Allow",
                "Resource": [
                    {"Fn::Sub": "arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${MockApi}/live/GET/orders-v1/orders"},
                    {"Fn::Sub": "arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${MockApi}/live/GET/orders-v2-broken/orders"},
                    {"Fn::Sub": "arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${MockApi}/live/GET/orders-v2-corrected/orders"},
                ],
            },
        ]
    )
    resources["RegisterRole"] = role(
        [
            {"Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"], "Effect": "Allow", "Resource": table_arn},
        ]
    )
    for logical, suffix, handler, timeout, role_name in FUNCTIONS:
        resources[f"{logical}LogGroup"] = {
            "Type": "AWS::Logs::LogGroup",
            "Properties": {"LogGroupName": {"Fn::Sub": f"/aws/lambda/${{ProjectName}}-{suffix}"}, "RetentionInDays": 30},
        }
        environment = {
            "ARTIFACT_BUCKET_NAME": {"Ref": "ArtifactBucket"},
            "BEDROCK_ADVISORY": "disabled",
            "BEDROCK_MODEL_ID": {"Ref": "BedrockModelId"},
            "EVENT_BUS_NAME": {"Ref": "ReviewBus"},
            "HARNESS_TRANSPORT": "signed-mock",
            "JOB_TABLE_NAME": {"Ref": "JobTable"},
            "LOCAL_IDENTITY_SIMULATION": "false",
            "MOCK_API_HOST": {"Fn::Sub": "${MockApi}.execute-api.${AWS::Region}.amazonaws.com"},
            "MOCK_STAGE": "live",
            "OTEL_AWS_APPLICATION_SIGNALS_ENABLED": "false",
            "OTEL_PROPAGATORS": "xray,tracecontext",
            "OTEL_SERVICE_NAME": {"Fn::Sub": f"${{ProjectName}}-{suffix}"},
            "REVIEW_TTL_SECONDS": "3600",
            "SPEC_CHECK_MODE": "in-process",
            "STATE_MACHINE_ARN": {"Fn::Sub": "arn:${AWS::Partition}:states:${AWS::Region}:${AWS::AccountId}:stateMachine:${ProjectName}"},
        }
        resources[f"{logical}Function"] = {
            "Type": "AWS::Lambda::Function",
            "DependsOn": f"{logical}LogGroup",
            "Properties": {
                "Code": {"S3Bucket": {"Ref": "LambdaCodeBucket"}, "S3Key": {"Ref": "LambdaCodeKey"}},
                "Environment": {"Variables": environment},
                "FunctionName": {"Fn::Sub": f"${{ProjectName}}-{suffix}"},
                "Handler": handler,
                "Layers": [{"Ref": "OtelLayerArn"}],
                "MemorySize": 256,
                "Role": {"Fn::GetAtt": [role_name, "Arn"]},
                "Runtime": "python3.12",
                "Timeout": timeout,
                "TracingConfig": {"Mode": "Active"},
            },
        }
        resources[f"{logical}Function"]["Properties"]["Environment"]["Variables"]["AWS_LAMBDA_EXEC_WRAPPER"] = "/opt/otel-instrument"
    resources["PublisherMapping"] = {
        "Type": "AWS::Lambda::EventSourceMapping",
        "Properties": {
            "BatchSize": 10,
            "EventSourceArn": {"Fn::GetAtt": ["JobTable", "StreamArn"]},
            "FunctionName": {"Ref": "PublisherFunction"},
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
            "StartingPosition": "LATEST",
        },
    }
    resources["DispatcherMapping"] = {
        "Type": "AWS::Lambda::EventSourceMapping",
        "Properties": {
            "BatchSize": 5,
            "EventSourceArn": {"Fn::GetAtt": ["WorkQueue", "Arn"]},
            "FunctionName": {"Ref": "DispatcherFunction"},
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
            "MaximumBatchingWindowInSeconds": 2,
        },
    }
    review_body = {
        "info": {"title": "api-review"},
        "paths": {
            "/jobs": {"post": proxy_method("SubmitFunction")},
            "/jobs/{jobId}": {"get": proxy_method("StatusFunction")},
            "/jobs/{jobId}/report": {"get": proxy_method("StatusFunction")},
            "/jobs/{jobId}/review-context": {"get": proxy_method("ReviewContextFunction")},
            "/reviews": {"post": proxy_method("ReviewFunction")},
        },
        "securityDefinitions": security_definitions(),
        "swagger": "2.0",
    }
    mock_body = {
        "info": {"title": "api-review-mocks"},
        "paths": {
            "/orders-v1/orders": {"get": mock_method(mock_body_text("orders-v1"))},
            "/orders-v2-broken/orders": {"get": mock_method(mock_body_text("orders-v2-broken"))},
            "/orders-v2-corrected/orders": {"get": mock_method(mock_body_text("orders-v2-corrected"))},
        },
        "securityDefinitions": security_definitions(),
        "swagger": "2.0",
    }
    resources["ReviewApi"] = {
        "Type": "AWS::ApiGateway::RestApi",
        "Properties": {"Body": review_body, "EndpointConfiguration": {"Types": ["REGIONAL"]}, "Name": {"Fn::Sub": "${ProjectName}"}},
    }
    resources["MockApi"] = {
        "Type": "AWS::ApiGateway::RestApi",
        "Properties": {"Body": mock_body, "EndpointConfiguration": {"Types": ["REGIONAL"]}, "Name": {"Fn::Sub": "${ProjectName}-mocks"}},
    }
    resources["ReviewDeployment"] = {
        "Type": "AWS::ApiGateway::Deployment",
        "DependsOn": "ReviewApi",
        "Properties": {"Description": {"Ref": "DeploymentNonce"}, "RestApiId": {"Ref": "ReviewApi"}},
    }
    resources["MockDeployment"] = {
        "Type": "AWS::ApiGateway::Deployment",
        "DependsOn": "MockApi",
        "Properties": {"Description": {"Ref": "DeploymentNonce"}, "RestApiId": {"Ref": "MockApi"}},
    }
    for prefix, deployment, api in (("Review", "ReviewDeployment", "ReviewApi"), ("Mock", "MockDeployment", "MockApi")):
        resources[f"{prefix}Stage"] = {
            "Type": "AWS::ApiGateway::Stage",
            "Properties": {
                "DeploymentId": {"Ref": deployment},
                "MethodSettings": [
                    {
                        "HttpMethod": "*",
                        "ResourcePath": "/*",
                        "ThrottlingBurstLimit": 20,
                        "ThrottlingRateLimit": 10,
                    }
                ],
                "RestApiId": {"Ref": api},
                "StageName": "live",
                "TracingEnabled": True,
            },
        }
    for logical, method, path in (
        ("Submit", "POST", "/jobs"),
        ("Status", "GET", "/jobs/*"),
        ("Review", "POST", "/reviews"),
        ("ReviewContext", "GET", "/jobs/*"),
    ):
        resources[f"{logical}Permission"] = {
            "Type": "AWS::Lambda::Permission",
            "Properties": {
                "Action": "lambda:InvokeFunction",
                "FunctionName": {"Ref": f"{logical}Function"},
                "Principal": "apigateway.amazonaws.com",
                "SourceArn": {"Fn::Sub": "arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${ReviewApi}/*/" + method + path},
            },
        }
    definition = sub_escape(ASL_PATH.read_text(encoding="utf-8"))
    substitutions = {}
    for name in PLACEHOLDERS:
        logical = name[: -len("Arn")]
        substitutions[name] = {"Fn::GetAtt": [logical, "Arn"]}
    resources["StateMachineLogGroup"] = {
        "Type": "AWS::Logs::LogGroup",
        "Properties": {"LogGroupName": {"Fn::Sub": "/aws/states/${ProjectName}"}, "RetentionInDays": 30},
    }
    resources["StateMachineRole"] = {
        "Type": "AWS::IAM::Role",
        "Properties": {
            "AssumeRolePolicyDocument": assume("states.amazonaws.com"),
            "Policies": [
                {
                    "PolicyName": "api-review-states",
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Action": ["lambda:InvokeFunction"],
                                "Effect": "Allow",
                                "Resource": [{"Fn::GetAtt": [f"{name[: -len('Arn')]}", "Arn"]} for name in PLACEHOLDERS],
                            },
                            {
                                "Action": [
                                    "logs:CreateLogDelivery",
                                    "logs:DeleteLogDelivery",
                                    "logs:DescribeLogGroups",
                                    "logs:DescribeResourcePolicies",
                                    "logs:GetLogDelivery",
                                    "logs:ListLogDeliveries",
                                    "logs:PutResourcePolicy",
                                    "logs:UpdateLogDelivery",
                                ],
                                "Effect": "Allow",
                                "Resource": "*",
                            },
                            {
                                "Action": ["xray:GetSamplingRules", "xray:GetSamplingTargets", "xray:PutTelemetryRecords", "xray:PutTraceSegments"],
                                "Effect": "Allow",
                                "Resource": "*",
                            },
                        ],
                    },
                }
            ],
        },
    }
    resources["ReviewStateMachine"] = {
        "Type": "AWS::StepFunctions::StateMachine",
        "Properties": {
            "DefinitionString": {"Fn::Sub": [definition, substitutions]},
            "LoggingConfiguration": {
                "Destinations": [{"CloudWatchLogsLogGroup": {"LogGroupArn": {"Fn::GetAtt": ["StateMachineLogGroup", "Arn"]}}}],
                "IncludeExecutionData": False,
                "Level": "ERROR",
            },
            "RoleArn": {"Fn::GetAtt": ["StateMachineRole", "Arn"]},
            "StateMachineName": {"Ref": "ProjectName"},
            "StateMachineType": "STANDARD",
            "TracingConfiguration": {"Enabled": True},
        },
    }
    # Silence unused locals kept for readers of the generator.
    del bucket_arn
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Serverless API breaking-change review gate. Research deployment asset. Not executed in the reference test run.",
        "Parameters": {
            "BedrockModelId": {
                "Default": "us.amazon.nova-2-lite-v1:0",
                "Description": "Bedrock model id used only if BEDROCK_ADVISORY is later changed to enabled.",
                "Type": "String",
            },
            "DeploymentNonce": {
                "Default": "1",
                "Description": "Change this value to replace API Gateway deployments after an API body edit.",
                "Type": "String",
            },
            "LambdaCodeBucket": {"Description": "Bucket that holds the packaged Lambda zip.", "Type": "String"},
            "LambdaCodeKey": {"Description": "Key of the packaged Lambda zip.", "Type": "String"},
            "OtelLayerArn": {
                "Description": "Regional AWS Lambda Layer for OpenTelemetry ARN from the current AWS documentation.",
                "MinLength": 20,
                "Type": "String",
            },
            "ProjectName": {"Default": "api-review", "Type": "String"},
        },
        "Resources": resources,
        "Outputs": {
            "ArtifactBucketName": {"Value": {"Ref": "ArtifactBucket"}},
            "JobTableName": {"Value": {"Ref": "JobTable"}},
            "MockApiEndpoint": {"Value": {"Fn::Sub": "https://${MockApi}.execute-api.${AWS::Region}.amazonaws.com/live"}},
            "ReviewApiEndpoint": {"Value": {"Fn::Sub": "https://${ReviewApi}.execute-api.${AWS::Region}.amazonaws.com/live"}},
            "StateMachineArn": {"Value": {"Ref": "ReviewStateMachine"}},
            "WorkQueueUrl": {"Value": {"Ref": "WorkQueue"}},
        },
    }


def main() -> None:
    OUT_PATH.write_text(json.dumps(build_template(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
