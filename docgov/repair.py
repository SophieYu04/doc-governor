from __future__ import annotations

from typing import List

from .engine import RepositorySnapshot, changed_dependency_evidence
from .models import DocumentRecord
from .patterns import matches_repo_glob


def repair_candidates(snapshot: RepositorySnapshot) -> List[DocumentRecord]:
    patterns = [
        str(item)
        for item in snapshot.catalog.policies.get("auto_repair_documents", [])
    ]
    return [
        record
        for record in snapshot.catalog.documents
        if record.type in {"contract", "procedure"}
        and any(matches_repo_glob(record.path, pattern) for pattern in patterns)
        and any(
            matches_repo_glob(path, dependency)
            for path in snapshot.changed
            for dependency in record.depends_on
        )
    ]


def build_repair_prompt(snapshot: RepositorySnapshot) -> str:
    candidates = repair_candidates(snapshot)
    if not candidates:
        return ""
    sections: List[str] = []
    for record in candidates:
        changed = changed_dependency_evidence(snapshot, record)
        sources = "\n".join(f"- {item.path}" for item in changed) or "- none"
        sections.append(
            f"Document: {record.path}\n"
            f"Type: {record.type}\n"
            f"Declared dependencies: {', '.join(record.depends_on)}\n"
            f"Changed evidence:\n{sources}"
        )
    documents = "\n\n".join(sections)
    return f"""You are the repository's coding agent. Repair required documentation before this commit.

Inspect the staged code diff and the declared source dependencies below. Update each listed document so its factual claims match the implementation in the working tree. Preserve valid human-authored guidance and edit only what the source change makes inaccurate or incomplete. Do not claim deployment, testing, approval, or verification unless repository evidence proves it. Do not change dates merely to make a document look current. Do not commit.

Required documents:

{documents}

After editing, run the repository's documented verification command. If a claim cannot be grounded, leave the document unchanged and report the blocker instead of inventing content.
"""