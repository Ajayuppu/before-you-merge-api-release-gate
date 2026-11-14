"""Model comparison uses a fake Converse client. No AWS calls."""

from __future__ import annotations

import json

from scripts.compare_bedrock_models import compare_model, fold_system_into_user, render_markdown


class _Error(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


FINDINGS = [
    {
        "id": "1",
        "message": "amount type changed",
        "path": "/components/schemas/Order/properties/amount/type",
        "rule": "response.type.changed",
        "severity": "breaking",
    }
]


def test_validation_exception_folds_system_prompt_and_parses():
    class Client:
        def __init__(self) -> None:
            self.saw_system = False
            self.saw_folded = False

        def converse(self, **kwargs):
            if "system" in kwargs:
                self.saw_system = True
                raise _Error("ValidationException", "system is not supported")
            self.saw_folded = True
            text = json.dumps(
                {
                    "explanations": [{"explanation": "amount became a string"}],
                    "proposals": [{"description": "restore the number type"}],
                }
            )
            return {
                "metrics": {"latencyMs": 20},
                "output": {"message": {"content": [{"text": text}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 11, "outputTokens": 7},
            }

    client = Client()
    result = compare_model(client, "us.meta.llama3-3-70b-instruct-v1:0", FINDINGS, runs=2)
    assert client.saw_system is True
    assert client.saw_folded is True
    assert result["systemFolded"] is True
    assert result["okRuns"] == 2
    assert result["parseOk"] == 2
    assert result["firstSummary"] == "amount became a string"
    assert result["firstSuggestedFix"] == "restore the number type"
    assert "system" not in result["runs"][1]
    folded = fold_system_into_user({"messages": [{"content": [{"text": "prompt"}], "role": "user"}], "system": [{"text": "rules"}]})
    assert "system" not in folded
    assert folded["messages"][0]["content"][0]["text"].startswith("rules\n")
    table = render_markdown({"models": [result]})
    assert "ok runs/total" in table
    assert "2/2" in table
    assert "amount became a string" in table


def test_access_denied_and_missing_model_are_recorded():
    class Denied:
        def converse(self, **kwargs):
            raise _Error("AccessDeniedException", "not allowed")

    class Missing:
        def converse(self, **kwargs):
            raise _Error("ResourceNotFoundException", "model is missing")

    denied = compare_model(Denied(), "us.amazon.nova-2-lite-v1:0", FINDINGS, runs=2)
    assert denied["okRuns"] == 0
    assert denied["parseOk"] == 0
    assert denied["runs"][0]["error"]["code"] == "AccessDeniedException"
    assert denied["firstSummary"] is None
    missing = compare_model(Missing(), "us.mistral.pixtral-large-2502-v1:0", FINDINGS, runs=1)
    assert missing["runs"][0]["error"]["code"] == "ResourceNotFoundException"
    assert "no parsed advisory" in render_markdown({"models": [denied, missing]})
