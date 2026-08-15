@@
 def _normalise_shot(raw: dict) -> dict:
     """Reshape one model shot into what the EDL parser expects."""
     shot = dict(raw)
     ramp = str(shot.get("ramp", "none")).strip().lower()
     if ramp in ("none", "", "constant"):
         # Fall through to the plain speed multiplier.
         shot.pop("ramp", None)
-    shot["transition_in"] = {
-        "kind": shot.pop("transition", "cut"),
-        "duration": shot.pop("transition_duration", 0.0) or 0.0,
-    }
+    # Sanitize transition kind and duration coming from the model payload
+    from ..sanitize import sanitize_transition, sanitize_number, sanitize_text
+
+    t_kind = sanitize_transition(shot.pop("transition", "cut"))
+    t_dur = sanitize_number(shot.pop("transition_duration", 0.0), low=0.0, high=3.0, default=0.0)
+    shot["transition_in"] = {
+        "kind": t_kind,
+        "duration": t_dur or 0.0,
+    }
+    # Ensure note and other textual fields are bounded in length
+    if "note" in shot:
+        shot["note"] = sanitize_text(shot.get("note"), maxlen=200)
+    if "clip" in shot:
+        shot["clip"] = sanitize_text(shot.get("clip"), maxlen=48)
     return shot
