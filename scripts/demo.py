"""End-to-end demo: the write path corrects, and the read path refuses.

The whole point of Doc Governor is the *read*. A pull-request gate that tidies
documentation is unremarkable; a supply layer that will not hand a coding agent a
document whose claims no longer hold is the thing worth showing. So this script
runs the governor and then goes on to prove three refusals:

* step 3 — a document with no evidence is refused, with a pointer to the source;
* step 4 — a *previously trusted* document is refused seconds later, purely
  because a dependency file changed and the fingerprint recheck caught it, with
  no Doc Governor run in between;
* step 5 — a production environment that drifted from Git makes every document
  describing production unreadable.

Step 4 is the most persuasive moment: nothing re-ran, and the answer still changed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docgov.mcp_server import DocumentSupply, SupplyConfig  # noqa: E402
from docgov.supabase_remote import build_evidence_snapshot, write_evidence  # noqa: E402
from docgov.trust_state import DEFAULT_TRUST_STATE_PATH  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def commit(root: Path, message: str) -> str:
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=root, check=True)
    return git(root, "rev-parse", "HEAD")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def docgov(destination: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "docgov", "--root", str(destination), "--json", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def supply_for(destination: Path) -> DocumentSupply:
    """The MCP read path.

    ``docgov-mcp`` is a thin stdio adapter over exactly this object, so reading
    through it here exercises the same refusal logic a coding agent would hit,
    without requiring the optional MCP SDK. Pass --mcp-stdio to drive the real
    server over a stdio session instead.
    """
    return DocumentSupply(SupplyConfig(
        root=destination,
        trust_state_path=destination / DEFAULT_TRUST_STATE_PATH,
        catalog_path=destination / ".docgov/catalog.yaml",
    ))


def _lint(name: str, categories: List[str], entity: str) -> Dict[str, Any]:
    return {
        "name": name,
        "level": "ERROR",
        "facing": "EXTERNAL",
        "categories": categories,
        "metadata": {"schema": "public", "name": entity},
        "cache_key": f"{name}_{entity}",
    }


def seed_environment_evidence(destination: Path) -> None:
    """Record staging and production Advisor evidence that agree with each other."""
    shared = [_lint("auth_rls_disabled", ["SECURITY"], "public.profiles")]
    for environment, observed_at in (
        ("staging", "2026-09-05T00:00:00Z"),
        ("production", "2026-09-05T00:01:00Z"),
    ):
        write_evidence(destination, build_evidence_snapshot(
            environment,
            f"{environment}refdemo0001",
            {"security": shared, "performance": []},
            observed_at=observed_at,
        ))


def drift_production(destination: Path) -> None:
    """Simulate someone changing production from the dashboard, outside the release flow."""
    production_only = _lint("unindexed_foreign_keys", ["PERFORMANCE"], "public.orders")
    write_evidence(destination, build_evidence_snapshot(
        "production",
        "productionrefdemo0001",
        {
            "security": [_lint("auth_rls_disabled", ["SECURITY"], "public.profiles")],
            "performance": [production_only],
        },
        observed_at="2026-09-06T00:00:00Z",
    ))


def run_demo(destination: Path, *, enable_model: bool = False, mcp_stdio: bool = False) -> Dict[str, Any]:
    source = Path(__file__).resolve().parents[1] / "examples" / "supabase-demo"
    shutil.copytree(source, destination, dirs_exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=destination, check=True)
    git(destination, "config", "user.name", "Doc Governor Demo")
    git(destination, "config", "user.email", "demo@example.com")

    # The production status document starts out genuinely verified, so step 5
    # shows it *losing* trust rather than never having had any.
    catalog_path = destination / ".docgov/catalog.yaml"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    for record in catalog["documents"]:
        if record["path"] == "docs/status/PRODUCTION.md":
            record["last_verified_at"] = utc_now()
    catalog_path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    public_doc = destination / "docs" / "public" / "ANNOUNCEMENT.md"
    base = commit(destination, "demo baseline")
    seed_environment_evidence(destination)
    baseline = docgov(destination, "baseline", "--approved", "docs/status/PRODUCTION.md")
    if baseline.returncode != 0:
        raise RuntimeError(f"Could not record the production baseline: {baseline.stdout}{baseline.stderr}")
    commit(destination, "record the production status baseline")

    # --- step 1: a coding agent drifts the documentation -------------------
    function = destination / "supabase" / "functions" / "send-email" / "index.ts"
    function.parent.mkdir(parents=True, exist_ok=True)
    function.write_text("export default {};\n", encoding="utf-8")
    config = destination / "supabase" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8") + "\n[functions.send-email]\nverify_jwt = false\n",
        encoding="utf-8",
    )
    shutil.copy2(
        destination / "docs" / "architecture" / "API.md",
        destination / "docs" / "architecture" / "API-notes.md",
    )
    release = destination / "docs" / "status" / "RELEASE.md"
    release.write_text(
        release.read_text(encoding="utf-8").replace("2026-09-03", "2026-09-05"),
        encoding="utf-8",
    )
    public_doc.write_text("# Announcement\n\nUnreviewed public claim.\n", encoding="utf-8")
    head = commit(destination, "simulate coding-agent documentation drift")

    # --- step 2: the governor rules and writes the trust table -------------
    args = ["review", "--base", base, "--head", head, "--apply"]
    if enable_model:
        args.append("--enable-model")
    completed = docgov(destination, *args)
    decision = json.loads(completed.stdout)
    if completed.returncode != 2 or decision.get("result") != "action_required":
        raise RuntimeError(f"Unexpected demo decision: {completed.stdout}\n{completed.stderr}")
    if enable_model and not decision.get("model_used"):
        raise RuntimeError(f"Strands was not used by the model-enabled demo: {completed.stdout}")
    if (destination / "docs" / "architecture" / "API-notes.md").exists():
        raise RuntimeError("Safe duplicate was not removed")
    for path in ("docs/architecture/API.md", "docs/architecture/EDGE_FUNCTIONS.md"):
        if '"send-email"' not in (destination / path).read_text(encoding="utf-8"):
            raise RuntimeError(f"Supabase inventory was not synchronized in {path}")
    if "Unreviewed public claim." not in public_doc.read_text(encoding="utf-8"):
        raise RuntimeError("Protected public copy was unexpectedly rewritten")
    if not (destination / DEFAULT_TRUST_STATE_PATH).exists():
        raise RuntimeError("The trust state was not written")
    commit(destination, "apply Doc Governor correction")

    reads: List[Dict[str, Any]] = []

    def read(supply: DocumentSupply, path: str, label: str) -> Dict[str, Any]:
        response = supply.get_document(path)
        if response.get("content") is not None and response.get("status") != "ok":
            raise RuntimeError(f"A refused document leaked content: {path}")
        reads.append({
            "step": label,
            "path": path,
            "status": response["status"],
            "code": response.get("code"),
            "served_characters": len(response.get("content") or ""),
            "reason": response.get("reason"),
            "read_instead": response.get("read_instead", []),
            "canonical_path": response.get("canonical_path"),
        })
        return response

    # --- step 3: two reads through the supply layer ------------------------
    supply = supply_for(destination)
    if read(supply, "docs/architecture/API.md", "3-corrected-contract")["status"] != "ok":
        raise RuntimeError("The corrected contract should be readable")
    if read(supply, "docs/status/RELEASE.md", "3-unverified-state")["status"] == "ok":
        raise RuntimeError("A release note with no evidence must not be served")
    if read(supply, "docs/architecture/API-notes.md", "3-merged-duplicate")["canonical_path"] is None:
        raise RuntimeError("A merged duplicate must still point at its canonical document")

    # --- step 4: the fingerprint recheck, with no governor run in between --
    config.write_text(
        config.read_text(encoding="utf-8") + "\n[functions.send-email]\nverify_jwt = true\n",
        encoding="utf-8",
    )
    after = read(supply, "docs/architecture/API.md", "4-dependency-touched")
    if after["status"] == "ok":
        raise RuntimeError("The fingerprint recheck failed to notice a changed dependency")
    config.write_text(
        config.read_text(encoding="utf-8").replace("\n[functions.send-email]\nverify_jwt = true\n", ""),
        encoding="utf-8",
    )

    # --- step 5: a production nobody can tie to a commit -------------------
    if read(supply, "docs/status/PRODUCTION.md", "5-production-before-drift")["status"] != "ok":
        raise RuntimeError("The production status document should start out trusted")
    drift_production(destination)
    drift = docgov(destination, "drift", "--environments", "staging,production", "--apply")
    drift_decision = json.loads(drift.stdout)
    if drift_decision.get("result") != "action_required":
        raise RuntimeError(f"Production drift was not detected: {drift.stdout}\n{drift.stderr}")
    if not any(item["kind"] == "environment_drift" for item in drift_decision["findings"]):
        raise RuntimeError("No environment_drift finding was emitted")
    if read(supply, "docs/status/PRODUCTION.md", "5-production-after-drift")["status"] == "ok":
        raise RuntimeError("A drifted production must make its status document unreadable")

    listing = supply.list_documents(usable_only=False)
    if not any(item["usable"] is False and item["reason"] for item in listing):
        raise RuntimeError("Unusable documents must be listed with a reason")

    if mcp_stdio:
        reads.extend(run_stdio_session(destination))

    return {"decision": decision, "drift": drift_decision, "reads": reads, "listing": listing}


def run_stdio_session(destination: Path) -> List[Dict[str, Any]]:  # pragma: no cover - optional dep
    """Drive the real `docgov-mcp` stdio server, the way a coding agent would."""
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def session() -> List[Dict[str, Any]]:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "docgov.mcp_server", "--root", str(destination)],
        )
        collected: List[Dict[str, Any]] = []
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                tools = await client.list_tools()
                collected.append({"step": "mcp", "tools": sorted(tool.name for tool in tools.tools)})
                for path in ("docs/architecture/API.md", "docs/status/RELEASE.md"):
                    result = await client.call_tool("get_document", {"path": path})
                    payload = json.loads(result.content[0].text)
                    collected.append({
                        "step": "mcp",
                        "path": path,
                        "status": payload["status"],
                        "code": payload.get("code"),
                        "served_characters": len(payload.get("content") or ""),
                    })
        return collected

    return asyncio.run(session())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Doc Governor demo scenario end to end.")
    parser.add_argument("--keep", action="store_true", help="Keep the temporary demo repository for inspection.")
    parser.add_argument(
        "--enable-model",
        action="store_true",
        help="Run the same bounded scenario through the Strands agent graph on Amazon Bedrock.",
    )
    parser.add_argument(
        "--mcp-stdio",
        action="store_true",
        help="Also drive the real docgov-mcp stdio server (requires the `mcp` extra).",
    )
    args = parser.parse_args()
    if args.keep:
        destination = Path(tempfile.mkdtemp(prefix="docgov-demo-"))
        outcome = run_demo(destination, enable_model=args.enable_model, mcp_stdio=args.mcp_stdio)
        print(f"Demo repository: {destination}")
    else:
        with tempfile.TemporaryDirectory(prefix="docgov-demo-") as temporary:
            outcome = run_demo(
                Path(temporary), enable_model=args.enable_model, mcp_stdio=args.mcp_stdio
            )
    decision = outcome["decision"]
    print(json.dumps({
        "result": decision["result"],
        "changed": decision["changed"],
        "finding_count": decision["finding_count"],
        "safe_modified_paths": decision["modified_paths"],
        "blocking_kinds": sorted({
            finding["kind"]
            for finding in decision["findings"]
            if finding["risk"] == "high"
        }),
        "model_used": decision.get("model_used", False),
        "model_trace": decision.get("model_trace", []),
        "drift_findings": sorted({finding["kind"] for finding in outcome["drift"]["findings"]}),
        "reads": outcome["reads"],
        "documents": [
            {"path": item["path"], "usable": item["usable"]} for item in outcome["listing"]
        ],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
