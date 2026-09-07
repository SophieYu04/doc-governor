from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Evidence:
    path: str
    kind: str = "source"
    sha256: Optional[str] = None
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass
class DocumentRecord:
    path: str
    type: str
    owner: str = "unassigned"
    status: str = "current"
    depends_on: List[str] = field(default_factory=list)
    ttl_days: Optional[int] = None
    approval: str = "auto"
    last_verified_at: Optional[str] = None
    authority: str = "canonical"
    canonical_key: Optional[str] = None
    # Deployment environments this document describes. Declaring one binds the
    # document's trust to that environment matching the state Git produced.
    environments: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "DocumentRecord":
        return cls(
            path=str(value["path"]),
            type=str(value.get("type", "contract")),
            owner=str(value.get("owner", "unassigned")),
            status=str(value.get("status", "current")),
            depends_on=[str(item) for item in value.get("depends_on", [])],
            ttl_days=value.get("ttl_days"),
            approval=str(value.get("approval", "auto")),
            last_verified_at=(
                str(value["last_verified_at"])
                if value.get("last_verified_at") is not None
                else None
            ),
            authority=str(value.get("authority", "canonical")),
            canonical_key=value.get("canonical_key"),
            environments=[str(item) for item in value.get("environments", [])],
        )

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        return {key: item for key, item in value.items() if item is not None and item != []}


@dataclass
class VerificationRecord:
    id: str
    command: List[str]
    inputs: List[str]
    depends_on: List[str] = field(default_factory=list)
    workdir: str = "."
    related_documents: List[str] = field(default_factory=list)
    packages: List[str] = field(default_factory=list)
    timeout_seconds: Optional[float] = None

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "VerificationRecord":
        command = value.get("command", [])
        if not isinstance(command, list) or not command or not all(
            isinstance(item, str) and item for item in command
        ):
            raise ValueError("Verification command must be a non-empty argv list.")
        inputs = value.get("inputs", [])
        depends_on = value.get("depends_on", [])
        if not isinstance(inputs, list) or not isinstance(depends_on, list):
            raise ValueError("Verification inputs and depends_on must be lists.")
        if not inputs and not depends_on:
            raise ValueError("Verification must declare at least one input or dependency pattern.")
        environment = value.get("environment", {})
        if not isinstance(environment, dict):
            raise ValueError("Verification environment must be a mapping.")
        packages = environment.get("packages", [])
        if not isinstance(packages, list) or not all(isinstance(item, str) and item for item in packages):
            raise ValueError("Verification environment packages must be a string list.")
        timeout = value.get("timeout_seconds")
        if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
            raise ValueError("Verification timeout_seconds must be positive.")
        return cls(
            id=str(value["id"]),
            command=list(command),
            inputs=[str(item) for item in inputs],
            depends_on=[str(item) for item in depends_on],
            workdir=str(value.get("workdir", ".")),
            related_documents=[str(item) for item in value.get("related_documents", [])],
            packages=list(packages),
            timeout_seconds=float(timeout) if timeout is not None else None,
        )

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "id": self.id,
            "command": self.command,
            "workdir": self.workdir,
            "inputs": self.inputs,
        }
        if self.depends_on:
            value["depends_on"] = self.depends_on
        if self.related_documents:
            value["related_documents"] = self.related_documents
        if self.packages:
            value["environment"] = {"packages": self.packages}
        if self.timeout_seconds is not None:
            value["timeout_seconds"] = self.timeout_seconds
        return value


@dataclass
class Finding:
    kind: str
    risk: str
    action: str
    documents: List[str]
    reason: str
    evidence: List[Evidence] = field(default_factory=list)
    proposed_patch: Optional[str] = None
    human_decision: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["evidence"] = [item.to_dict() for item in self.evidence]
        return {key: item for key, item in value.items() if item is not None}


@dataclass
class TrustResult:
    path: str
    type: str
    status: str
    scope: str
    reason: str
    evidence: List[Evidence] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["evidence"] = [item.to_dict() for item in self.evidence]
        return value


@dataclass
class GovernanceDecision:
    run_id: str
    mode: str
    result: str
    changed: bool
    findings: List[Finding] = field(default_factory=list)
    trust_results: List[TrustResult] = field(default_factory=list)
    modified_paths: List[str] = field(default_factory=list)
    head_sha: Optional[str] = None
    model_used: bool = False
    model_trace: List[Dict[str, str]] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def high_risk_findings(self) -> List[Finding]:
        return [finding for finding in self.findings if finding.risk == "high"]

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "run_id": self.run_id,
            "mode": self.mode,
            "result": self.result,
            "changed": self.changed,
            "finding_count": len(self.findings),
            "findings": [finding.to_dict() for finding in self.findings],
            "trust_results": [result.to_dict() for result in self.trust_results],
            "modified_paths": sorted(set(self.modified_paths)),
            "model_used": self.model_used,
            "model_trace": self.model_trace,
        }
        if self.head_sha:
            value["head_sha"] = self.head_sha
        if self.error:
            value["error"] = self.error
        return value
