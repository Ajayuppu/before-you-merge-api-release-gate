"""Parser cases for model replies. No AWS calls."""

from __future__ import annotations

import json

from apireview.advisory import merge_advisory, parse_converse_response
from apireview.evidence import authoritative_verdict


def _reply(text: str) -> dict:
    return {"output": {"message": {"content": [{"text": text}]}}}


def test_fenced_json_parses():
    text = '```json\n{"explanations":[{"explanation":"type changed"}],"proposals":[{"description":"restore number"}]}\n```'
    parsed = parse_converse_response(_reply(text))
    assert parsed["modelStatus"] == "parsed"
    assert parsed["error"] is None
    assert parsed["rejection"] is None
    assert parsed["explanations"][0]["explanation"] == "type changed"
    assert parsed["authoritative"] is False
    assert parsed["includedInVerdict"] is False


def test_prose_wrapped_json_parses():
    text = 'Here is the result:\n{"explanations":[{"explanation":"field removed"}],"proposals":[{"description":"restore the field"}]}\nI hope that helps.'
    parsed = parse_converse_response(_reply(text))
    assert parsed["modelStatus"] == "parsed"
    assert parsed["explanations"][0]["explanation"] == "field removed"
    assert parsed["proposals"][0]["description"] == "restore the field"


def test_single_object_proposals_are_wrapped():
    text = json.dumps(
        {
            "explanation": "amount type changed",
            "proposals": {"description": "restore number"},
            "note": "ignored extra key",
        }
    )
    parsed = parse_converse_response(_reply(text))
    assert parsed["modelStatus"] == "parsed"
    assert parsed["explanations"] == ["amount type changed"]
    assert parsed["proposals"] == [{"description": "restore number"}]
    assert "note" not in json.dumps(parsed["explanations"]) 


def test_forbidden_content_is_rejected_and_error_matches_rejection():
    text = json.dumps({"explanations": [{"explanation": "see https://example.invalid"}], "proposals": []})
    parsed = parse_converse_response(_reply(text))
    assert parsed["modelStatus"] == "rejected"
    assert parsed["error"] == parsed["rejection"]
    assert parsed["error"] == "model output contained forbidden content"
    merged = merge_advisory(
        {
            "source": "deterministic-local",
            "authoritative": False,
            "includedInVerdict": False,
            "explanations": [{"explanation": "local"}],
            "proposals": [],
            "modelStatus": "not-called",
        },
        parsed,
    )
    assert merged["error"] == merged["modelRejection"] == "model output contained forbidden content"
    assert merged["source"] == "deterministic-local"
    assert merged["includedInVerdict"] is False
    assert authoritative_verdict([{"severity": "breaking"}], {"complete": True, "passed": True, "cases": [{"id": "c"}]}) == "BLOCKED"
    assert authoritative_verdict([], {"complete": True, "passed": True, "cases": [{"id": "c"}]}) == "PASS"
