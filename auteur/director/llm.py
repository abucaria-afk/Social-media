@@
 def _normalise_shot(raw: dict) -> dict:
@@
-    t_kind = sanitize_transition(shot.pop("transition", "cut"))
-    t_dur = sanitize_number(shot.pop("transition_duration", 0.0), low=0.0, high=3.0, default=0.0)
+    t_kind = sanitize_transition(shot.pop("transition", "cut"))
+    t_dur = sanitize_number(shot.pop("transition_duration", 0.0), low=0.0, high=3.0, default=0.0)
@@
     if "note" in shot:
         shot["note"] = sanitize_text(shot.get("note"), maxlen=200)
     if "clip" in shot:
         shot["clip"] = sanitize_text(shot.get("clip"), maxlen=48)
+    # Clamp numeric fields that may influence ffmpeg filters
+    if "speed" in shot:
+        shot["speed"] = sanitize_number(shot.get("speed"), low=0.1, high=10.0, default=1.0)
     return shot
