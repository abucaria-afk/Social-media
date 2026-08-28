/* The list of projects.
 *
 * Each one is drawn as an album: a cover pulled from the newest film in it,
 * the dates, and how much is in it. A project with nothing in it yet still
 * gets a card — the empty one is the state somebody is in for the whole week
 * before the trip, which is exactly when the map is worth opening.
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function escaped(text) {
    var box = document.createElement("span");
    box.textContent = text == null ? "" : String(text);
    return box.innerHTML;
  }

  function count(n, one, many) {
    return n + " " + (n === 1 ? one : many);
  }

  function draw(rows) {
    var list = $("albums");
    $("blank").hidden = rows.length > 0;
    list.innerHTML = rows.map(function (row) {
      var facts = [];
      if (row.films) { facts.push(count(row.films, "film", "films")); }
      if (row.plans) { facts.push(count(row.plans, "plan", "plans")); }
      if (row.nodes) { facts.push(count(row.nodes, "note", "notes")); }
      return '<li><a class="album" href="/project/' + encodeURIComponent(row.id) + '">' +
        '<span class="album-cover"' +
          (row.poster ? ' style="background-image:url(' + encodeURI(row.poster) + ')"' : "") +
          ">" + (row.poster ? "" : '<span class="album-mark" aria-hidden="true">◱</span>') +
        "</span>" +
        '<span class="album-lines">' +
          '<span class="album-name">' + escaped(row.name) + "</span>" +
          (row.dated || row.place
            ? '<span class="album-when">' +
              escaped([row.dated, row.place].filter(Boolean).join(" · ")) + "</span>"
            : "") +
          '<span class="album-facts">' +
            escaped(facts.length ? facts.join(" · ") : "nothing in it yet") + "</span>" +
        "</span>" +
        '<span class="chevron" aria-hidden="true">›</span>' +
      "</a></li>";
    }).join("");
  }

  function load() {
    return fetch("/api/projects", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (r.status === 401) { location.href = "/login"; return null; }
        return r.ok ? r.json() : null;
      })
      .then(function (data) { if (data) { draw(data.projects || []); } })
      .catch(function () {});
  }

  /* -- starting one ------------------------------------------------------ */

  function open() {
    ["new-name", "new-place", "new-starts", "new-ends", "new-note"].forEach(function (id) {
      $(id).value = "";
    });
    $("new-error").hidden = true;
    $("new-sheet").hidden = false;
    document.body.classList.add("sheet-open");
    $("new-name").focus();
  }

  function close() {
    $("new-sheet").hidden = true;
    document.body.classList.remove("sheet-open");
  }

  document.addEventListener("click", function (event) {
    if (event.target.closest('[data-close="new"]')) { close(); }
  });

  $("new-project").addEventListener("click", open);
  $("blank-new").addEventListener("click", open);

  $("new-go").addEventListener("click", function () {
    var problem = $("new-error");
    problem.hidden = true;
    fetch("/api/projects", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("new-name").value,
        place: $("new-place").value,
        starts: $("new-starts").value,
        ends: $("new-ends").value,
        note: $("new-note").value
      })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (got) {
        if (!got.ok) {
          problem.textContent = got.d.error || "That did not start.";
          problem.hidden = false;
          return;
        }
        /* Straight into it. Somebody who just named a trip has the next
           thought already — making them find it in a list first is a step
           that exists only because the code was easier to write that way. */
        location.href = "/project/" + encodeURIComponent(got.d.project.id);
      })
      .catch(function () {
        problem.textContent = "Could not reach the app.";
        problem.hidden = false;
      });
  });

  load();
})();
