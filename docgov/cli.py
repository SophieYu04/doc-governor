from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from .agent import govern
from .catalog import Catalog
from .engine import analyze_trust, apply_safe_actions, build_snapshot, record_baseline
from .git_tools import tracked_paths
from .ledger import utc_now
from .models import DocumentRecord, GovernanceDecision


def _catalog_path(root: Path, value: str | None) -> Path:
    return root / (value or ".docgov/catalog.yaml")


def _ledger_path(root: Path, value: str | None) -> Path:
    return root / (value or ".docgov/ledger.jsonl")


def _init_catalog(root: Path, path: Path) -> Dict[str, Any]:
    catalog = Catalog.default()
    markdown_paths = tracked_paths(root, suffix=".md")
    if not markdown_paths:
        markdown_paths = [path.relative_to(root).as_posix() for path in root.rglob("*.md")]
    for relative in sorted(set(markdown_paths)):
        parts = Path(relative).parts
        if any(part in {".git", ".venv", "node_modules", "vendor"} for part in parts):
            continue
        document_type = catalog.classify(relative) or _suggest_type(relative)
        catalog.documents.append(DocumentRecord(
            path=relative,
            type=document_type,
            status="review_required",
            approval="human",
        ))
    return catalog.to_dict()


def _suggest_type(relative_path: str) -> str:
    value = relative_path.lower()
    name = Path(value).name
    if "/evidence/" in f"/{value}" or "evidence" in name:
        return "evidence"
    if "/decisions/" in f"/{value}" or "/adr/" in f"/{value}" or name.startswith("adr-"):
        return "decision"
    if any(token in value for token in ("/status/", "current_release", "release_state")):
        return "state"
    if any(token in value for token in ("/operations/", "runbook", "playbook", "checklist", "testing", "setup")):
        return "procedure"
    return "contract"


def _print(decision: GovernanceDecision, as_json: bool) -> None:
    if as_json:
        print(json.dumps(decision.to_dict(), ensure_ascii=False, sort_keys=True))
        return
    print(f"Doc Governor: {decision.result} ({len(decision.findings)} finding(s))")
    for finding in decision.findings:
        print(f"- [{finding.risk}] {finding.kind}: {finding.reason}")
        if finding.documents:
            print(f"  documents: {', '.join(finding.documents)}")


def _exit_code(decision: GovernanceDecision) -> int:
    return 2 if decision.result in {"action_required", "blocked"} else 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docgov")
    parser.add_argument("--root", default=".")
    parser.add_argument("--catalog", default=None)
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--json", action="store_true", dest="as_json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--apply", action="store_true")
    init_parser.add_argument("--json", action="store_true", dest="sub_json")

    for command in ("review", "audit", "verify"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--base", default=None)
        command_parser.add_argument("--head", default=None)
        command_parser.add_argument("--apply", action="store_true")
        command_parser.add_argument("--json", action="store_true", dest="sub_json")
        command_parser.add_argument(
            "--approved",
            action="store_true",
            help="Apply the narrow action authorized by the docgov-approved label",
        )
        command_parser.add_argument(
            "--enable-model",
            action="store_true",
            default=os.environ.get("DOCGOV_ENABLE_MODEL", "").lower() in {"1", "true", "yes"},
        )
        command_parser.add_argument("--model-id", default=os.environ.get("DOCGOV_MODEL_ID"))

    verify_parser = subparsers.choices["verify"]
    verify_parser.add_argument("--strict", action="store_true")
    verify_parser.add_argument("--ref", default=None, help="Read governed content from this Git ref")
    verify_parser.add_argument("paths", nargs="*", help="Tracked Markdown paths to verify")

    baseline_parser = subparsers.add_parser("baseline")
    baseline_parser.add_argument("--approved", action="store_true", required=True)
    baseline_parser.add_argument("--ref", default=None)
    baseline_parser.add_argument("--json", action="store_true", dest="sub_json")
    baseline_parser.add_argument("paths", nargs="*")

    args = parser.parse_args(argv)
    args.as_json = bool(args.as_json or getattr(args, "sub_json", False))
    root = Path(args.root).resolve()
    catalog_path = _catalog_path(root, args.catalog)
    ledger_path = _ledger_path(root, args.ledger)

    if args.command == "init":
        value = _init_catalog(root, catalog_path)
        changed = False
        if args.apply:
            # Initialization is safe to run repeatedly: never overwrite a
            # reviewed Catalog that already exists.
            if not catalog_path.exists():
                catalog_path.parent.mkdir(parents=True, exist_ok=True)
                Catalog(value).save(catalog_path)
                changed = True
        print(json.dumps({
            "result": "changed" if changed else "pass",
            "changed": changed,
            "finding_count": 0,
            "proposal_only": not args.apply,
            "catalog": value,
        }, ensure_ascii=False, sort_keys=True))
        return 0

    mode = "review" if args.command == "review" else "audit"
    if args.command == "verify":
        mode = "review"
    try:
        snapshot = build_snapshot(
            root,
            catalog_path,
            base=getattr(args, "base", None),
            head=getattr(args, "head", None),
            ledger_path=ledger_path,
            source_ref=getattr(args, "ref", None),
        )
    except Exception as exc:
        decision = GovernanceDecision(
            run_id=f"unknown-{mode}",
            mode=mode,
            result="blocked",
            changed=False,
            error=f"Unable to load governance inputs: {exc}",
        )
        _print(decision, args.as_json)
        return _exit_code(decision)
    if args.command == "baseline":
        decision = record_baseline(
            snapshot,
            ledger_path,
            approved=args.approved,
            documents=args.paths or None,
        )
        _print(decision, args.as_json)
        return _exit_code(decision)

    if args.command == "verify" and args.strict:
        decision = analyze_trust(
            snapshot,
            ledger_path,
            requested_paths=args.paths or None,
        )
        _print(decision, args.as_json)
        return _exit_code(decision)

    try:
        decision = govern(
            snapshot,
            mode=mode,
            run_id=f"{snapshot.head_sha}-{mode}",
            enable_model=args.enable_model and args.command != "verify",
            model_id=args.model_id,
        )
    except Exception as exc:  # model failures must never mutate a repository
        decision = GovernanceDecision(
            run_id=f"{snapshot.head_sha}-{mode}",
            mode=mode,
            result="blocked",
            changed=False,
            head_sha=snapshot.head_sha,
            error=str(exc),
        )
    if args.apply and args.command != "verify" and decision.result != "blocked":
        decision = apply_safe_actions(
            snapshot,
            decision,
            catalog_path,
            ledger_path,
            approved=args.approved,
        )
    _print(decision, args.as_json)
    return _exit_code(decision)


if __name__ == "__main__":
    raise SystemExit(main())
