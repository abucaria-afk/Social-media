@@
 def _run_edit(args: argparse.Namespace) -> int:
     from .agent import direct
     from .config import Settings, resolve_format, resolve_quality
+    from .sanitize import sanitize_text
@@
     try:
         formats = tuple(resolve_format(name) for name in args.format.split(",") if name.strip())
         quality = resolve_quality(args.quality)
     except ValueError as exc:
         print(f"error: {exc}", file=sys.stderr)
         return 2
     if not formats:
         print("error: no delivery format requested", file=sys.stderr)
         return 2
@@
-    try:
-        production = direct(
-            args.inputs, args.prompt, settings=settings,
-            workspace=args.out, formats=formats, duration=args.duration,
-        )
+    # Validate input paths exist and are accessible before handing them to the agent.
+    from pathlib import Path
+
+    validated_inputs = []
+    for entry in args.inputs:
+        p = Path(entry).expanduser()
+        try:
+            resolved = p.resolve()
+        except Exception as exc:
+            print(f"error: could not resolve input {entry}: {exc}", file=sys.stderr)
+            return 2
+        if not (resolved.is_file() or resolved.is_dir()):
+            print(f"error: input not found or not a file/directory: {resolved}", file=sys.stderr)
+            return 2
+        validated_inputs.append(str(resolved))
+
+    try:
+        production = direct(
+            validated_inputs, args.prompt, settings=settings,
+            workspace=args.out, formats=formats, duration=args.duration,
+        )
