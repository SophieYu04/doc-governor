from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

from .git_tools import tracked_paths


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _document_markers(root: Path) -> Dict[str, Any]:
    markers: Dict[str, Any] = {}
    pattern = re.compile(r"<!--\s*docgov:supabase-inventory\s*(\{.*?\})\s*-->", re.DOTALL)
    tracked_markdown = tracked_paths(root, suffix=".md")
    paths = [root / relative for relative in tracked_markdown]
    if not paths:
        paths = list(root.rglob("*.md"))
    for path in paths:
        relative = path.relative_to(root).as_posix()
        for match in pattern.finditer(_read(path)):
            try:
                value = json.loads(match.group(1))
            except json.JSONDecodeError:
                value = {"invalid": True, "raw": match.group(1)}
            markers[relative] = value
    return markers


def inventory(root: Path) -> Dict[str, Any]:
    supabase_root = root
    if not (root / "supabase" / "config.toml").exists():
        candidates = [
            path.parent.parent
            for path in root.rglob("supabase/config.toml")
            if not any(part.startswith(".") for part in path.relative_to(root).parts)
        ]
        if len(candidates) == 1:
            supabase_root = candidates[0]
    migrations_dir = supabase_root / "supabase" / "migrations"
    functions_dir = supabase_root / "supabase" / "functions"
    config = _read(supabase_root / "supabase" / "config.toml")
    migration_count = len(list(migrations_dir.glob("*.sql"))) if migrations_dir.exists() else 0
    source_functions = sorted(
        item.name
        for item in functions_dir.iterdir()
        if item.is_dir() and item.name != "_shared" and (item / "index.ts").exists()
    ) if functions_dir.exists() else []
    config_functions = sorted(re.findall(r"^\[functions\.([^\]]+)\]$", config, re.MULTILINE))
    jwt_flags: Dict[str, bool] = {}
    current: str | None = None
    for line in config.splitlines():
        header = re.match(r"^\[functions\.([^\]]+)\]$", line.strip())
        if header:
            current = header.group(1)
            continue
        flag = re.match(r"^verify_jwt\s*=\s*(true|false)", line.strip())
        if current and flag:
            jwt_flags[current] = flag.group(1) == "true"
    bucket_names = set(re.findall(r"['\"]([a-z0-9-]+)['\"]", "\n".join(
        _read(path) for path in migrations_dir.glob("*.sql")
    ))) if migrations_dir.exists() else set()
    buckets = sorted(bucket_names & {"post-staging", "post-media", "avatars", "security-evidence", "template-kit-media"})
    config_path = supabase_root / "supabase" / "config.toml"
    try:
        config_relative = config_path.relative_to(root).as_posix()
    except ValueError:
        config_relative = "supabase/config.toml"
    return {
        "config_path": config_relative if config_path.exists() else None,
        "migration_count": migration_count,
        "source_functions": source_functions,
        "config_functions": config_functions,
        "jwt_flags": jwt_flags,
        "storage_buckets": buckets,
        "document_markers": _document_markers(root),
    }


def evidence_for_change(root: Path, relative_path: str) -> str | None:
    if "supabase" in relative_path.split("/"):
        return relative_path
    if relative_path.startswith("FirstgramIOS/"):
        return relative_path
    return None
