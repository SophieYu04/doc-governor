"""Post-validation for model-proposed prose.

Agent C may re-draft a span of a ``contract`` document. That is the single place
in Doc Governor where a model's words can reach a file, so the words are treated
as a proposal that has to survive a deterministic proof before anything is
written (P4).

The proof is grounding: every factual token in the proposed span — identifiers,
function names, endpoint paths, file paths, config keys, type names — must appear
literally in a source file the document already declares as a dependency. A span
that introduces a date, a version number, or a verification claim is rejected
outright, because none of those are derivable from source (P2) and inventing one
is exactly the failure this project exists to prevent.

A draft that fails any check is discarded whole. There is no partial application:
the fallback is to leave the document as it was and refuse it, which fails safe.
Auto-rewriting a document incorrectly fails dangerous — the document would be
confidently wrong and nobody would notice (P3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

from .patterns import matches_repo_glob


#: A claim about *when* something was true is never derivable from source.
DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
#: Nor is a released version number.
VERSION_PATTERN = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")
#: Nor is a human's assertion that something was checked.
VERIFICATION_PATTERN = re.compile(
    r"\b(verified|verification|validated|confirmed|signed[- ]off|tested in|approved by|last checked)\b"
    r"|驗證|已確認|已驗證",
    re.IGNORECASE,
)

#: Tokens that look like code rather than prose.
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_PATHLIKE = re.compile(r"\b[\w.\-]+(?:/[\w.\-]+)+\b")
#: A leading-slash route such as `/refunds` is an assertion about an interface.
_ROUTE = re.compile(r"(?<![\w/])/[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-{}:]+)*")
#: Bare numbers are facts: ports, limits, quotas, counts. Prose says "three".
_NUMBER = re.compile(r"(?<![\w.])\d+(?![\w.])")
_SNAKE_OR_CAMEL = re.compile(r"\b(?:[a-z]+(?:_[a-z0-9]+)+|[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z]+[A-Z][A-Za-z0-9]*)\b")
_KEBAB = re.compile(r"\b[a-z0-9]+(?:-[a-z0-9]+)+\b")
_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(\)")
_DOTTED = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b")

#: English words that the kebab and dotted heuristics pick up but that no source
#: file is obliged to contain.
_PROSE_ALLOWLIST = frozenset({
    "e.g", "i.e", "etc", "read-only", "write-only", "up-to-date", "well-known",
    "so-called", "long-running", "single-page", "end-to-end", "fail-closed",
    "fail-open", "human-readable", "machine-readable", "self-describing",
})


@dataclass(frozen=True)
class DraftValidation:
    ok: bool
    reason: str
    ungrounded_tokens: List[str]
    checked_tokens: List[str]


def factual_tokens(span: str) -> List[str]:
    """Extract the tokens a proposed span asserts as fact.

    Deliberately over-inclusive. A token wrongly treated as factual causes a
    discard, and a discarded draft only means the document is left alone and
    refused — the safe outcome. A token wrongly treated as prose would let an
    invented identifier through, which is what this module exists to prevent.
    """
    found: List[str] = []
    for pattern in (_BACKTICKED, _PATHLIKE, _ROUTE, _SNAKE_OR_CAMEL, _KEBAB, _CALL, _DOTTED, _NUMBER):
        for match in pattern.finditer(span):
            token = (match.group(1) if match.lastindex else match.group(0)).strip()
            if token and token.lower() not in _PROSE_ALLOWLIST:
                found.append(token)
    ordered: List[str] = []
    for token in found:
        if token not in ordered:
            ordered.append(token)
    return ordered


def _appears(token: str, text: str) -> bool:
    """Match a token as a whole word, not as a substring of a longer identifier.

    A plain ``in`` test would let ``get_user`` be "grounded" by a source that only
    contains ``forget_user_id``, which is precisely the kind of near-miss a model
    invents.
    """
    return re.search(
        r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])", text
    ) is not None


def _grounded(token: str, sources: Dict[str, str]) -> bool:
    if any(_appears(token, text) for text in sources.values()):
        return True
    # `functionName()` in prose is grounded by `functionName` in source.
    bare = token.rstrip("()").strip()
    if bare and bare != token and any(_appears(bare, text) for text in sources.values()):
        return True
    return False


def validate_draft(
    *,
    document_path: str,
    document_type: str,
    declared_dependencies: Sequence[str],
    current_text: str,
    original_span: str,
    proposed_span: str,
    cited_sources: Dict[str, str],
    declared_tokens: Iterable[str] = (),
    protected: bool = False,
    approval: str = "auto",
) -> DraftValidation:
    """Return whether a proposed span may be applied. Anything unproven is a refusal."""
    if document_type != "contract":
        return DraftValidation(
            False,
            f"Only contract documents may be re-drafted; {document_path} is a {document_type} document.",
            [],
            [],
        )
    if protected:
        return DraftValidation(
            False, "Protected legal, public, or production content is never re-drafted.", [], []
        )
    if approval == "human":
        return DraftValidation(
            False, "The catalog requires human approval for this document.", [], []
        )
    if not original_span.strip() or not proposed_span.strip():
        return DraftValidation(False, "A draft must replace a non-empty span with non-empty text.", [], [])
    occurrences = current_text.count(original_span)
    if occurrences != 1:
        return DraftValidation(
            False,
            f"The original span matches the current document {occurrences} time(s); exactly one is required.",
            [],
            [],
        )
    if not cited_sources:
        return DraftValidation(False, "A draft must cite at least one declared source file.", [], [])
    undeclared = sorted(
        path
        for path in cited_sources
        if not any(matches_repo_glob(path, pattern) for pattern in declared_dependencies)
    )
    if undeclared:
        return DraftValidation(
            False,
            f"The draft cites sources the document does not declare as dependencies: {', '.join(undeclared)}.",
            [],
            [],
        )
    if DATE_PATTERN.search(proposed_span):
        return DraftValidation(False, "The draft introduces a date, which no source file can prove.", [], [])
    if VERSION_PATTERN.search(proposed_span):
        return DraftValidation(
            False, "The draft introduces a version number, which no source file can prove.", [], []
        )
    if VERIFICATION_PATTERN.search(proposed_span):
        return DraftValidation(
            False, "The draft introduces a verification claim, which is a human claim and not derivable.", [], []
        )

    checked: List[str] = list(factual_tokens(proposed_span))
    for token in declared_tokens:
        value = str(token).strip()
        if value and value not in checked:
            checked.append(value)
    # A token already present in the span being replaced was in the document
    # before the model touched it; it is not something the model asserted.
    original_tokens = set(factual_tokens(original_span))
    ungrounded = [
        token
        for token in checked
        if token not in original_tokens and not _grounded(token, cited_sources)
    ]
    if ungrounded:
        return DraftValidation(
            False,
            "The draft asserts tokens that appear in no cited source file: " + ", ".join(sorted(ungrounded)) + ".",
            sorted(ungrounded),
            checked,
        )
    return DraftValidation(True, "Every factual token in the draft is grounded in a cited source file.", [], checked)


def apply_span(current_text: str, original_span: str, proposed_span: str) -> Optional[str]:
    """Replace the span, or return None when it no longer matches exactly once."""
    if current_text.count(original_span) != 1:
        return None
    return current_text.replace(original_span, proposed_span, 1)
