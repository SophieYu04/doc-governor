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
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from .models import Evidence, Finding


DEFAULT_API_BASE = "https://api.supabase.com"
DEFAULT_EVIDENCE_DIR = "docs/security-evidence/supabase-advisors"
PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class SupabaseAdvisorError(RuntimeError):
    """A fail-closed error raised while collecting remote Advisor evidence."""


#: Ledger action a release workflow records when it promotes a build to an
#: environment, together with the Advisor fingerprint observed at that moment.
PROMOTION_ACTION = "promote_environment"
#: Ledger document key for an environment-level record.
ENVIRONMENT_DOCUMENT_PREFIX = "supabase:"


@dataclass(frozen=True)
class EnvironmentFingerprint:
    """One environment's Advisor state, reduced to something comparable."""

    environment: str
    advisor_fingerprint: str
    lint_counts: Dict[str, int]
    collected_at: str

    @property
    def signals(self) -> set[str]:
        """Advisor categories and lint types this environment currently reports.

        Both are kept: a new lint type inside a category staging already has is
        still a way the environments have diverged.
        """
        return {name for name, count in self.lint_counts.items() if count}


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
    category_counts: Counter[str] = Counter()
    for identity in identities:
        category_counts.update(identity["categories"])
    return {
        "finding_count": len(identities),
        "level_counts": dict(sorted(level_counts.items())),
        "name_counts": dict(sorted(name_counts.items())),
        "category_counts": dict(sorted(category_counts.items())),
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
        "schema_version": 2,
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


def environment_fingerprint(snapshot: Mapping[str, Any]) -> EnvironmentFingerprint:
    """Reduce an Advisor evidence snapshot to the comparable shape drift detection needs."""
    advisors = snapshot.get("advisors", {})
    if not isinstance(advisors, Mapping):
        raise SupabaseAdvisorError("The Advisor snapshot has no advisors summary.")
    counts: Dict[str, int] = {}
    for advisor_type in ("security", "performance"):
        summary = advisors.get(advisor_type, {})
        if not isinstance(summary, Mapping):
            continue
        for category, count in (summary.get("category_counts") or {}).items():
            counts[f"{advisor_type}:{category}"] = counts.get(f"{advisor_type}:{category}", 0) + int(count)
        for name, count in (summary.get("name_counts") or {}).items():
            counts[f"{advisor_type}:lint:{name}"] = counts.get(f"{advisor_type}:lint:{name}", 0) + int(count)
    return EnvironmentFingerprint(
        environment=str(snapshot.get("environment", "")),
        advisor_fingerprint=str(snapshot.get("fingerprint_sha256", "")),
        lint_counts=dict(sorted(counts.items())),
        collected_at=str(snapshot.get("observed_at", "")),
    )


def latest_promotion(
    ledger_entries: Sequence[Mapping[str, Any]],
    environment: str,
) -> Optional[Mapping[str, Any]]:
    """Return the last release promotion recorded for an environment, if any."""
    document = f"{ENVIRONMENT_DOCUMENT_PREFIX}{environment}"
    promotion: Optional[Mapping[str, Any]] = None
    for entry in ledger_entries:
        if entry.get("document") == document and entry.get("action") == PROMOTION_ACTION:
            promotion = entry
    return promotion


def compare_environments(
    fingerprints: Mapping[str, EnvironmentFingerprint],
    ledger: Any,
    *,
    production: str = "production",
    reference: str = "staging",
) -> List[Finding]:
    """Detect symptomatic divergence between what Git produced and what production is.

    Read-only by construction: this function reads Advisor summaries and the
    append-only ledger and returns findings. It never deploys, never writes, and
    never holds a production write credential (non-goal 1, D7).

    Two rules, both stated as failures of the documented release flow
    ``staging -> git commit / migration -> release branch -> production deployment``:

    1. Production's Advisor fingerprint differs from the one recorded when the
       last release was promoted to production, and no later promotion explains
       it. Someone changed production outside the release flow.
    2. Production reports Advisor categories that staging does not. The
       environments have diverged, so staging verification no longer predicts
       production.

    A drifted production makes every document describing it untrustworthy, which
    is how this connects back to the read path: if production is not at a known
    Git state, no document about production may be served.
    """
    findings: List[Finding] = []
    current = fingerprints.get(production)
    if current is None:
        return findings

    entries = list(ledger.entries()) if hasattr(ledger, "entries") else list(ledger or [])
    promotion = latest_promotion(entries, production)
    baseline = fingerprints.get(reference)
    if promotion is None and baseline is None:
        # Neither rule can be evaluated. Reporting "no findings" here would let a
        # caller record the environment as clean, so the absence of evidence is
        # itself the finding (P3).
        return [Finding(
            kind="environment_drift",
            risk="high",
            action="block",
            documents=[],
            reason=(
                f"The {production} environment cannot be checked: there is no recorded release "
                f"promotion to compare its Advisor state against, and no {reference} evidence to "
                "compare it with. An unverifiable environment is not a verified one."
            ),
            evidence=[Evidence(path=production, kind="environment", sha256=current.advisor_fingerprint)],
            human_decision=(
                f"Collect {reference} Advisor evidence, or record the promoted state with "
                "`docgov drift --apply --record-promotion "
                f"{production}`."
            ),
        )]
    if promotion is None:
        # Honest boundary: with no recorded promotion there is no Git-side state
        # to compare against, so rule 1 cannot fire. Rule 2 still applies.
        pass
    elif str(promotion.get("dependency_fingerprint", "")) != current.advisor_fingerprint:
        findings.append(Finding(
            kind="environment_drift",
            risk="high",
            action="block",
            documents=[],
            reason=(
                f"The {production} environment no longer matches the state recorded when the last "
                "release was promoted to it, and no later promotion explains the change. It was "
                "changed outside the release flow."
            ),
            evidence=[
                Evidence(path=production, kind="environment", sha256=current.advisor_fingerprint),
                Evidence(
                    path=production,
                    kind="promoted_baseline",
                    sha256=str(promotion.get("dependency_fingerprint", "")),
                    detail=str(promotion.get("timestamp", "")),
                ),
            ],
            human_decision=(
                "Reconcile the environment with the release branch, then record a new promotion "
                "with `docgov drift --record-promotion`."
            ),
        ))

    if baseline is not None:
        extra = sorted(current.signals - baseline.signals)
        if extra:
            findings.append(Finding(
                kind="environment_drift",
                risk="high",
                action="block",
                documents=[],
                reason=(
                    f"The {production} environment reports {len(extra)} Advisor "
                    f"signal{'' if len(extra) == 1 else 's'} (categories or lint types) that "
                    f"{reference} does not, so {reference} verification no longer predicts {production}."
                ),
                evidence=[
                    Evidence(path=production, kind="environment", sha256=current.advisor_fingerprint),
                    Evidence(
                        # A reference environment is context, not a drifted one:
                        # the trust state keys off `kind == "environment"` to
                        # decide which environments to distrust.
                        path=reference,
                        kind="reference_environment",
                        sha256=baseline.advisor_fingerprint,
                        detail=f"{len(extra)} signal(s) present only in {production}",
                    ),
                ],
                human_decision=(
                    f"Bring {reference} and {production} back into alignment, or re-verify the "
                    f"documents that describe {production} against {production} itself."
                ),
            ))
    return findings
