from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import DocumentRecord

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - exercised only in minimal installs
    yaml = None


DEFAULT_TTLS = {"contract": None, "state": 7, "procedure": 90, "evidence": None, "decision": None}
CORE_TYPES = frozenset(DEFAULT_TTLS)


class Catalog:
    def __init__(self, data: Optional[Dict[str, Any]] = None):
        data = data or {}
        self.data = data
        self.version = int(data.get("version", 1))
        self.taxonomy: Dict[str, List[str]] = {
            str(key): [str(item) for item in value]
            for key, value in data.get("taxonomy", {}).items()
        }
        self.documents: List[DocumentRecord] = [
            DocumentRecord.from_dict(item) for item in data.get("documents", [])
        ]
        self.policies: Dict[str, Any] = dict(data.get("policies", {}))
        unsupported = sorted(
            (set(self.taxonomy) | {record.type for record in self.documents}) - CORE_TYPES
        )
        if unsupported:
            raise ValueError(f"Unsupported document type(s): {', '.join(unsupported)}")

    @classmethod
    def default(cls) -> "Catalog":
        return cls(
            {
                "version": 1,
                "taxonomy": {
                    "contract": ["docs/architecture/**"],
                    "state": ["docs/status/**"],
                    "procedure": ["docs/operations/**"],
                    "evidence": ["docs/evidence/**"],
                    "decision": ["docs/decisions/**"],
                },
                "documents": [],
                "policies": {
                    "auto_remove_new_duplicates": True,
                    "protected": [
                        "docs/legal/**",
                        "docs/public/**",
                        "README.md",
                        "docs/status/production*.md",
                        "docs/status/production/**",
                    ],
                },
            }
        )

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        if not path.exists():
            return cls.default()
        return cls.loads(path.read_text(encoding="utf-8"), source=str(path))

    @classmethod
    def loads(cls, text: str, *, source: str = "catalog") -> "Catalog":
        if yaml is not None:
            parsed = yaml.safe_load(text)
        else:
            parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError(f"Catalog must be a mapping: {source}")
        return cls(parsed)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "taxonomy": self.taxonomy,
            "documents": [record.to_dict() for record in self.documents],
            "policies": self.policies,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if yaml is not None:
            rendered = yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True)
        else:
            rendered = json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"
        path.write_text(rendered, encoding="utf-8")

    def record_for(self, relative_path: str) -> Optional[DocumentRecord]:
        normalized = relative_path.replace("\\", "/")
        for record in self.documents:
            if record.path == normalized:
                return record
        return None

    def classify(self, relative_path: str) -> Optional[str]:
        normalized = relative_path.replace("\\", "/")
        record = self.record_for(normalized)
        if record:
            return record.type
        for document_type, patterns in self.taxonomy.items():
            if any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns):
                return document_type
        return None

    def is_protected(self, relative_path: str) -> bool:
        normalized = relative_path.replace("\\", "/")
        if any(
            fnmatch.fnmatch(normalized, pattern)
            for pattern in self.policies.get("protected", [])
        ):
            return True
        # Production status is a high-impact state even when a repository's
        # local policy forgot to repeat the built-in safety boundary.
        lowered = normalized.lower()
        return (
            lowered == "readme.md"
            or any(part == "production" for part in lowered.split("/"))
            or lowered.endswith("/production.md")
        )

    @property
    def canonical_ref(self) -> Optional[str]:
        value = self.policies.get("canonical_ref")
        return str(value) if value else None

    def ignored(self, relative_path: str) -> bool:
        normalized = relative_path.replace("\\", "/")
        return any(
            fnmatch.fnmatch(normalized, pattern)
            for pattern in self.policies.get("ignore", [])
        )

    def ttl_for(self, record: DocumentRecord) -> Optional[int]:
        if record.ttl_days is not None:
            return int(record.ttl_days)
        return DEFAULT_TTLS.get(record.type)

    def ensure_record(self, relative_path: str, document_type: Optional[str] = None) -> DocumentRecord:
        existing = self.record_for(relative_path)
        if existing:
            return existing
        inferred_type = document_type or self.classify(relative_path) or "contract"
        record = DocumentRecord(path=relative_path, type=inferred_type)
        self.documents.append(record)
        return record

    def duplicate_canonical_records(self) -> Dict[str, List[DocumentRecord]]:
        grouped: Dict[str, List[DocumentRecord]] = {}
        for record in self.documents:
            if record.authority == "canonical" and record.canonical_key:
                grouped.setdefault(f"{record.type}:{record.canonical_key}", []).append(record)
        return {key: value for key, value in grouped.items() if len(value) > 1}

    def records_for_paths(self, paths: Iterable[str]) -> List[DocumentRecord]:
        wanted = set(paths)
        return [record for record in self.documents if record.path in wanted]
