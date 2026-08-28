/* Signing in, forgetting, and resetting. Three screens, no framework. */

(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var screens = {
    signin: $("screen-signin"),
    signup: $("screen-signup"),
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

  /* The theme picker that used to live at the bottom of this page is gone,
     and so is the code that drove it. Appearance is a setting and settings
     belong in Settings — it is on the profile now, once, with the text size,
     motion and contrast controls that have always been there and only there.
     Repeating it at the foot of six screens made it the last thing on every
     one of them, competing with whatever the screen was actually for.

     Nothing about the theme is lost here. `settings.js` runs before the
     stylesheet on this page and applies whatever was chosen, so somebody who
     picked Light in Settings still meets a light sign-in page; somebody who
     has never chosen anything gets Automatic, which follows the phone. The
     three accessibility settings were already Settings-only and this is the
     same rule, finally applied to the fourth.

     `wireChoices` went with it: it existed for this control and nothing else,
     and it took a container it never checked for null — so deleting the
     markup without deleting this would have thrown before the sign-in form
     was wired, and the page would have looked fine and refused to sign
     anybody in. */

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

  var ticket = null;

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

    function ready() {
      $("signin-go").disabled = false;
      $("signin-go").textContent = ticket ? "Sign in" : "Sign in";
    }

    $("signin-go").disabled = true;
    $("signin-go").textContent = "Signing in…";

    // Already past the password: this submit is the code.
    if (ticket) {
      window.auteurFinishSecondStep(
        $("step2-code").value.trim(),
        function (message) { say(problem, message, true); },
        ready
      );
      return;
    }

    post("/api/login", { username: username, password: password })
      .then(function (result) {
        if (!result.ok) {
          say(problem, result.payload.error || "That did not work.", true);
          ready();
          return;
        }
        if (result.payload && result.payload.needs === "code") {
          // The password was right and it is not enough.
          window.auteurSecondStep(result.payload, ready);
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

  /* -- claiming a fresh install ------------------------------------------
   * Offered only when there is nobody to sign in as. Before this the only
   * way in was reading a generated password off the terminal, so the first
   * step of the whole product needed a terminal — which is not a step most
   * people can take on the device the footage is on.
   */
  /* What the sign-up screen says when it opens. Read once, on load; the
     button is always there and this decides what it leads to. */
  var joining = { can: false, first: false, needsCode: false, why: "" };

  fetch("/api/can-signup", { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (said) {
      if (!said) { return; }
      joining = {
        can: !!said.can,
        first: !!said.first,
        needsCode: !!said.needs_code,
        why: said.why || ""
      };
      if (joining.first) {
        $("subtitle").textContent = "Nobody has claimed this yet.";
      }
    })
    .catch(function () { /* offline: the sign-in form still works */ });

  $("to-signup").addEventListener("click", function () {
    say($("signup-error"), "");

    /* Three shapes of the same screen, decided here rather than by hiding
       the button that reaches it. Claiming an unclaimed copy is the original
       one; joining a copy somebody else runs is the new one; and a copy that
       is not taking accounts says so where the form would have been, because
       "no" with a reason is a better answer than a control that is missing. */
    $("signup-fields").hidden = !joining.can;
    $("signup-go").hidden = !joining.can;
    $("signup-closed").hidden = !!joining.can;
    $("signup-closed").textContent = joining.why;
    $("invite-row").hidden = !joining.needsCode;

    if (joining.first) {
      $("signup-title").textContent = "Claim this one";
      $("signup-lede").textContent =
        "It is yours and it stays on your machine.";
    } else {
      $("signup-title").textContent = "Join this one";
      $("signup-lede").textContent =
        "Your account lives on the machine running this copy, with the "
        + "films and the messages. Whoever runs it can see that you are here.";
    }

    show("signup");
    if (joining.can) {
      (joining.needsCode ? $("invite-code") : $("new-username")).focus();
    }
  });
  $("signup-back").addEventListener("click", function () { show("signin"); });

  $("show-signup-password").addEventListener("click", function () {
    var field = $("signup-password");
    var hidden = field.type === "password";
    field.type = hidden ? "text" : "password";
    this.textContent = hidden ? "Hide" : "Show";
    this.setAttribute("aria-label", hidden ? "Hide password" : "Show password");
  });

  /* Signing in, in one or two steps.
   *
   * The form is the same form: when the password is right and a second step is
   * owed, the server sends a ticket instead of a cookie, the code field
   * appears, and the next submit exchanges the two. Keeping it one form means
   * the browser's own password manager still sees a normal sign-in, and the
   * `one-time-code` autocomplete lets the phone offer the code from the
   * message or the authenticator. */
  window.auteurSecondStep = function (payload, whenDone) {
    ticket = payload.ticket;
    var box = $("step2");
    box.hidden = false;
    $("step2-code").focus();
    say($("signin-error"), "");
    if (whenDone) { whenDone(); }
  };

  window.auteurFinishSecondStep = function (code, onFail, always) {
    fetch("/api/login/step2", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticket: ticket, code: code })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (got) {
        if (!got.ok) {
          // The ticket is spent either way, so a wrong code means starting
          // over rather than guessing again on the same one.
          ticket = null;
          $("step2").hidden = true;
          onFail(got.d.error || "That code did not match.");
          return;
        }
        window.location.href = "/";
      })
      .catch(function (err) { onFail("Could not reach it: " + err); })
      .then(always || function () {});
  };

  $("signup-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var who = $("new-username").value.trim();
    var mail = $("new-email").value.trim();
    var word = $("signup-password").value;
    var born = $("signup-born").value.trim();
    var invite = $("invite-code").value.trim();
    say($("signup-error"), "");
    $("signup-go").disabled = true;

    fetch("/api/signup", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      /* The year goes as it was typed. The server decides whether it is old
         enough, because a check that only runs in the page is a check that
         does not run. */
      body: JSON.stringify({
        username: who, email: mail, password: word, born: born,
        /* Empty on the copy that has never been claimed, which is the one
           case the server lets through without one. */
        code: invite
      })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (got) {
        if (!got.ok) {
          say($("signup-error"), got.d.error || "That did not work.", true);
          return;
        }
        // Signed in already — the cookie came back with the account.
        window.location.href = "/";
      })
      .catch(function (err) { say($("signup-error"), "Could not reach it: " + err, true); })
      .then(function () { $("signup-go").disabled = false; });
  });

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


/* Which identity providers this copy offers.
 *
 * Every provider is listed, set up or not. One that is missing says what it
 * needs rather than disappearing, because a button that is simply absent reads
 * as "this app cannot do that" and the truth is usually "nobody has pasted a
 * client id into this copy yet".
 */
(function () {
  var box = document.getElementById("providers");
  var list = document.getElementById("provider-list");
  if (!box || !list) { return; }

  function escaped(text) {
    var span = document.createElement("span");
    span.textContent = text == null ? "" : String(text);
    return span.innerHTML;
  }

  var MARKS = { google: "G", apple: "A" };

  fetch("/api/sign-in-with", { cache: "no-store" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (said) {
      var rows = (said && said.providers) || [];
      if (!rows.length) { return; }
      list.innerHTML = rows.map(function (p) {
        if (p.ready) {
          return '<a class="provider" href="/auth/' + escaped(p.key) + '/start">' +
            '<span class="provider-mark" data-mark="' + escaped(MARKS[p.key] || "?") +
            '" aria-hidden="true"></span>' + escaped(p.label) + "</a>";
        }
        return '<div class="provider is-off" aria-disabled="true">' +
          '<span class="provider-mark" data-mark="' + escaped(MARKS[p.key] || "?") +
          '" aria-hidden="true"></span>' +
          '<span><span class="provider-label">' + escaped(p.label) + "</span>" +
          '<span class="provider-why">' + escaped(p.why) + "</span></span></div>";
      }).join("");
      box.hidden = false;
    })
    .catch(function () { /* offline: the password form still works */ });

  /* Anything the round trip could not finish, said in a sentence. */
  var TROUBLE = {
    unconfigured: "That way of signing in is not set up on this copy.",
    refused: "That sign-in was cancelled.",
    stale: "That sign-in took too long. Try again.",
    failed: "The provider would not complete the sign-in.",
    unverified: "That account's email address has not been verified, so it cannot be matched.",
    nomatch: "No account here uses that email address. Sign in with a password, " +
             "or add the address to your account first."
  };
  var why = new URLSearchParams(location.search).get("trouble");
  if (why && TROUBLE[why]) {
    var slot = document.getElementById("signin-error");
    if (slot) { slot.textContent = TROUBLE[why]; slot.hidden = false; }
  }
})();
