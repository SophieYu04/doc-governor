from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .models import Evidence


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Ledger:
    def __init__(self, path: Path):
        self.path = path

    def entries(self) -> list[Dict[str, Any]]:
        if not self.path.exists():
            return []
        entries: list[Dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
        return entries

    def latest_for(self, document: str, action: Optional[str] = None) -> Optional[Dict[str, Any]]:
        matches = [
            entry
            for entry in self.entries()
            if entry.get("document") == document
            and (action is None or entry.get("action") == action)
        ]
        return matches[-1] if matches else None

    def append(
        self,
        *,
        run_id: str,
        document: str,
        action: str,
        reason: str,
        evidence: Iterable[Evidence] = (),
        previous_hash: Optional[str] = None,
        new_hash: Optional[str] = None,
        head_sha: Optional[str] = None,
        dependency_fingerprint: Optional[str] = None,
        verifier: Optional[str] = None,
    ) -> bool:
        entry: Dict[str, Any] = {
            "run_id": run_id,
            "document": document,
            "action": action,
            "timestamp": utc_now(),
            "reason": reason,
            "evidence": [item.to_dict() for item in evidence],
        }
        if previous_hash:
            entry["previous_hash"] = previous_hash
        if new_hash:
            entry["new_hash"] = new_hash
        if head_sha:
            entry["head_sha"] = head_sha
        if dependency_fingerprint:
            entry["dependency_fingerprint"] = dependency_fingerprint
        if verifier:
            entry["verifier"] = verifier
        identity = (run_id, document, action, new_hash)
        for existing in self.entries():
            if (existing.get("run_id"), existing.get("document"), existing.get("action"), existing.get("new_hash")) == identity:
                return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return True
