@@
 def xfade_spec(kind: str, duration: float, offset: float) -> str:
@@
-    offset = max(offset, 0.0)
+    offset = sanitize_number(offset, low=0.0, high=10.0, default=0.0)
@@
     builtin = BUILTIN.get(kind, "fade")
     return f"xfade=transition={builtin}:duration={duration:.4f}:offset={offset:.4f}"
