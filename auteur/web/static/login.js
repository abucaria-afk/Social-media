/* Signing in, forgetting, and resetting. Three screens, no framework. */

(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var screens = {
    signin: $("screen-signin"),
    forgot: $("screen-forgot"),
    reset: $("screen-reset")
  };

  function show(name) {
    Object.keys(screens).forEach(function (key) { screens[key].hidden = key !== name; });
    window.scrollTo(0, 0);
  }

  function say(element, message, isError) {
    element.textContent = message;
    element.hidden = !message;
    if (isError !== undefined) { element.classList.toggle("error", !!isError); }
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store"
    }).then(function (response) {
      return response.json()
        .catch(function () { return {}; })
        .then(function (payload) { return { ok: response.ok, payload: payload }; });
    });
  }

  // -- appearance -----------------------------------------------------------
  // Duplicated from app.js rather than shared: this page must work before the
  // reader has an account, so it loads nothing the signed-in app needs.

  function wireChoices(container, onPick) {
    container.addEventListener("click", function (event) {
      var button = event.target.closest(".choice");
      if (!button) { return; }
      Array.prototype.forEach.call(container.querySelectorAll(".choice"), function (other) {
        other.classList.toggle("is-on", other === button);
        other.setAttribute("aria-checked", other === button ? "true" : "false");
      });
      onPick(button.dataset.value);
    });
  }

  function applyTheme(mode) {
    if (mode === "light" || mode === "dark") {
      document.documentElement.setAttribute("data-theme", mode);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    try { localStorage.setItem("auteur-theme", mode); } catch (e) { /* private mode */ }
  }

  var savedTheme = "system";
  try { savedTheme = localStorage.getItem("auteur-theme") || "system"; } catch (e) { /* */ }
  wireChoices($("appearance"), applyTheme);
  Array.prototype.forEach.call($("appearance").querySelectorAll(".choice"), function (button) {
    var on = button.dataset.value === savedTheme;
    button.classList.toggle("is-on", on);
    button.setAttribute("aria-checked", on ? "true" : "false");
  });

  // -- show/hide a password -------------------------------------------------

  function wireReveal(buttonId, fieldId) {
    $(buttonId).addEventListener("click", function () {
      var field = $(fieldId);
      var hidden = field.type === "password";
      field.type = hidden ? "text" : "password";
      $(buttonId).textContent = hidden ? "Hide" : "Show";
      $(buttonId).setAttribute("aria-label", hidden ? "Hide password" : "Show password");
      field.focus();
    });
  }
  wireReveal("show-password", "password");
  wireReveal("show-new", "new-password");

  // -- sign in --------------------------------------------------------------

  $("signin-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var problem = $("signin-error");
    problem.hidden = true;

    var username = $("username").value.trim();
    var password = $("password").value;
    if (!username || !password) {
      say(problem, "Fill in both boxes.", true);
      return;
    }

    $("signin-go").disabled = true;
    $("signin-go").textContent = "Signing in…";
    post("/api/login", { username: username, password: password })
      .then(function (result) {
        if (!result.ok) {
          say(problem, result.payload.error || "That did not work.", true);
          $("signin-go").disabled = false;
          $("signin-go").textContent = "Sign in";
          return;
        }
        // Full navigation rather than a fetch, so the browser offers to save
        // the password and the app loads with the cookie already set.
        window.location.href = "/";
      })
      .catch(function () {
        say(problem, "Could not reach the edit room. Is it still running?", true);
        $("signin-go").disabled = false;
        $("signin-go").textContent = "Sign in";
      });
  });

  $("to-forgot").addEventListener("click", function () {
    $("forgot-who").value = $("username").value.trim();
    say($("forgot-said"), "");
    show("forgot");
  });

  $("back-to-signin").addEventListener("click", function () { show("signin"); });

  // -- forgot ---------------------------------------------------------------

  $("forgot-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var who = $("forgot-who").value.trim();
    if (!who) { return; }

    $("forgot-go").disabled = true;
    post("/api/forgot", { username: who })
      .then(function (result) {
        var message = result.payload.message || "If that account exists, a link is on its way.";
        if (result.payload.via === "console") {
          message += " No email is set up here, so it has been printed in the " +
                     "window where the edit room is running.";
        }
        say($("forgot-said"), message, false);
        $("forgot-go").disabled = false;
      })
      .catch(function () {
        say($("forgot-said"), "Could not reach the edit room.", true);
        $("forgot-go").disabled = false;
      });
  });

  // -- reset ----------------------------------------------------------------

  var token = new URLSearchParams(window.location.search).get("token");
  if (token) {
    $("subtitle").textContent = "Nearly there.";
    show("reset");
  }

  $("reset-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var problem = $("reset-error");
    problem.hidden = true;

    var password = $("new-password").value;
    if (password.length < 12) {
      say(
        problem,
        "Use at least 12 characters. Several ordinary words in a row beat one clever word.",
        true
      );
      return;
    }

    $("reset-go").disabled = true;
    post("/api/reset", { token: token, password: password })
      .then(function (result) {
        if (!result.ok) {
          say(problem, result.payload.error || "That did not work.", true);
          $("reset-go").disabled = false;
          return;
        }
        // Straight into signing in, with the query string dropped so the used
        // token stops sitting in the address bar and the history.
        window.history.replaceState({}, "", "/login");
        $("subtitle").textContent = "Password changed. Sign in with it.";
        show("signin");
        $("password").focus();
      })
      .catch(function () {
        say(problem, "Could not reach the edit room.", true);
        $("reset-go").disabled = false;
      });
  });
})();
