/* Asking the Scholar.
 *
 * Plain ES5-ish and no build step, like the rest of this app. Everything the
 * Scholar says arrives through textContent — its answers are built from video
 * descriptions and documents, which is to say from strangers.
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  var thread = $("thread");
  var field = $("question");
  var send = $("send");

  function say(who, text, unreachable, fromStudy) {
    var li = document.createElement("li");
    li.className = "says " + who + (unreachable ? " unreachable" : "")
      + (fromStudy ? " from-study" : "");
    if (fromStudy) {
      // An answer somebody thought about and a reading-back of notes are
      // different things, and only the label distinguishes them.
      var tag = document.createElement("span");
      tag.className = "says-tag";
      tag.textContent = "read out of what it has studied";
      li.appendChild(tag);
    }
    // Blank lines become paragraphs; nothing else is interpreted.
    String(text).split(/\n\s*\n/).forEach(function (part) {
      var p = document.createElement("p");
      p.className = "said";
      p.textContent = part.trim();
      li.appendChild(p);
    });
    thread.appendChild(li);
    li.scrollIntoView({ block: "end", behavior: "smooth" });
    return li;
  }

  function ask(question) {
    if (!question) { return; }
    say("you", question);
    field.value = "";
    grow();
    send.disabled = true;

    var waiting = say("scholar thinking", "thinking…");

    fetch("/api/scholar/ask", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: question }),
    })
      .then(function (r) { return r.json(); })
      .then(function (payload) {
        waiting.remove();
        // Notes read back are not an outage — they are the Scholar working
        // without a model, so they are not greyed out as unreachable.
        say("scholar", payload.reply || payload.error || "It said nothing.",
            payload.reachable === false && !payload.from_study,
            payload.from_study === true);
        if (typeof payload.learnings === "number") {
          $("knows").textContent = payload.learnings + " learnings";
        }
      })
      .catch(function (err) {
        waiting.remove();
        say("scholar", "It could not be reached: " + err, true);
      })
      .then(function () {
        send.disabled = false;
        field.focus();
      });
  }

  $("asker").addEventListener("submit", function (event) {
    event.preventDefault();
    ask(field.value.trim());
  });

  $("openers").addEventListener("click", function (event) {
    var chip = event.target.closest(".chip");
    if (chip) { ask(chip.dataset.ask); }
  });

  // Enter sends, shift-enter makes a new line — what everybody expects of a
  // box like this, and the on-screen keyboard shows "send" because of
  // enterkeyhint.
  field.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      ask(field.value.trim());
    }
  });

  function grow() {
    field.style.height = "auto";
    field.style.height = Math.min(field.scrollHeight, window.innerHeight * 0.4) + "px";
  }
  field.addEventListener("input", grow);

  // How much it knows, so the page is not silent about whether it has studied.
  fetch("/api/scholar", { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (s) {
      if (s && s.available) {
        $("knows").textContent = s.learnings + " learnings";
      } else {
        $("knows").textContent = "not available";
      }
    })
    .catch(function () { $("knows").textContent = ""; });
})();
