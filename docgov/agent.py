from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Dict, List, Literal

from .engine import RepositorySnapshot, analyze
from .models import Evidence, Finding, GovernanceDecision


SYSTEM_PROMPT = """
You are Doc Governor, a documentation governance agent.
You do not generate documentation for its own sake. You decide whether a changed
Markdown file should be kept, merged into an existing canonical document, marked
stale, or blocked for human review. Use only the repository snapshot and evidence
returned by tools. Never invent dates, source paths, product decisions, legal text,
or production status. Return JSON with a `findings` array. Every finding must use
one of: duplicate, stale, misclassified, unverified_date, orphan, conflict.
""".strip()

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"
DEFAULT_MODEL_TIMEOUT_SECONDS = 60.0
MAX_MODEL_DOCUMENT_CHARS = 12_000
MAX_MODEL_CONTEXT_CHARS = 60_000


def _result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    message = getattr(result, "message", None)
    if isinstance(message, dict):
        content = message.get("content", [])
        if isinstance(content, list):
            text = "".join(item.get("text", "") for item in content if isinstance(item, dict))
            if text:
                return text
    return str(result)


def _json_from_text(value: str) -> Dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", value, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else value[value.find("{") : value.rfind("}") + 1]
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("findings", []), list):
        raise ValueError("Strands response must be an object with a findings array")
    return parsed


