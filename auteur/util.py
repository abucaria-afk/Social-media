"""Utility helpers for path safety checks."""
+from __future__ import annotations
+
+import os
+from pathlib import Path
+
+
+def safe_workspace_path(path: str) -> str:
+    """Resolve and validate a workspace path. Refuse obvious system roots.
+
+    Returns the resolved absolute path string or raises ValueError.
+    """
+    if not path:
+        raise ValueError("workspace path must be provided")
+    p = Path(path).expanduser()
+    try:
+        r = p.resolve()
+    except Exception as exc:
+        raise ValueError(f"could not resolve workspace path: {exc}")
+    # Disallow root and system directories
+    forbidden = {Path("/"), Path("/root"), Path("/etc"), Path("/usr"), Path("/bin"), Path("/sbin"), Path("/var")}
+    for f in forbidden:
+        if f == r or str(r).startswith(str(f) + os.sep):
+            raise ValueError(f"refuse workspace path inside system directory: {r}")
+    return str(r)
+
