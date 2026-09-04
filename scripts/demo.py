from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def commit(root: Path, message: str) -> str:
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=root, check=True)
    return git(root, "rev-parse", "HEAD")


def run_demo(destination: Path) -> dict[str, object]:
    source = Path(__file__).resolve().parents[1] / "examples" / "supabase-demo"
    shutil.copytree(source, destination, dirs_exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=destination, check=True)
    git(destination, "config", "user.name", "Doc Governor Demo")
    git(destination, "config", "user.email", "demo@example.com")

    public_doc = destination / "docs" / "public" / "ANNOUNCEMENT.md"
    base = commit(destination, "demo baseline")

    function = destination / "supabase" / "functions" / "send-email" / "index.ts"
    function.parent.mkdir(parents=True, exist_ok=True)
    function.write_text("export default {};\n", encoding="utf-8")
    config = destination / "supabase" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\n[functions.send-email]\nverify_jwt = false\n",
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

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "docgov",
            "--root",
            str(destination),
            "--json",
            "review",
            "--base",
            base,
            "--head",
            head,
            "--apply",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    decision = json.loads(completed.stdout)
    if completed.returncode != 2 or decision.get("result") != "action_required":
        raise RuntimeError(f"Unexpected demo decision: {completed.stdout}\n{completed.stderr}")
    if (destination / "docs" / "architecture" / "API-notes.md").exists():
        raise RuntimeError("Safe duplicate was not removed")
    for path in ("docs/architecture/API.md", "docs/architecture/EDGE_FUNCTIONS.md"):
        if '"send-email"' not in (destination / path).read_text(encoding="utf-8"):
            raise RuntimeError(f"Supabase inventory was not synchronized in {path}")
    if "Unreviewed public claim." not in public_doc.read_text(encoding="utf-8"):
        raise RuntimeError("Protected public copy was unexpectedly rewritten")
    return decision


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic Doc Governor demo scenario.")
    parser.add_argument("--keep", action="store_true", help="Keep the temporary demo repository for inspection.")
    args = parser.parse_args()
    if args.keep:
        destination = Path(tempfile.mkdtemp(prefix="docgov-demo-"))
        decision = run_demo(destination)
        print(f"Demo repository: {destination}")
    else:
        with tempfile.TemporaryDirectory(prefix="docgov-demo-") as temporary:
            decision = run_demo(Path(temporary))
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
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
