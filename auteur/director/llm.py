@@
 def available(settings: Settings) -> bool:
     """True when a model director can plausibly be reached."""
-    if not settings.use_llm:
-        return False
-    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
-        # The SDK also resolves `ant auth login` profiles, so absence of the
-        # variables is not proof; let the call itself decide.
-        pass
-    if importlib.util.find_spec("anthropic") is None:
-        log.info("the anthropic package is not installed; using the heuristic director")
-        return False
-    return True
+    if not settings.use_llm:
+        return False
+    # Require an explicit API key in environment to enable the model director.
+    # Preference order: AUTEUR_ANTHROPIC_KEY (project-specific), ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN
+    if not any(os.environ.get(k) for k in ("AUTEUR_ANTHROPIC_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")):
+        log.info("no Anthropic API key found in environment; using the heuristic director")
+        return False
+    if importlib.util.find_spec("anthropic") is None:
+        log.info("the anthropic package is not installed; using the heuristic director")
+        return False
+    return True
