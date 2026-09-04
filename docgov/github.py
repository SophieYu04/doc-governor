from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote
from typing import Any, Dict, Optional

from .models import Evidence, Finding, GovernanceDecision


MARKER = "<!-- doc-governor-report -->"


def report_markdown(decision: GovernanceDecision) -> str:
    lines = [MARKER, "## Doc Governor", "", f"**Result:** `{decision.result}`", ""]
    if decision.error:
        lines.extend([f"**Error:** {decision.error}", ""])
    if not decision.findings and not decision.error:
        lines.append("No documentation drift was found.")
    elif decision.findings:
        lines.extend(["| Risk | Finding | Action | Documents |", "| --- | --- | --- | --- |"])
        for finding in decision.findings:
            lines.append(
                f"| `{finding.risk}` | `{finding.kind}` | `{finding.action}` | "
                f"{', '.join(f'`{item}`' for item in finding.documents)} |"
            )
            lines.append(f"\n> {finding.reason}")
            if finding.evidence:
                evidence = ", ".join(
                    f"`{item.path}`" + (f" ({item.sha256[:12]})" if item.sha256 else "")
                    for item in finding.evidence
                )
                lines.append(f"\nEvidence: {evidence}")
            if finding.human_decision:
                lines.append(f"\n**Human decision required:** {finding.human_decision}")
    lines.extend(["", f"Run: `{decision.run_id}`", "", "<!-- /doc-governor-report -->"])
    return "\n".join(lines)


def _request(method: str, url: str, token: str, payload: Optional[Dict[str, Any]] = None) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read().decode("utf-8")
        return json.loads(payload) if payload else None


def upsert_pr_comment(decision: GovernanceDecision, repository: str, issue_number: str, token: str) -> None:
    base = f"https://api.github.com/repos/{repository}/issues/{issue_number}/comments"
    body = {"body": report_markdown(decision)}
    comments = _request("GET", base, token)
    existing = next(
        (item for item in comments if MARKER in str(item.get("body", "")) and item.get("user", {}).get("type") == "Bot"),
        None,
    )
    if existing:
        _request("PATCH", f"{base}/{existing['id']}", token, body)
    else:
        _request("POST", base, token, body)


def remove_pr_label(repository: str, issue_number: str, label: str, token: str) -> None:
    """Remove a one-time approval label after a successful governed run."""
    encoded = quote(label, safe="")
    url = f"https://api.github.com/repos/{repository}/issues/{issue_number}/labels/{encoded}"
    _request("DELETE", url, token)


def upsert_check_run(decision: GovernanceDecision, repository: str, head_sha: str, token: str) -> None:
    """Publish one completed, repeatable Check for the governed head SHA."""
    if not head_sha:
        return
    base = f"https://api.github.com/repos/{repository}/check-runs"
    query = f"{base}?check_name={quote('Doc Governor', safe='')}&head_sha={quote(head_sha, safe='')}"
    existing_payload = _request("GET", query, token) or {}
    existing = next(iter(existing_payload.get("check_runs", [])), None)
    conclusion = "success" if decision.result in {"pass", "changed"} else "action_required"
    output = report_markdown(decision)
    payload: Dict[str, Any] = {
        "name": "Doc Governor",
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "output": {
            "title": f"Doc Governor: {decision.result}",
            "summary": output[:65000],
        },
    }
    if existing:
        _request("PATCH", f"{base}/{existing['id']}", token, payload)
    else:
        _request("POST", base, token, payload)


def event_context() -> Dict[str, str]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        return {}
    with open(event_path, encoding="utf-8") as handle:
        event = json.load(handle)
    pull_request = event.get("pull_request", {})
    return {
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "issue_number": str(pull_request.get("number", "")),
        "head_sha": str(pull_request.get("head", {}).get("sha", "")),
        "action": str(event.get("action", "")),
        "label": str(event.get("label", {}).get("name", "")),
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="docgov-github-report")
    parser.add_argument("--result", required=True)
    parser.add_argument("--remove-approved-label", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    context = event_context()
    if not token or not context.get("repository") or not context.get("issue_number"):
        return 0
    with open(args.result, encoding="utf-8") as handle:
        payload = json.load(handle)
    findings = []
    for item in payload.get("findings", []):
        evidence = [
            Evidence(
                path=str(value.get("path", "")),
                kind=str(value.get("kind", "source")),
                sha256=value.get("sha256"),
                detail=value.get("detail"),
            )
            for value in item.get("evidence", [])
            if isinstance(value, dict) and value.get("path")
        ]
        findings.append(Finding(
            kind=item["kind"],
            risk=item["risk"],
            action=item["action"],
            documents=item.get("documents", []),
            reason=item.get("reason", ""),
            evidence=evidence,
            human_decision=item.get("human_decision"),
        ))
    decision = GovernanceDecision(
        run_id=payload.get("run_id", "unknown"),
        mode=payload.get("mode", "review"),
        result=payload.get("result", "blocked"),
        changed=bool(payload.get("changed", False)),
        findings=findings,
        modified_paths=payload.get("modified_paths", []),
        head_sha=payload.get("head_sha"),
        model_used=bool(payload.get("model_used", False)),
        error=payload.get("error"),
    )
    try:
        upsert_check_run(decision, context["repository"], context.get("head_sha", ""), token)
    except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
        print(f"Doc Governor Check update failed: {exc}")
    try:
        upsert_pr_comment(decision, context["repository"], context["issue_number"], token)
        if (
            args.remove_approved_label
            and context.get("action") == "labeled"
            and context.get("label") == "docgov-approved"
            and decision.result in {"pass", "changed"}
        ):
            remove_pr_label(context["repository"], context["issue_number"], "docgov-approved", token)
    except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
        print(f"Doc Governor comment update failed: {exc}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
