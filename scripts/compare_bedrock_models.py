"""Compare Bedrock Converse models on one findings file.

Run this in CloudShell with credentials that may call bedrock-runtime:Converse.
The reference test suite does not import boto3. Model text is not a verdict.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from apireview.advisory import build_converse_request, parse_converse_response

DEFAULT_MODELS = (
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.meta.llama3-3-70b-instruct-v1:0",
    "us.mistral.pixtral-large-2502-v1:0",
    "us.amazon.nova-2-lite-v1:0",
)
CAPTURED_CODES = {
    "AccessDeniedException",
    "ValidationException",
    "ResourceNotFound",
    "ResourceNotFoundException",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare Bedrock Converse models on review findings.")
    parser.add_argument("--region", required=True)
    parser.add_argument("--findings", default="")
    parser.add_argument("--report", default="~/broken-report.json")
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", default="build/model-comparison.json")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    findings = load_findings(args.findings, args.report)
    models = [item.strip() for item in args.models.split(",") if item.strip()]
    import boto3

    client = boto3.client("bedrock-runtime", region_name=args.region)
    document = compare_models(client, models, findings, args.runs, args.region)
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(render_markdown(document))
    print(f"wrote {destination}")
    return 0


def load_findings(findings_path: str, report_path: str) -> list[dict[str, Any]]:
    if findings_path:
        data = json.loads(Path(findings_path).expanduser().read_text(encoding="utf-8"))
    else:
        path = Path(report_path).expanduser()
        if not path.is_file():
            raise SystemExit(f"findings file is missing: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("findings"), list):
            return data["findings"]
        evidence = data.get("evidence") or {}
        if isinstance(evidence.get("findings"), list):
            return evidence["findings"]
    raise SystemExit("findings JSON must be a list, or a report with evidence.findings")


def compare_models(client: Any, models: list[str], findings: list[dict[str, Any]], runs: int, region: str) -> dict[str, Any]:
    return {
        "models": [compare_model(client, model_id, findings, runs) for model_id in models],
        "region": region,
        "runs": runs,
    }


def compare_model(client: Any, model_id: str, findings: list[dict[str, Any]], runs: int) -> dict[str, Any]:
    request = build_converse_request(model_id, findings)
    folded = False
    rows: list[dict[str, Any]] = []
    for _ in range(runs):
        row, request, folded_now = one_run(client, request, folded)
        folded = folded or folded_now
        rows.append(row)
    parsed = [row for row in rows if row["parseOk"]]
    latencies = [row["latencyMs"] for row in rows if row["latencyMs"] is not None]
    inputs = [row["inputTokens"] for row in rows if row["inputTokens"] is not None]
    outputs = [row["outputTokens"] for row in rows if row["outputTokens"] is not None]
    first = parsed[0] if parsed else None
    return {
        "firstSuggestedFix": None if first is None else first["suggestedFix"],
        "firstSummary": None if first is None else first["summary"],
        "medianInputTokens": _median(inputs),
        "medianLatencyMs": _median(latencies),
        "medianOutputTokens": _median(outputs),
        "modelId": model_id,
        "okRuns": sum(1 for row in rows if row["error"] is None),
        "parseOk": len(parsed),
        "runs": rows,
        "systemFolded": folded,
        "totalRuns": runs,
    }


def one_run(client: Any, request: dict[str, Any], folded: bool) -> tuple[dict[str, Any], dict[str, Any], bool]:
    started = time.perf_counter()
    try:
        response = client.converse(**request)
    except Exception as exc:
        captured = captured_error(exc)
        if captured and captured["code"] == "ValidationException" and not folded and "system" in request:
            folded_request = fold_system_into_user(request)
            return one_run(client, folded_request, True)
        latency = round((time.perf_counter() - started) * 1000, 3)
        if captured is None:
            captured = {"code": type(exc).__name__, "message": str(exc)}
        return _error_row(latency, captured), request, folded
    latency = round((time.perf_counter() - started) * 1000, 3)
    parsed = parse_converse_response(response)
    summary, suggested = advisory_text(parsed)
    usage = response.get("usage") or {}
    metrics = response.get("metrics") or {}
    return (
        {
            "error": None,
            "inputTokens": usage.get("inputTokens"),
            "latencyMs": latency,
            "outputTokens": usage.get("outputTokens"),
            "parseOk": parsed.get("modelStatus") == "parsed",
            "serviceLatencyMs": metrics.get("latencyMs"),
            "stopReason": response.get("stopReason"),
            "suggestedFix": suggested,
            "summary": summary,
        },
        request,
        folded,
    )


def fold_system_into_user(request: dict[str, Any]) -> dict[str, Any]:
    folded = json.loads(json.dumps(request))
    system = folded.pop("system", None) or []
    instruction = "\n".join(str(block.get("text") or "") for block in system if isinstance(block, dict)).strip()
    message = folded["messages"][0]["content"][0]
    if instruction:
        message["text"] = instruction + "\n" + str(message.get("text") or "")
    return folded


def captured_error(exc: BaseException) -> dict[str, str] | None:
    response = getattr(exc, "response", None)
    code = ""
    message = str(exc)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        code = str(error.get("Code") or "")
        message = str(error.get("Message") or message)
    if code not in CAPTURED_CODES and type(exc).__name__ not in CAPTURED_CODES:
        return None
    return {"code": code or type(exc).__name__, "message": message}


def advisory_text(parsed: dict[str, Any]) -> tuple[str, str]:
    explanations = parsed.get("explanations") or []
    proposals = parsed.get("proposals") or []
    summary = "; ".join(_piece(item, "explanation") for item in explanations if isinstance(item, dict))
    suggested = "; ".join(_piece(item, "description") for item in proposals if isinstance(item, dict))
    return summary, suggested


def _piece(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    return str(value if value is not None else item)


def _error_row(latency: float, error: dict[str, str]) -> dict[str, Any]:
    return {
        "error": error,
        "inputTokens": None,
        "latencyMs": latency,
        "outputTokens": None,
        "parseOk": False,
        "serviceLatencyMs": None,
        "stopReason": None,
        "suggestedFix": "",
        "summary": "",
    }


def _median(values: list[float | int]) -> float | None:
    if not values:
        return None
    return statistics.median(values)


def render_markdown(document: dict[str, Any]) -> str:
    lines = [
        "| model | ok runs/total | median latency ms | median input/output tokens | parse ok |",
        "| --- | --- | --- | --- | --- |",
    ]
    for model in document["models"]:
        lines.append(
            "| {model} | {ok}/{total} | {latency} | {tokens} | {parsed}/{total} |".format(
                model=model["modelId"],
                ok=model["okRuns"],
                total=model["totalRuns"],
                latency=_cell(model["medianLatencyMs"]),
                tokens=f"{_cell(model['medianInputTokens'])}/{_cell(model['medianOutputTokens'])}",
                parsed=model["parseOk"],
            )
        )
    lines.append("")
    lines.append("## First advisory")
    for model in document["models"]:
        lines.append(f"### {model['modelId']}")
        if model["systemFolded"]:
            lines.append("System prompt was folded into the user message after ValidationException.")
        if model["firstSummary"]:
            lines.append(model["firstSummary"])
            if model["firstSuggestedFix"]:
                lines.append(model["firstSuggestedFix"])
        else:
            lines.append("(no parsed advisory)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _cell(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
