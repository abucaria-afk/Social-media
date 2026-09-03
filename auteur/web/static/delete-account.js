/* Deleting an account from outside the app.
 *
 * The profile screen has this same button, and this page does not reimplement
 * it: it signs in with the ordinary sign-in endpoints — two-step included —
 * and then calls the one erasure the profile calls. Nothing here checks a
 * password itself, so this page adds no second way in and no unthrottled
 * place to guess one. The account lockout that protects /api/login protects
 * this by construction.
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  var ticket = null;

  function show(name) {
    ["what", "done"].forEach(function (n) {
      $("screen-" + n).hidden = n !== name;
    });
  }

  function say(message) {
    var box = $("delete-error");
    box.textContent = message || "";
    box.hidden = !message;
  }

  function post(where, body) {
    return fetch(where, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, payload: d }; });
    });
  }

  function ready() {
    $("delete-go").disabled = false;
    $("delete-go").textContent = "Delete my account";
  }

  $("delete-back").addEventListener("click", function () {
    window.location.href = "/login";
  });

  $("show-password").addEventListener("click", function () {
    var field = $("password");
    var hidden = field.type === "password";
    field.type = hidden ? "text" : "password";
    this.textContent = hidden ? "Hide" : "Show";
    this.setAttribute("aria-label", hidden ? "Hide password" : "Show password");
  });

  /* The erasure. Same endpoint, same payload, same password check as the
   * button in the profile — this page has only arranged to have a session. */
  function erase(password, confirm) {
    return post("/api/profile/delete", { password: password, confirm: confirm })
      .then(function (result) {
        if (!result.ok) {
          say(result.payload.error || "That did not work.");
          ready();
          return;
        }
        // Said with the count because "it is gone" is a claim, and a number
        // is the only part of it you can check. Nought films is a sentence of
        // its own rather than "its 0 films", which is how a template reads
        // when nobody tried the empty case.
        var films = result.payload.films || 0;
        var made =
          films === 0
            ? "The account and everything it made"
            : films === 1
              ? "The account, the one film in it and everything else it made"
              : "The account, its " + films + " films and everything else it made";
        $("done-said").textContent = made + " have been removed from this machine.";
        show("done");
      });
  }

  $("delete-form").addEventListener("submit", function (event) {
    event.preventDefault();
    say("");

    var who = $("who").value.trim();
    var password = $("password").value;
    var confirm = $("confirm").value.trim();

    if (!who || !password) {
      say("Your username and your password, both.");
      return;
    }
    // Asked for before anything is sent, so a mistyped confirmation is not a
    // sign-in attempt against the lockout.
    if (confirm.toLowerCase() !== "delete") {
      say('Type the word "delete" to confirm.');
      return;
    }

    $("delete-go").disabled = true;
    $("delete-go").textContent = "Deleting…";

    if (ticket) {
      // Past the password already: this submit is the code.
      post("/api/login/step2", { ticket: ticket, code: $("step2-code").value.trim() })
        .then(function (got) {
          if (!got.ok) {
            // The ticket is spent either way, so a wrong code starts over
            // rather than being guessed at again on the same one.
            ticket = null;
            $("step2").hidden = true;
            say(got.payload.error || "That code did not match.");
            ready();
            return;
          }
          return erase(password, confirm);
        })
        .catch(function () {
          say("Could not reach it. Is the edit room still running?");
          ready();
        });
      return;
    }

    post("/api/login", { username: who, password: password })
      .then(function (result) {
        if (!result.ok) {
          say(result.payload.error || "That did not work.");
          ready();
          return;
        }
        if (result.payload && result.payload.needs === "code") {
          ticket = result.payload.ticket;
          $("step2").hidden = false;
          $("step2-code").focus();
          say("");
          ready();
          return;
        }
        return erase(password, confirm);
      })
      .catch(function () {
        say("Could not reach it. Is the edit room still running?");
        ready();
      });
  });
})();
