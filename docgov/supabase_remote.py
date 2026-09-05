from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple
from urllib.parse import quote


DEFAULT_API_BASE = "https://api.supabase.com"
DEFAULT_EVIDENCE_DIR = "docs/security-evidence/supabase-advisors"
PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class SupabaseAdvisorError(RuntimeError):
    """A fail-closed error raised while collecting remote Advisor evidence."""


@dataclass(frozen=True)
class EvidenceWrite:
    environment: str
    changed: bool
    path: str | None
    fingerprint: str


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def parse_projects(value: str) -> List[Tuple[str, str]]:
    """Parse `environment=project_ref` pairs without exposing refs in errors."""
    projects: List[Tuple[str, str]] = []
    seen: set[str] = set()
    for item in re.split(r"[\n,]", value):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise SupabaseAdvisorError(
                "Each Supabase project must use the environment=project_ref format."
            )
        environment, project_ref = (part.strip().lower() for part in item.split("=", 1))
        if not PROJECT_PATTERN.fullmatch(environment):
            raise SupabaseAdvisorError("A Supabase environment name is invalid.")
        if not re.fullmatch(r"[a-z0-9]{8,64}", project_ref):
            raise SupabaseAdvisorError("A Supabase project ref is invalid.")
        if environment in seen:
            raise SupabaseAdvisorError(
                f"The Supabase environment {environment!r} was configured more than once."
            )
        seen.add(environment)
        projects.append((environment, project_ref))
    if not projects:
        raise SupabaseAdvisorError("At least one Supabase project is required.")
    return projects


