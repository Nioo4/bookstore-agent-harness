"""Content identity of the actual Agent runtime, including dirty source changes."""
from __future__ import annotations

import hashlib
import base64
from pathlib import Path

from app.models.configuration import AGENT_ROOT
from app.trace.errors import TraceError


def runner_build_version(root: Path = AGENT_ROOT) -> str:
    digest = hashlib.sha256()
    files = []
    for directory in ("app", "rpc", "config", "prompts", "pricing"):
        folder = root / directory
        if not folder.is_dir():
            raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "Agent runtime build inputs are missing")
        files.extend(path for path in folder.rglob("*") if path.is_file()
            and path.suffix in {".py", ".yaml", ".yml", ".json", ".md", ".txt"}
            and "__pycache__" not in path.parts and not path.name.startswith("."))
    for name in ("requirements.txt", "requirements.lock", "pyproject.toml", "uv.lock", "Dockerfile"):
        if (root / name).is_file():
            files.append(root / name)
    for path in sorted(files, key=lambda value:value.relative_to(root).as_posix()):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "Agent build input escapes runtime root")
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    # Preserve all 256 bits while fitting the frozen VARCHAR(64) version fields.
    return "evaluation-v2+sha256:" + base64.urlsafe_b64encode(digest.digest()).decode("ascii").rstrip("=")
