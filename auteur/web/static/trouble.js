/* The bug finder: what went wrong, kept where somebody can see it.
 *
 * Until now a script error on a phone went to a console nobody has open, and
 * the only symptom was a button that did nothing. That is the failure this
 * whole app keeps having, and the one thing that makes it findable is a place
 * the error is written down.
 *
 * Nothing is sent anywhere. There is no telemetry here and there is nowhere to
 * send it to: the report goes to the server on your own machine, which writes
 * it to a file beside the accounts, and to a panel you can read and copy. That
 * is the difference between a bug finder and analytics, and it is the whole
 * difference.
 */
(function (global) {
  "use strict";

  var SEEN = {};        // one report per distinct fault, not one per repeat
  var MOST = 20;

  function fingerprint(report) {
    return [report.what, report.where, report.line].join("|");
  }

  function tell(report) {
    var key = fingerprint(report);
    if (SEEN[key]) { SEEN[key].count += 1; return; }
    if (Object.keys(SEEN).length >= MOST) { return; }
    SEEN[key] = { count: 1 };

    // To the server, which writes it to a file on this machine.
    try {
      fetch("/api/trouble", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(report)
      }).catch(function () { /* offline, or signed out: the panel still shows */ });
    } catch (e) { /* nothing here may throw — see below */ }

    panel(report);
  }

  /* A panel rather than an alert. An alert stops the page, and the most common
   * script error is one that has already broken something else — interrupting
   * on top of that turns a degraded screen into an unusable one. */
  function panel(report) {
    var box = document.getElementById("trouble");
    if (!box) {
      box = document.createElement("div");
      box.id = "trouble";
      box.className = "trouble";
      box.setAttribute("role", "alert");
      document.body.appendChild(box);
    }
    box.innerHTML =
      '<p class="trouble-what">Something in this screen stopped working.</p>' +
      '<p class="trouble-detail"></p>' +
      '<div class="trouble-acts">' +
      '<button type="button" class="trouble-copy">Copy the details</button>' +
      '<button type="button" class="trouble-close">Dismiss</button></div>';
    box.querySelector(".trouble-detail").textContent =
      report.what + "  —  " + report.where + ":" + report.line;
    box.querySelector(".trouble-copy").addEventListener("click", function () {
      var text = JSON.stringify(report, null, 1);
      if (global.navigator.clipboard) {
        global.navigator.clipboard.writeText(text).then(function () {
          box.querySelector(".trouble-copy").textContent = "Copied";
        }).catch(function () {});
      }
    });
    box.querySelector(".trouble-close").addEventListener("click", function () {
      box.remove();
    });
  }

  function report(what, where, line, stack) {
    return {
      what: String(what || "an error with no message").slice(0, 300),
      where: String(where || location.pathname).replace(/^.*\//, "").slice(0, 120),
      line: line || 0,
      stack: String(stack || "").split("\n").slice(0, 6).join("\n").slice(0, 900),
      page: location.pathname,
      at: new Date().toISOString(),
      // Enough to tell a browser apart, and nothing that identifies a person.
      screen: global.innerWidth + "x" + global.innerHeight,
      agent: (global.navigator.userAgent || "").slice(0, 160)
    };
  }

  global.addEventListener("error", function (event) {
    // A failed <img> or <script> raises this too, with no `error` object.
    if (!event.error && !event.message) { return; }
    tell(report(event.message, event.filename, event.lineno,
                event.error && event.error.stack));
  });

  global.addEventListener("unhandledrejection", function (event) {
    var why = event.reason;
    tell(report(
      why && why.message ? why.message : why,
      location.pathname, 0, why && why.stack
    ));
  });

  /* Deliberately last, and deliberately small: everything above has to be
   * incapable of throwing, because an error handler that throws produces a
   * loop that takes the page down harder than the fault it was reporting. */
  global.auteurTrouble = { report: report, tell: tell };
})(window);