def _request_json(
    url: str,
    access_token: str,
    *,
    timeout_seconds: float,
) -> Dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {access_token}")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "doc-governor/supabase-advisor-evidence")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise SupabaseAdvisorError(
            f"Supabase Advisor request failed with HTTP {exc.code}."
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SupabaseAdvisorError("Supabase Advisor request could not be completed.") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SupabaseAdvisorError("Supabase Advisor returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise SupabaseAdvisorError("Supabase Advisor returned an unexpected response shape.")
    return payload


def fetch_advisors(
    project_ref: str,
    access_token: str,
    *,
    api_base: str = DEFAULT_API_BASE,
    timeout_seconds: float = 20,
) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch security and performance lints through read-only Management API GETs."""
    if not access_token.strip():
        raise SupabaseAdvisorError("SUPABASE_ACCESS_TOKEN is required for Advisor checks.")
    if not api_base.startswith("https://"):
        raise SupabaseAdvisorError("Supabase API base must use HTTPS.")
    result: Dict[str, List[Dict[str, Any]]] = {}
    for advisor_type in ("security", "performance"):
        url = (
            f"{api_base.rstrip('/')}/v1/projects/{quote(project_ref, safe='')}"
            f"/advisors/{advisor_type}"
        )
        payload = _request_json(url, access_token, timeout_seconds=timeout_seconds)
        # The public Management API returns {"lints": [...]}. Accept the MCP
        # wrapper too so recorded fixtures can be replayed without reshaping.
        if isinstance(payload.get("result"), dict):
            payload = payload["result"]
        lints = payload.get("lints")
        if not isinstance(lints, list) or not all(isinstance(item, dict) for item in lints):
            raise SupabaseAdvisorError(
                f"Supabase {advisor_type} Advisor response is missing its lint list."
            )
        result[advisor_type] = list(lints)
    return result


def _lint_identity(lint: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep change-sensitive fields for hashing, without publishing entity names."""
    return {
        "name": str(lint.get("name", "unknown")),
        "level": str(lint.get("level", "UNKNOWN")).upper(),
        "facing": str(lint.get("facing", "UNKNOWN")).upper(),
        "categories": sorted(str(item).upper() for item in lint.get("categories", [])),
        "cache_key": str(lint.get("cache_key", "")),
        "metadata": lint.get("metadata", {}),
    }


def summarize_lints(lints: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    identities = [_lint_identity(lint) for lint in lints]
    identities.sort(key=_canonical_json)
    level_counts = Counter(identity["level"] for identity in identities)
    name_counts = Counter(identity["name"] for identity in identities)
    return {
        "finding_count": len(identities),
        "level_counts": dict(sorted(level_counts.items())),
        "name_counts": dict(sorted(name_counts.items())),
        "finding_fingerprints": sorted(_sha256(identity) for identity in identities),
        "fingerprint_sha256": _sha256(identities),
    }


def build_evidence_snapshot(
    environment: str,
    project_ref: str,
    advisors: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    observed_at: str | None = None,
) -> Dict[str, Any]:
    timestamp = observed_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    summaries = {
        advisor_type: summarize_lints(advisors.get(advisor_type, []))
        for advisor_type in ("security", "performance")
    }
    stable_state = {
        "environment": environment,
        "project_ref_sha256": hashlib.sha256(project_ref.encode("utf-8")).hexdigest(),
        "advisors": summaries,
    }
    return {
        "schema_version": 1,
        "kind": "supabase_advisor_snapshot",
        "environment": environment,
        "project_ref_sha256": stable_state["project_ref_sha256"],
        "observed_at": timestamp,
        "read_only": True,
        "source": "Supabase Management API Advisor GET endpoints",
        "required_token_permission": "advisors_read",
        "redaction": "Entity names, metadata, cache keys, descriptions, details, and raw payloads are not stored.",
        "advisors": summaries,
        "fingerprint_sha256": _sha256(stable_state),
    }


def _safe_output_root(root: Path, relative_dir: str) -> Path:
    candidate = Path(relative_dir)
    if candidate.is_absolute() or not relative_dir.strip():
        raise SupabaseAdvisorError("Supabase evidence directory must be repository-relative.")
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise SupabaseAdvisorError("Supabase evidence directory escapes the repository.") from exc
    if resolved == resolved_root or ".git" in resolved.relative_to(resolved_root).parts:
        raise SupabaseAdvisorError("Supabase evidence directory is not an allowed write target.")
    return resolved


def _latest_snapshot(
    output_root: Path,
    environment: str,
    project_ref_sha256: str,
) -> Tuple[Path, Dict[str, Any]] | None:
    candidates: List[Tuple[str, str, Path, Dict[str, Any]]] = []
    if not output_root.exists():
        return None
    for path in output_root.rglob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or value.get("kind") != "supabase_advisor_snapshot":
            continue
        if value.get("environment") != environment:
            continue
        if value.get("project_ref_sha256") != project_ref_sha256:
            continue
        candidates.append((str(value.get("observed_at", "")), path.as_posix(), path, value))
    if not candidates:
        return None
    _, _, path, value = max(candidates)
    return path, value


def write_evidence(
    root: Path,
    snapshot: Mapping[str, Any],
    *,
    output_dir: str = DEFAULT_EVIDENCE_DIR,
) -> EvidenceWrite:
    output_root = _safe_output_root(root, output_dir)
    environment = str(snapshot["environment"])
    fingerprint = str(snapshot["fingerprint_sha256"])
    latest = _latest_snapshot(
        output_root,
        environment,
        str(snapshot["project_ref_sha256"]),
    )
    if latest is not None and latest[1].get("fingerprint_sha256") == fingerprint:
        return EvidenceWrite(
            environment=environment,
            changed=False,
            path=latest[0].relative_to(root.resolve()).as_posix(),
            fingerprint=fingerprint,
        )

    observed_at = str(snapshot["observed_at"])
    try:
        instant = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SupabaseAdvisorError("Supabase evidence timestamp is invalid.") from exc
    day = instant.astimezone(timezone.utc).strftime("%Y-%m-%d")
    stamp = instant.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = output_root / day / f"{environment}-{stamp}-{fingerprint[:12]}.json"
    if destination.exists():
        existing = json.loads(destination.read_text(encoding="utf-8"))
        if existing == snapshot:
            return EvidenceWrite(
                environment,
                False,
                destination.relative_to(root.resolve()).as_posix(),
                fingerprint,
            )
        raise SupabaseAdvisorError("A different immutable Advisor snapshot already uses this path.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return EvidenceWrite(
        environment=environment,
        changed=True,
        path=destination.relative_to(root.resolve()).as_posix(),
        fingerprint=fingerprint,
    )


def collect_advisor_evidence(
    root: Path,
    projects: Iterable[Tuple[str, str]],
    access_token: str,
    *,
    output_dir: str = DEFAULT_EVIDENCE_DIR,
    api_base: str = DEFAULT_API_BASE,
    timeout_seconds: float = 20,
    observed_at: str | None = None,
) -> List[EvidenceWrite]:
    """Fetch every project first, then persist privacy-preserving immutable snapshots."""
    fetched: List[Tuple[str, Dict[str, Any]]] = []
    for environment, project_ref in projects:
        advisors = fetch_advisors(
            project_ref,
            access_token,
            api_base=api_base,
            timeout_seconds=timeout_seconds,
        )
        fetched.append((project_ref, build_evidence_snapshot(
            environment,
            project_ref,
            advisors,
            observed_at=observed_at,
        )))
    return [
        write_evidence(root, snapshot, output_dir=output_dir)
        for _, snapshot in fetched
    ]