def _model_findings(payload: Dict[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    allowed_kinds = {"duplicate", "stale", "misclassified", "unverified_date", "orphan", "conflict"}
    allowed_actions = {"update", "merge_new_file", "mark_stale", "block", "noop"}
    for item in payload.get("findings", []):
        if not isinstance(item, dict):
            raise ValueError("Each model finding must be an object")
        kind = str(item.get("kind", ""))
        action = str(item.get("action", "block"))
        risk = str(item.get("risk", "high"))
        if kind not in allowed_kinds or action not in allowed_actions or risk not in {"low", "high"}:
            raise ValueError("Model returned an unsupported finding contract")
        evidence = [Evidence(path=str(path)) for path in item.get("evidence", []) if isinstance(path, str)]
        findings.append(Finding(
            kind=kind,
            risk=risk,
            action=action,
            documents=[str(path) for path in item.get("documents", [])],
            reason=str(item.get("reason", "No reason supplied")),
            evidence=evidence,
            proposed_patch=item.get("proposed_patch"),
            human_decision=item.get("human_decision"),
        ))
    return findings


def _bounded_documents(snapshot: RepositorySnapshot) -> Dict[str, str]:
    """Return only PR-relevant Markdown, capped before it reaches the model."""
    candidates: List[str] = []
    for path in snapshot.changed + snapshot.added:
        if path.endswith(".md") and path in snapshot.files and path not in candidates:
            candidates.append(path)
    changed_types = {
        snapshot.catalog.classify(path)
        for path in candidates
        if snapshot.catalog.classify(path)
    }
    for path in sorted(snapshot.files):
        if snapshot.catalog.classify(path) in changed_types and path not in candidates:
            candidates.append(path)

    result: Dict[str, str] = {}
    remaining = MAX_MODEL_CONTEXT_CHARS
    for path in candidates:
        if remaining <= 0:
            break
        content = snapshot.files[path]
        excerpt = content[: min(MAX_MODEL_DOCUMENT_CHARS, remaining)]
        result[path] = excerpt
        remaining -= len(excerpt)
    return result


def invoke_strands(snapshot: RepositorySnapshot, baseline: GovernanceDecision, model_id: str | None = None) -> GovernanceDecision:
    try:
        from strands import Agent, tool  # type: ignore
        from strands.models import BedrockModel  # type: ignore
        from pydantic import BaseModel, Field  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Strands is not installed; run with DOCGOV_ENABLE_MODEL=0 for offline checks") from exc

    class FindingOutput(BaseModel):
        kind: Literal["duplicate", "stale", "misclassified", "unverified_date", "orphan", "conflict"]
        risk: Literal["low", "high"]
        action: Literal["update", "merge_new_file", "mark_stale", "block", "noop"]
        documents: List[str] = Field(default_factory=list)
        reason: str
        evidence: List[str] = Field(default_factory=list)
        proposed_patch: str | None = None
        human_decision: str | None = None

    class GovernanceOutput(BaseModel):
        findings: List[FindingOutput] = Field(default_factory=list)

    @tool
    def repository_snapshot() -> str:
        """Return the bounded repository facts and deterministic findings."""
        return json.dumps({
            "changed_paths": snapshot.changed,
            "added_paths": snapshot.added,
            "documents": _bounded_documents(snapshot),
            "catalog": [record.to_dict() for record in snapshot.catalog.documents],
            "supabase": snapshot.supabase,
            "baseline_findings": [item.to_dict() for item in baseline.findings],
        }, ensure_ascii=False)

    model_kwargs: Dict[str, Any] = {
        "model_id": model_id or DEFAULT_MODEL_ID,
        "region_name": os.environ.get("AWS_REGION", "us-west-2"),
    }
    model = BedrockModel(**model_kwargs)
    agent = Agent(
        model=model,
        tools=[repository_snapshot],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
    )
    prompt = (
        "Call repository_snapshot exactly once, then review only those bounded facts. "
        "Preserve deterministic findings unless evidence proves they are wrong. "
        "Return JSON only in the form {\"findings\":[{\"kind\":...,\"risk\":...,\"action\":...,"
        "\"documents\":[...],\"reason\":...,\"evidence\":[...],\"human_decision\":...}]} ."
    )
    cancel_signal = threading.Event()
    timeout_seconds = float(os.environ.get("DOCGOV_MODEL_TIMEOUT_SECONDS", DEFAULT_MODEL_TIMEOUT_SECONDS))
    timer = threading.Timer(timeout_seconds, cancel_signal.set)
    timer.daemon = True
    timer.start()
    try:
        result = agent(
            prompt,
            structured_output_model=GovernanceOutput,
            cancel_signal=cancel_signal,
        )
    finally:
        timer.cancel()
    if cancel_signal.is_set():
        raise TimeoutError(f"Strands model invocation exceeded {timeout_seconds:g} seconds")
    structured = getattr(result, "structured_output", None)
    if structured is not None:
        if hasattr(structured, "model_dump"):
            payload = structured.model_dump()
        elif hasattr(structured, "dict"):
            payload = structured.dict()
        elif isinstance(structured, dict):
            payload = structured
        else:
            raise ValueError("Strands returned an unsupported structured output object")
    else:
        # Older Strands versions may expose only text; still validate the same
        # JSON contract before considering the model result.
        payload = _json_from_text(_result_text(result))
    model_findings = _model_findings(payload)
    # Model output can add context, but it cannot downgrade deterministic high-risk findings.
    deterministic_keys = {(item.kind, tuple(item.documents)) for item in baseline.findings}
    findings = list(baseline.findings)
    for finding in model_findings:
        key = (finding.kind, tuple(finding.documents))
        if key not in deterministic_keys:
            findings.append(finding)
    result = "action_required" if any(item.risk == "high" for item in findings) else ("changed" if findings else "pass")
    return GovernanceDecision(
        run_id=baseline.run_id,
        mode=baseline.mode,
        result=result,
        changed=False,
        findings=findings,
        head_sha=baseline.head_sha,
        model_used=True,
    )


def govern(snapshot: RepositorySnapshot, mode: str, run_id: str | None = None, enable_model: bool = False, model_id: str | None = None) -> GovernanceDecision:
    baseline = analyze(snapshot, mode=mode, run_id=run_id)
    if not enable_model:
        return baseline
    try:
        return invoke_strands(snapshot, baseline, model_id=model_id)
    except Exception as exc:
        return GovernanceDecision(
            run_id=baseline.run_id,
            mode=baseline.mode,
            result="blocked",
            changed=False,
            findings=baseline.findings,
            head_sha=baseline.head_sha,
            model_used=True,
            error=f"Strands model failed closed: {exc}",
        )
