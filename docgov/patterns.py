from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=512)
def _compile_repo_glob(pattern: str) -> re.Pattern[str]:
    """Compile a Git-style repository glob where only ** crosses `/`."""
    pattern = pattern.replace("\\", "/")
    index = 0
    parts = ["^"]
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                while index + 1 < len(pattern) and pattern[index + 1] == "*":
                    index += 1
                if index + 1 < len(pattern) and pattern[index + 1] == "/":
                    index += 1
                    parts.append("(?:.*/)?")
                else:
                    parts.append(".*")
            else:
                parts.append("[^/]*")
        elif character == "?":
            parts.append("[^/]")
        elif character == "[":
            closing = pattern.find("]", index + 1)
            if closing == -1:
                parts.append(r"\[")
            else:
                content = pattern[index + 1:closing]
                if content.startswith("!"):
                    content = "^" + content[1:]
                elif content.startswith("^"):
                    content = "\\" + content
                parts.append("[" + content.replace("\\", r"\\") + "]")
                index = closing
        else:
            parts.append(re.escape(character))
        index += 1
    parts.append("$")
    return re.compile("".join(parts))


def matches_repo_glob(path: str, pattern: str) -> bool:
    normalized = path.replace("\\", "/")
    return _compile_repo_glob(pattern).fullmatch(normalized) is not None
