@@
-    settings = settings or Settings()
-    space = Workspace(workspace or Path.cwd() / "auteur-work")
+    settings = settings or Settings()
+    # Validate workspace path to avoid accidental writes to system directories
+    from .util import safe_workspace_path
+
+    if workspace is None:
+        root = Path.cwd() / "auteur-work"
+    else:
+        root = safe_workspace_path(workspace)
+    space = Workspace(root)
