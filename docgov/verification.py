from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .catalog import Catalog
from .git_tools import current_sha, tracked_paths, untracked_paths
from .ledger import Ledger, sha256_bytes, utc_now
from .models import VerificationRecord
from .patterns import matches_repo_glob


VERIFICATION_KIND = "verification"
VERIFICATION_ACTION = "verification_run"
IMPORT_ACTION = "verification_import"
REQUIRED_ENVIRONMENT_FIELDS = ("os", "os_release", "architecture", "python")


class VerificationError(ValueError):
    pass


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def definition_hash(record: VerificationRecord) -> str:
    return _canonical_hash(record.to_dict())


def capture_environment(packages: Sequence[str]) -> Dict[str, Any]:
    versions: Dict[str, Optional[str]] = {}
    for package in sorted(set(packages)):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return {
        "os": platform.system() or None,
        "os_release": platform.release() or None,
        "architecture": platform.machine() or None,
        "python": platform.python_version() or None,
        "packages": versions,
    }


def _repo_file_candidates(root: Path) -> List[str]:
    return sorted(set(tracked_paths(root)) | set(untracked_paths(root)))


def capture_scope(
    root: Path,
    patterns: Sequence[str],
    *,
    excluded_paths: Iterable[str] = (),
) -> Dict[str, str]:
    resolved_root = root.resolve()
    excluded = {item.replace("\\", "/") for item in excluded_paths}
    files: Dict[str, str] = {}
    for relative in _repo_file_candidates(root):
        normalized = relative.replace("\\", "/")
        if normalized in excluded or not any(matches_repo_glob(normalized, pattern) for pattern in patterns):
            continue
        absolute = root / normalized
        # A tracked path may be deleted in the working tree. Omitting it from
        # the live map lets the comparison report the deletion explicitly.
        if not absolute.exists() and not absolute.is_symlink():
            continue
        try:
            resolved = absolute.resolve(strict=True)
        except OSError as exc:
            raise VerificationError(f"Verification input is unreadable: {normalized}: {exc}") from exc
        if resolved_root not in resolved.parents or not resolved.is_file():
            raise VerificationError(f"Verification input escapes the repository or is not a file: {normalized}")
        files[normalized] = sha256_bytes(resolved.read_bytes())
    return files


def capture_inputs(
    root: Path,
    record: VerificationRecord,
    *,
    ledger_path: Optional[Path] = None,
) -> Dict[str, Dict[str, str]]:
    excluded: List[str] = []
    if ledger_path is not None:
        try:
            excluded.append(ledger_path.resolve().relative_to(root.resolve()).as_posix())
        except ValueError:
            pass
    return {
        "inputs": capture_scope(root, record.inputs, excluded_paths=excluded),
        "dependencies": capture_scope(root, record.depends_on, excluded_paths=excluded),
    }


def _changed_files(before: Mapping[str, str], after: Mapping[str, str]) -> List[str]:
    return sorted({path for path in set(before) | set(after) if before.get(path) != after.get(path)})


def _validate_workdir(root: Path, workdir: str) -> Path:
    if Path(workdir).is_absolute() or ".." in Path(workdir).parts:
        raise VerificationError("Verification workdir must stay inside the repository.")
    resolved_root = root.resolve()
    resolved = (resolved_root / workdir).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise VerificationError("Verification workdir escapes the repository.")
    if not resolved.is_dir():
        raise VerificationError(f"Verification workdir does not exist: {workdir}")
    return resolved


