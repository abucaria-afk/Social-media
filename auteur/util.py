"""Utility helpers for safe path handling."""
from __future__ import annotations

import os
from pathlib import Path


def safe_workspace_path(path: str | Path) -> Path:
    """Resolve and validate a user-supplied workspace path.

    Refuse obvious system directories to avoid accidental destructive writes.
    """
    p = Path(path).expanduser().resolve()
    forbidden = [Path("/"), Path("/etc"), Path("/bin"), Path("/usr"), Path("/sbin"), Path("/var")]
    for f in forbidden:
        try:
            if p == f or str(p).startswith(str(f) + os.sep):
                raise ValueError(f"refuse to use system directory as workspace: {p}")
        except Exception:
            # Defensive: if os.stat fails for some reason, treat as unsafe
            raise
    return p
