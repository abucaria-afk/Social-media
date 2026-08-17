/* Linking Instagram and TikTok.
 *
 * A link records which account is yours. It is not a key to it: nothing on
 * this page can post, and the server never hands a token to a browser — the
 * only shape a connection has out here is the one without it.
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function draw(state) {
    var list = $("links");
    list.textContent = "";
    var linked = 0;

    (state.platforms || []).forEach(function (p) {
      if (p.connected) { linked += 1; }
      var item = document.createElement("li");
      item.className = "link" + (p.connected ? " is-on" : "");

      var head = document.createElement("div");
      head.className = "link-head";
      var name = document.createElement("span");
      name.className = "link-name";
      name.textContent = p.name;
      var who = document.createElement("span");
      who.className = "link-who";
      who.textContent = p.connected ? p.handle : "not linked";
      head.appendChild(name);
      head.appendChild(who);
      item.appendChild(head);

      var note = document.createElement("span");
      note.className = "link-note";
      note.textContent = p.handoff;
      item.appendChild(note);

      if (!p.can_post) {
        var why = document.createElement("span");
        why.className = "link-why";
        why.textContent = p.why_not;
        item.appendChild(why);
      }

      var row = document.createElement("div");
      row.className = "link-row";
      if (p.connected) {
        var off = document.createElement("button");
        off.type = "button";
        off.className = "link-off";
        off.textContent = "Unlink";
        off.addEventListener("click", function () { unlink(p.platform); });
        row.appendChild(off);
      } else {
        var field = document.createElement("input");
        field.type = "text";
        field.placeholder = "@your handle";
        field.autocapitalize = "none";
        field.autocorrect = "off";
        field.spellcheck = false;
        field.setAttribute("aria-label", p.name + " handle");
        var go = document.createElement("button");
        go.type = "button";
        go.className = "link-go";
        go.textContent = "Link";
        go.addEventListener("click", function () { link(p.platform, field.value); });
        field.addEventListener("keydown", function (event) {
          if (event.key === "Enter") { link(p.platform, field.value); }
        });
        row.appendChild(field);
        row.appendChild(go);
      }
      item.appendChild(row);
      list.appendChild(item);
    });

    $("connect-state").textContent = linked
      ? linked + (linked === 1 ? " account linked" : " accounts linked")
      : "nothing linked yet";
  }

  function post(where, body) {
    return fetch(where, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); });
  }

  function link(platform, handle) {
    if (!(handle || "").trim()) { return; }
    post("/api/connections/link", { platform: platform, handle: handle.trim() })
      .then(draw)
      .catch(function () { $("connect-state").textContent = "could not save that"; });
  }

  function unlink(platform) {
    post("/api/connections/unlink", { platform: platform })
      .then(draw)
      .catch(function () { $("connect-state").textContent = "could not save that"; });
  }

  fetch("/api/connections", { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(draw)
    .catch(function () { $("connect-state").textContent = "could not be reached"; });
})();