def _safe_trace(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    trace: List[Dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        sanitized = {
            key: str(item[key])
            for key in ("event", "name", "agent", "tool", "model")
            if item.get(key) is not None
            and "\n" not in str(item[key])
            and len(str(item[key])) <= 240
        }
        if sanitized:
            trace.append(sanitized)
    return trace


def _event_id(event: Mapping[str, Any]) -> str:
    value = {key: item for key, item in event.items() if key != "event_id"}
    return _canonical_hash(value)


def _result_summary(stdout: bytes, stderr: bytes, *, timed_out: bool, changed: bool) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "timed_out": timed_out,
        "inputs_changed_during_run": changed,
    }
    combined = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    unittest_count = re.search(r"\bRan (\d+) tests?\b", combined)
    pytest_count = re.search(r"\b(\d+) passed\b", combined)
    subtest_count = re.search(r"\b(\d+) subtests? passed\b", combined)
    if unittest_count:
        summary["tests"] = int(unittest_count.group(1))
    elif pytest_count:
        summary["tests"] = int(pytest_count.group(1))
    if subtest_count:
        summary["subtests"] = int(subtest_count.group(1))
    return summary


def run_verification(
    root: Path,
    catalog: Catalog,
    ledger: Ledger,
    identifier: str,
) -> Dict[str, Any]:
    record = catalog.verification_for(identifier)
    if record is None:
        raise VerificationError(f"Unknown verification: {identifier}")
    workdir = _validate_workdir(root, record.workdir)
    before = capture_inputs(root, record, ledger_path=ledger.path)
    environment = capture_environment(record.packages)
    source_commit = current_sha(root)
    started_at = utc_now()
    timed_out = False
    try:
        completed = subprocess.run(
            record.command,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=record.timeout_seconds,
            shell=False,
            check=False,
        )
        exit_code: Optional[int] = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
    except OSError as exc:
        exit_code = None
        stdout = b""
        stderr = str(exc).encode("utf-8", errors="replace")
    completed_at = utc_now()
    after = capture_inputs(root, record, ledger_path=ledger.path)
    changed = sorted(set(
        _changed_files(before["inputs"], after["inputs"])
        + _changed_files(before["dependencies"], after["dependencies"])
    ))
    result = "success" if exit_code == 0 and not changed else "failure"
    if changed:
        result = "invalidated"
    source_digest = sha256_bytes(stdout + b"\0" + stderr)
    event: Dict[str, Any] = {
        "kind": VERIFICATION_KIND,
        "action": VERIFICATION_ACTION,
        "verification": record.id,
        "definition_hash": definition_hash(record),
        "command": list(record.command),
        "workdir": record.workdir,
        "input_patterns": list(record.inputs),
        "dependency_patterns": list(record.depends_on),
        "input_files": before["inputs"],
        "dependency_files": before["dependencies"],
        "source_commit": source_commit,
        "started_at": started_at,
        "completed_at": completed_at,
        "timestamp": completed_at,
        "exit_code": exit_code,
        "result": result,
        "summary": _result_summary(stdout, stderr, timed_out=timed_out, changed=bool(changed)),
        "changed_during_run": changed,
        "environment": environment,
        "source": {
            "method": "executed",
            "url": None,
            "digest": source_digest,
        },
        "recorded_by": "run",
        "related_documents": list(record.related_documents),
    }
    # Model traces are parsed only from a single JSON result and always reduced
    # to public identifiers. Arbitrary command output is never stored.
    try:
        decoded = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = None
    if isinstance(decoded, dict):
        trace = _safe_trace(decoded.get("model_trace"))
        if trace:
            event["model_trace"] = trace
        if isinstance(decoded.get("model_used"), bool):
            event["model_used"] = decoded["model_used"]
    event["event_id"] = _event_id(event)
    ledger.append_verification(event)
    return event


def _parse_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerificationError(f"Imported verification requires {field}.")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerificationError(f"Imported verification has invalid {field}.") from exc
    return value


def normalize_import_event(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise VerificationError("Each imported verification event must be an object.")
    required = (
        "verification", "definition_hash", "command", "workdir", "input_patterns",
        "dependency_patterns", "input_files", "dependency_files", "source_commit",
        "exit_code", "result", "environment", "source",
    )
    missing = [key for key in required if key not in value]
    if missing:
        raise VerificationError(f"Imported verification is missing: {', '.join(missing)}")
    event = dict(value)
    event["kind"] = VERIFICATION_KIND
    event["action"] = IMPORT_ACTION
    event["started_at"] = _parse_timestamp(event.get("started_at"), "started_at")
    event["completed_at"] = _parse_timestamp(event.get("completed_at"), "completed_at")
    event["timestamp"] = event["completed_at"]
    event["recorded_by"] = "import"
    if event["result"] not in {"success", "failure", "invalidated"}:
        raise VerificationError("Imported verification result is invalid.")
    if not isinstance(event["command"], list) or not all(isinstance(item, str) for item in event["command"]):
        raise VerificationError("Imported verification command must be an argv list.")
    for field in ("input_patterns", "dependency_patterns"):
        if not isinstance(event[field], list) or not all(isinstance(item, str) for item in event[field]):
            raise VerificationError(f"Imported {field} must be a string list.")
    for field in ("environment", "source"):
        if not isinstance(event[field], dict):
            raise VerificationError(f"Imported {field} must be an object.")
    for field in ("input_files", "dependency_files"):
        if event[field] is None:
            continue
        if not isinstance(event[field], dict):
            raise VerificationError(f"Imported {field} must be an object or null when unknown.")
        normalized_files: Dict[str, str] = {}
        for path, digest in event[field].items():
            candidate = Path(str(path))
            if candidate.is_absolute() or ".." in candidate.parts or not str(path):
                raise VerificationError(f"Imported {field} contains an unsafe path.")
            if not isinstance(digest, str) or len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest.lower()
            ):
                raise VerificationError(f"Imported {field} contains an invalid SHA-256 digest.")
            normalized_files[str(path).replace("\\", "/")] = digest.lower()
        event[field] = normalized_files
    environment = event["environment"]
    packages = environment.get("packages")
    if not isinstance(packages, dict):
        raise VerificationError("Imported environment packages must be an object.")
    event["environment"] = {
        field: environment.get(field) if isinstance(environment.get(field), str) else None
        for field in REQUIRED_ENVIRONMENT_FIELDS
    }
    event["environment"]["packages"] = {
        str(package): version if isinstance(version, str) else None
        for package, version in packages.items()
    }
    source = event["source"]
    if not source.get("method") or not source.get("digest"):
        raise VerificationError("Imported source requires method and digest.")
    method = str(source["method"])
    digest = str(source["digest"]).lower()
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", method):
        raise VerificationError("Imported source method is not a safe identifier.")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise VerificationError("Imported source digest must be a SHA-256 value.")
    url = str(source["url"]) if source.get("url") is not None else None
    if url is not None and (len(url) > 2048 or not url.startswith(("https://", "http://"))):
        raise VerificationError("Imported source URL must be an HTTP(S) URL.")
    event["source"] = {
        "method": method,
        "url": url,
        "digest": digest,
    }
    summary = event.get("summary")
    event["summary"] = {
        "stdout_bytes": int(summary.get("stdout_bytes", 0)),
        "stderr_bytes": int(summary.get("stderr_bytes", 0)),
        "timed_out": bool(summary.get("timed_out", False)),
        "inputs_changed_during_run": bool(summary.get("inputs_changed_during_run", False)),
    } if isinstance(summary, dict) else {
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "timed_out": False,
        "inputs_changed_during_run": False,
    }
    if isinstance(summary, dict):
        for count in ("tests", "subtests"):
            if isinstance(summary.get(count), int) and summary[count] >= 0:
                event["summary"][count] = summary[count]
    event["changed_during_run"] = [
        str(item) for item in event.get("changed_during_run", []) if isinstance(item, str)
    ]
    event["related_documents"] = [
        str(item) for item in event.get("related_documents", []) if isinstance(item, str)
    ]
    event["model_trace"] = _safe_trace(event.get("model_trace"))
    if not isinstance(event.get("model_used"), bool):
        event.pop("model_used", None)
    supplied_event_id = event.get("event_id")
    allowed = {
        "kind", "action", "verification", "definition_hash", "command", "workdir",
        "input_patterns", "dependency_patterns", "input_files", "dependency_files",
        "source_commit", "started_at", "completed_at", "timestamp", "exit_code", "result",
        "summary", "changed_during_run", "environment", "source", "recorded_by",
        "related_documents", "model_used", "model_trace",
    }
    sanitized = {key: item for key, item in event.items() if key in allowed}
    sanitized["event_id"] = str(supplied_event_id or _event_id(sanitized))
    return sanitized


def import_verifications(ledger: Ledger, payload: Any) -> Dict[str, Any]:
    raw_events = payload.get("events") if isinstance(payload, dict) and "events" in payload else [payload]
    if not isinstance(raw_events, list):
        raise VerificationError("Imported events must be a list.")
    events = [normalize_import_event(item) for item in raw_events]
    imported = 0
    skipped = 0
    for event in events:
        if ledger.append_verification(event):
            imported += 1
        else:
            skipped += 1
    return {"result": "changed" if imported else "pass", "imported": imported, "skipped": skipped}


def _environment_complete(environment: Any, packages: Sequence[str]) -> bool:
    if not isinstance(environment, dict):
        return False
    if any(not isinstance(environment.get(field), str) or not environment.get(field) for field in REQUIRED_ENVIRONMENT_FIELDS):
        return False
    recorded_packages = environment.get("packages")
    return isinstance(recorded_packages, dict) and all(
        isinstance(recorded_packages.get(package), str) and recorded_packages.get(package)
        for package in packages
    )


def _latest_event(entries: Iterable[Dict[str, Any]], identifier: str) -> Optional[Dict[str, Any]]:
    matching = [
        (index, entry)
        for index, entry in enumerate(entries)
        if entry.get("kind") == VERIFICATION_KIND and entry.get("verification") == identifier
    ]
    if not matching:
        return None
    return max(matching, key=lambda item: (str(item[1].get("completed_at", "")), item[0]))[1]


def verification_status(
    root: Path,
    catalog: Catalog,
    ledger: Ledger,
    identifier: str,
) -> Dict[str, Any]:
    record = catalog.verification_for(identifier)
    if record is None:
        return {"id": identifier, "known": False, "reusable": False, "reason": "Unknown verification."}
    events = [
        entry for entry in ledger.entries()
        if entry.get("kind") == VERIFICATION_KIND and entry.get("verification") == identifier
    ]
    event = _latest_event(events, identifier)
    rerun = {"command": list(record.command), "workdir": record.workdir}
    base: Dict[str, Any] = {
        "id": identifier,
        "known": True,
        "reusable": False,
        "execution_count": len(events),
        "related_documents": list(record.related_documents),
        "rerun": rerun,
    }
    if event is None:
        return {**base, "reason": "No verification result has been recorded."}
    base["latest"] = {
        key: event.get(key)
        for key in (
            "event_id", "result", "source_commit", "started_at", "completed_at",
            "exit_code", "summary", "environment", "source", "recorded_by", "model_used",
            "model_trace",
        )
        if key in event
    }
    required = (
        "definition_hash", "command", "workdir", "input_patterns", "dependency_patterns",
        "input_files", "dependency_files", "result", "environment", "completed_at",
    )
    missing = [key for key in required if key not in event]
    if missing:
        return {**base, "reason": f"Latest verification record is incomplete: {', '.join(missing)}."}
    if event.get("result") != "success" or event.get("exit_code") != 0:
        return {**base, "reason": "The latest verification did not succeed."}
    summary = event.get("summary")
    if event.get("changed_during_run") or (
        isinstance(summary, dict) and summary.get("inputs_changed_during_run")
    ):
        return {**base, "reason": "Verification inputs changed while the command was running."}
    if event.get("definition_hash") != definition_hash(record):
        return {**base, "reason": "The verification definition changed after this result was recorded."}
    if event.get("command") != record.command or event.get("workdir") != record.workdir:
        return {**base, "reason": "The recorded command or working directory no longer matches the Catalog."}
    if not isinstance(event.get("input_files"), dict) or not isinstance(
        event.get("dependency_files"), dict
    ):
        return {**base, "reason": "The recorded input or dependency snapshot is incomplete or unknown."}
    environment = event.get("environment")
    if not _environment_complete(environment, record.packages):
        return {**base, "reason": "The recorded execution environment is incomplete or unknown."}
    current_environment = capture_environment(record.packages)
    if environment != current_environment:
        return {
            **base,
            "reason": "The current execution environment differs from the recorded environment.",
            "environment_matches": False,
        }
    try:
        current = capture_inputs(root, record, ledger_path=ledger.path)
    except VerificationError as exc:
        return {**base, "reason": str(exc)}
    affected = sorted(set(
        _changed_files(event["input_files"], current["inputs"])
        + _changed_files(event["dependency_files"], current["dependencies"])
    ))
    if affected:
        return {
            **base,
            "reason": "Verification inputs or dependencies changed after the result was recorded.",
            "affected_files": affected,
            "environment_matches": True,
        }
    return {
        **base,
        "reusable": True,
        "reason": "The latest successful result still matches its definition, inputs, dependencies, and environment.",
        "affected_files": [],
        "environment_matches": True,
    }


def list_verifications(root: Path, catalog: Catalog, ledger: Ledger) -> List[Dict[str, Any]]:
    return [verification_status(root, catalog, ledger, record.id) for record in catalog.verifications]
