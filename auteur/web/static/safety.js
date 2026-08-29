/* Reporting and blocking, on every screen that can raise them.
 *
 * One sheet, built once and appended to whatever page loads this file, rather
 * than three copies in three HTML files. The feed, a profile and a conversation
 * all need exactly this dialog, and a dialog that is right on two of them is
 * the thing this project keeps finding at the bottom of its own bugs.
 *
 * What it is for: the App Store requires an app carrying other people's content
 * to be able to report it and to block whoever sent it (guideline 1.2). Both
 * are real here. Blocking takes effect at once, in both directions, and asks
 * nobody. Reporting goes to the person whose computer this is — the sheet says
 * so, because a reporting flow that implies a review team nobody has is worse
 * than one that is honest about being small.
 */
(function () {
  "use strict";

  var REASONS = [
    ["sexual", "Sexual or nude content"],
    ["violence", "Violence or threats"],
    ["harassment", "Harassment or bullying"],
    ["hate", "Hate speech"],
    ["illegal", "Something illegal"],
    ["child-safety", "Something involving a child"],
    ["spam", "Spam"],
    ["other", "Something else"]
  ];

  var sheet = null;
  var target = null;   // { kind, about, about_who, name }

  function escaped(text) {
    var box = document.createElement("span");
    box.textContent = text == null ? "" : String(text);
    return box.innerHTML;
  }

  function build() {
    if (sheet) { return sheet; }
    sheet = document.createElement("div");
    sheet.className = "sheet";
    sheet.id = "safety-sheet";
    sheet.hidden = true;
    sheet.innerHTML =
      '<div class="sheet-scrim" data-safety-close="1"></div>' +
      '<div class="sheet-body" role="dialog" aria-modal="true" aria-label="Report">' +
        '<div class="sheet-grip" aria-hidden="true"></div>' +
        '<h2 class="sheet-title" id="safety-title">Report</h2>' +
        '<p class="sheet-note" id="safety-who"></p>' +
        '<div class="choices reasons" id="safety-reasons" role="radiogroup" ' +
          'aria-label="What is wrong with it">' +
          REASONS.map(function (row) {
            return '<button type="button" class="choice" role="radio" aria-checked="false" ' +
              'data-value="' + row[0] + '">' + escaped(row[1]) + "</button>";
          }).join("") +
        "</div>" +
        '<label class="field-label" for="safety-note">Anything else worth saying</label>' +
        '<textarea id="safety-note" rows="2" maxlength="600" ' +
          'placeholder="Optional"></textarea>' +
        '<label class="check"><input type="checkbox" id="safety-block" checked>' +
          '<span id="safety-block-label">Block them as well</span></label>' +
        '<p class="sheet-note" id="safety-where">' +
          "This goes to whoever runs this copy of Atlas — the person whose " +
          "computer it is. Blocking happens straight away and needs nobody." +
        "</p>" +
        '<p class="error" id="safety-error" role="alert" hidden></p>' +
        /* Not `danger-go`. Reporting is not destructive to the person doing
           it, and a red primary button reads as "are you sure" — which is
           exactly the hesitation this control should not add. Red is kept for
           the one action that deletes something. */
        '<button type="button" class="go" id="safety-send">Report it</button>' +
        '<button type="button" class="ghost" data-safety-close="1">Cancel</button>' +
      "</div>";
    document.body.appendChild(sheet);

    sheet.addEventListener("click", function (event) {
      if (event.target.closest("[data-safety-close]")) { close(); return; }
      var choice = event.target.closest("#safety-reasons .choice");
      if (choice) {
        Array.prototype.forEach.call(
          sheet.querySelectorAll("#safety-reasons .choice"),
          function (other) {
            var on = other === choice;
            other.classList.toggle("is-on", on);
            other.setAttribute("aria-checked", on ? "true" : "false");
          }
        );
      }
    });

    document.getElementById("safety-send").addEventListener("click", send);
    return sheet;
  }

  function close() {
    if (sheet) { sheet.hidden = true; }
    document.body.classList.remove("sheet-open");
  }

  /* `kind` is "film", "message" or "person"; `about` is its id or the
     username; `who` is whose it is, and the server checks that rather than
     trusting it — a report that names whoever the page said it names is a
     report anybody could file against anybody. */
  function open(kind, about, who, name) {
    build();
    target = { kind: kind, about: about, about_who: who, name: name || who };
    document.getElementById("safety-title").textContent =
      kind === "person" ? "Report this person" : "Report this " + kind;
    document.getElementById("safety-who").textContent =
      kind === "person" ? "" : "By " + target.name + ".";
    document.getElementById("safety-block-label").textContent =
      "Block " + target.name + " as well";
    document.getElementById("safety-block").checked = true;
    document.getElementById("safety-note").value = "";
    document.getElementById("safety-error").hidden = true;
    Array.prototype.forEach.call(sheet.querySelectorAll("#safety-reasons .choice"), function (b) {
      b.classList.remove("is-on");
      b.setAttribute("aria-checked", "false");
    });
    sheet.hidden = false;
    document.body.classList.add("sheet-open");
  }

  function send() {
    var picked = sheet.querySelector("#safety-reasons .choice.is-on");
    var problem = document.getElementById("safety-error");
    if (!picked) {
      problem.textContent = "Pick what is wrong with it.";
      problem.hidden = false;
      return;
    }
    var button = document.getElementById("safety-send");
    button.disabled = true;
    problem.hidden = true;
    fetch("/api/report", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind: target.kind,
        about: target.about,
        about_who: target.about_who,
        reason: picked.dataset.value,
        note: document.getElementById("safety-note").value,
        block: document.getElementById("safety-block").checked
      })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (got) {
        button.disabled = false;
        if (!got.ok) {
          problem.textContent = got.d.error || "That could not be sent.";
          problem.hidden = false;
          return;
        }
        close();
        if (window.auteurSafety.onDone) { window.auteurSafety.onDone(got.d); }
        note(got.d.blocked
          ? "Reported, and blocked. You will not see them again."
          : "Reported. Whoever runs this copy will see it.");
      })
      .catch(function () {
        button.disabled = false;
        problem.textContent = "Could not reach the app.";
        problem.hidden = false;
      });
  }

  /* A line that says what happened and takes itself away. A report that
     produces no visible response is a button people press twice. */
  function note(text) {
    var said = document.createElement("p");
    said.className = "said";
    said.setAttribute("role", "status");
    said.textContent = text;
    document.body.appendChild(said);
    setTimeout(function () { said.remove(); }, 4200);
  }

  function block(who, wanted) {
    return fetch("/api/profiles/" + encodeURIComponent(who) + "/block", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ block: !!wanted })
    }).then(function (r) { return r.ok ? r.json() : null; });
  }

  window.auteurSafety = { open: open, block: block, said: note, onDone: null };
})();
