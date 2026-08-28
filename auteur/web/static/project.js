/* One project: the map, and the album.
 *
 * The map is a real canvas — pan, zoom, drag, join — and it is built out of
 * ordinary elements rather than a <canvas>, deliberately. A 2D canvas would be
 * fewer lines and would throw away every accessible thing the browser already
 * does: a node here is a <button> with text in it, so it is reachable with Tab,
 * readable by a screen reader, findable with the browser's own search, and
 * legible at whatever text size somebody has set. A drawn rectangle is none of
 * those.
 *
 * Coordinates are *world* units. The world div carries one transform and every
 * node sits at its own world position inside it, so panning and zooming are one
 * property change rather than a re-layout of forty elements — and what gets
 * saved is where a thing is on the map, not where it happened to be on the
 * screen of the phone that put it there.
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function escaped(text) {
    var box = document.createElement("span");
    box.textContent = text == null ? "" : String(text);
    return box.innerHTML;
  }

  /* Which project. From the address, so a project is a link. */
  var id = (function () {
    var match = /\/project\/([^/?#]+)/.exec(location.pathname);
    try { return match ? decodeURIComponent(match[1]) : ""; } catch (e) { return ""; }
  })();

  var project = null;      // the whole answer, as the server last described it
  var kinds = {};          // kind -> what it is for
  var nodes = {};          // id -> { data, element }
  var linking = null;      // the node id waiting to be joined to another

  /* One glyph per kind. Text rather than icons: they sit inside a node that
     already has words in it, and an icon set for seven kinds is seven things
     to learn where a symbol and a label are none. */
  var MARKS = {
    idea: "✎", shot: "◉", look: "◐", reel: "▚", film: "▶", place: "⌖", person: "☺"
  };

  // -- the world ---------------------------------------------------------

  var view = { x: 0, y: 0, zoom: 1 };
  /* 0.5, not 0.35. A node is 44px tall at life size; at a third of that it is
     15px, which is a thing you can see and cannot reliably tap. Zooming out
     makes everything smaller by definition — the floor is where that stops
     being a view and starts being a picture of one. */
  var MIN_ZOOM = 0.5;
  var MAX_ZOOM = 2.5;

  function paint() {
    $("world").style.transform =
      "translate(" + view.x + "px," + view.y + "px) scale(" + view.zoom + ")";
  }

  function toWorld(clientX, clientY) {
    var box = $("map").getBoundingClientRect();
    return {
      x: (clientX - box.left - view.x) / view.zoom,
      y: (clientY - box.top - view.y) / view.zoom
    };
  }

  function zoomAt(factor, clientX, clientY) {
    var was = view.zoom;
    var now = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, was * factor));
    if (now === was) { return; }
    var box = $("map").getBoundingClientRect();
    var cx = (clientX === undefined ? box.width / 2 : clientX - box.left);
    var cy = (clientY === undefined ? box.height / 2 : clientY - box.top);
    /* Keep whatever is under the fingers under the fingers. Without this the
       map slides away from the thing somebody is zooming into, which reads as
       the map fighting them. */
    view.x = cx - (cx - view.x) * (now / was);
    view.y = cy - (cy - view.y) * (now / was);
    view.zoom = now;
    paint();
  }

  function fit() {
    var all = Object.keys(nodes).map(function (key) { return nodes[key].data; });
    var box = $("map").getBoundingClientRect();
    if (!all.length) {
      view = { x: box.width / 2 - 80, y: box.height / 2 - 40, zoom: 1 };
      paint();
      return;
    }
    var left = Math.min.apply(null, all.map(function (n) { return n.x; }));
    var top = Math.min.apply(null, all.map(function (n) { return n.y; }));
    var right = Math.max.apply(null, all.map(function (n) { return n.x + 170; }));
    var bottom = Math.max.apply(null, all.map(function (n) { return n.y + 110; }));
    var pad = 28;
    var zoom = Math.min(
      (box.width - pad * 2) / Math.max(1, right - left),
      (box.height - pad * 2) / Math.max(1, bottom - top)
    );
    /* Never magnify. "Fit" on two small notes would otherwise blow them up to
       fill the screen, which is not fitting anything — it is zooming in on a
       nearly empty map and clipping both boxes at the edges. Shrink to fit,
       stop at life size. */
    view.zoom = Math.min(1, Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom)));
    view.x = pad + (box.width - pad * 2 - (right - left) * view.zoom) / 2 - left * view.zoom;
    view.y = pad + (box.height - pad * 2 - (bottom - top) * view.zoom) / 2 - top * view.zoom;
    paint();
  }

  // -- drawing -----------------------------------------------------------

  function drawNode(data) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "node node-" + data.kind + (data.done ? " is-done" : "");
    button.dataset.node = data.id;
    button.style.left = data.x + "px";
    button.style.top = data.y + "px";
    button.innerHTML =
      '<span class="node-mark" aria-hidden="true">' + (MARKS[data.kind] || "•") + "</span>" +
      '<span class="node-text">' + escaped(data.text || kinds[data.kind] || data.kind) + "</span>" +
      (data.done ? '<span class="node-done">on the board</span>' : "");
    button.setAttribute(
      "aria-label",
      (kinds[data.kind] || data.kind) + ": " + (data.text || "empty")
    );
    return button;
  }

  function drawMap() {
    var world = $("world");
    Array.prototype.forEach.call(world.querySelectorAll(".node"), function (n) { n.remove(); });
    nodes = {};
    (project.map.nodes || []).forEach(function (data) {
      var element = drawNode(data);
      world.appendChild(element);
      nodes[data.id] = { data: data, element: element };
    });
    drawLinks();
    $("map-blank").hidden = (project.map.nodes || []).length > 0;
  }

  /* The lines. Drawn from the middle of one node to the middle of another,
     measured off the elements rather than assumed — a node's height depends on
     how much text is in it and on the reader's text size, and a line drawn to
     an assumed height points at the air beside the box. */
  function drawLinks() {
    var svg = $("links");
    svg.innerHTML = "";
    (project.map.links || []).forEach(function (link) {
      var a = nodes[link.a];
      var b = nodes[link.b];
      if (!a || !b) { return; }
      var line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", a.data.x + a.element.offsetWidth / 2);
      line.setAttribute("y1", a.data.y + a.element.offsetHeight / 2);
      line.setAttribute("x2", b.data.x + b.element.offsetWidth / 2);
      line.setAttribute("y2", b.data.y + b.element.offsetHeight / 2);
      line.setAttribute("class", "map-link");
      line.dataset.link = link.id;
      svg.appendChild(line);
    });
  }

  // -- moving things -----------------------------------------------------

  var drag = null;        // { id, pointer, fromX, fromY, grabX, grabY, moved }
  var pan = null;         // { pointer, fromX, fromY, atX, atY }
  var pinch = null;       // { a, b, gap }
  var pending = {};       // node id -> [x, y], saved on release
  var pointers = {};

  function saveMoves() {
    var moves = pending;
    pending = {};
    if (!Object.keys(moves).length) { return; }
    /* Sent once, on release. A drag emits a position every frame; posting each
       one would be sixty writes a second into a JSON file on somebody's own
       laptop. */
    fetch("/api/projects/" + encodeURIComponent(id) + "/node", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ moves: moves })
    }).catch(function () {});
  }

  $("map").addEventListener("pointerdown", function (event) {
    pointers[event.pointerId] = { x: event.clientX, y: event.clientY };
    var keys = Object.keys(pointers);

    if (keys.length === 2) {
      drag = null;
      pan = null;
      var one = pointers[keys[0]];
      var two = pointers[keys[1]];
      pinch = { gap: Math.hypot(one.x - two.x, one.y - two.y) };
      return;
    }

    var onNode = event.target.closest(".node");
    if (onNode) {
      var held = nodes[onNode.dataset.node];
      var at = toWorld(event.clientX, event.clientY);
      drag = {
        id: onNode.dataset.node,
        pointer: event.pointerId,
        grabX: at.x - held.data.x,
        grabY: at.y - held.data.y,
        moved: false
      };
      onNode.setPointerCapture(event.pointerId);
      return;
    }
    pan = { pointer: event.pointerId, fromX: event.clientX, fromY: event.clientY,
            atX: view.x, atY: view.y };
  });

  $("map").addEventListener("pointermove", function (event) {
    if (pointers[event.pointerId]) {
      pointers[event.pointerId] = { x: event.clientX, y: event.clientY };
    }

    if (pinch) {
      var keys = Object.keys(pointers);
      if (keys.length < 2) { return; }
      var one = pointers[keys[0]];
      var two = pointers[keys[1]];
      var gap = Math.hypot(one.x - two.x, one.y - two.y);
      if (pinch.gap > 0) {
        zoomAt(gap / pinch.gap, (one.x + two.x) / 2, (one.y + two.y) / 2);
      }
      pinch.gap = gap;
      return;
    }

    if (drag && drag.pointer === event.pointerId) {
      var held = nodes[drag.id];
      if (!held) { return; }
      var at = toWorld(event.clientX, event.clientY);
      held.data.x = at.x - drag.grabX;
      held.data.y = at.y - drag.grabY;
      held.element.style.left = held.data.x + "px";
      held.element.style.top = held.data.y + "px";
      drag.moved = true;
      pending[drag.id] = [held.data.x, held.data.y];
      drawLinks();
      return;
    }

    if (pan && pan.pointer === event.pointerId) {
      view.x = pan.atX + (event.clientX - pan.fromX);
      view.y = pan.atY + (event.clientY - pan.fromY);
      paint();
    }
  });

  function release(event) {
    delete pointers[event.pointerId];
    if (Object.keys(pointers).length < 2) { pinch = null; }
    if (drag && drag.pointer === event.pointerId) {
      /* A tap is a drag that did not move. Deciding here rather than with a
         click handler, because a click after a drag is a click nobody meant. */
      if (!drag.moved) { tapped(drag.id); } else { saveMoves(); }
      drag = null;
    }
    if (pan && pan.pointer === event.pointerId) { pan = null; }
  }

  $("map").addEventListener("pointerup", release);
  $("map").addEventListener("pointercancel", release);

  /* The wheel, and the trackpad pinch that arrives as a wheel with ctrlKey. */
  $("map").addEventListener("wheel", function (event) {
    event.preventDefault();
    zoomAt(event.deltaY < 0 ? 1.12 : 1 / 1.12, event.clientX, event.clientY);
  }, { passive: false });

  $("zoom-in").addEventListener("click", function () { zoomAt(1.25); });
  $("zoom-out").addEventListener("click", function () { zoomAt(1 / 1.25); });
  $("zoom-fit").addEventListener("click", fit);

  /* Keyboard: Tab reaches every node because every node is a button, and the
     arrows nudge the focused one. Without this the map is a thing only a
     pointer can rearrange. */
  $("world").addEventListener("keydown", function (event) {
    var onNode = event.target.closest(".node");
    if (!onNode) { return; }
    var step = event.shiftKey ? 60 : 12;
    var by = { ArrowLeft: [-step, 0], ArrowRight: [step, 0],
               ArrowUp: [0, -step], ArrowDown: [0, step] }[event.key];
    if (!by) { return; }
    event.preventDefault();
    var held = nodes[onNode.dataset.node];
    held.data.x += by[0];
    held.data.y += by[1];
    held.element.style.left = held.data.x + "px";
    held.element.style.top = held.data.y + "px";
    pending[held.data.id] = [held.data.x, held.data.y];
    drawLinks();
    clearTimeout(held.timer);
    held.timer = setTimeout(saveMoves, 600);
  });

  /* A node reached with Tab has to be brought on screen, or the focus ring is
     somewhere nobody can see. */
  $("world").addEventListener("focusin", function (event) {
    var onNode = event.target.closest(".node");
    if (!onNode) { return; }
    var box = $("map").getBoundingClientRect();
    var at = onNode.getBoundingClientRect();
    if (at.left < box.left || at.right > box.right || at.top < box.top || at.bottom > box.bottom) {
      var held = nodes[onNode.dataset.node];
      view.x = box.width / 2 - (held.data.x + 85) * view.zoom;
      view.y = box.height / 2 - (held.data.y + 40) * view.zoom;
      paint();
    }
  });

  // -- one node ----------------------------------------------------------

  function tapped(nodeId) {
    if (linking && linking !== nodeId) {
      join(linking, nodeId);
      return;
    }
    openNode(nodeId);
  }

  var open = null;

  function openNode(nodeId) {
    var held = nodes[nodeId];
    if (!held) { return; }
    open = nodeId;
    stopLinking();
    $("node-title").textContent = kinds[held.data.kind] ? "This " + held.data.kind : "This note";
    $("node-kind-note").textContent = kinds[held.data.kind] || "";
    $("node-text").value = held.data.text || "";
    $("node-error").hidden = true;
    $("node-plan").hidden = !(held.data.kind === "shot" && !held.data.done);
    $("node-kinds").innerHTML = Object.keys(kinds).map(function (kind) {
      return '<button type="button" class="choice' +
        (kind === held.data.kind ? " is-on" : "") + '" role="radio" aria-checked="' +
        (kind === held.data.kind ? "true" : "false") + '" data-value="' + kind + '">' +
        (MARKS[kind] || "•") + " " + escaped(kind) + "</button>";
    }).join("");
    sheet("node", true);
  }

  $("node-kinds").addEventListener("click", function (event) {
    var choice = event.target.closest(".choice");
    if (!choice) { return; }
    Array.prototype.forEach.call(this.querySelectorAll(".choice"), function (other) {
      var on = other === choice;
      other.classList.toggle("is-on", on);
      other.setAttribute("aria-checked", on ? "true" : "false");
    });
  });

  $("node-save").addEventListener("click", function () {
    var picked = $("node-kinds").querySelector(".choice.is-on");
    post("/api/projects/" + encodeURIComponent(id) + "/node/" + encodeURIComponent(open), {
      text: $("node-text").value,
      kind: picked ? picked.dataset.value : undefined
    }).then(function (got) {
      if (!got.ok) {
        $("node-error").textContent = got.d.error || "That did not save.";
        $("node-error").hidden = false;
        return;
      }
      var held = nodes[open];
      held.data.text = got.d.node.text;
      held.data.kind = got.d.node.kind;
      var fresh = drawNode(held.data);
      held.element.replaceWith(fresh);
      held.element = fresh;
      drawLinks();
      sheet("node", false);
    });
  });

  $("node-drop").addEventListener("click", function () {
    post("/api/projects/" + encodeURIComponent(id) + "/node/" +
         encodeURIComponent(open) + "/drop", {}).then(function (got) {
      if (!got.ok) { return; }
      project.map.nodes = project.map.nodes.filter(function (n) { return n.id !== open; });
      project.map.links = project.map.links.filter(function (l) {
        return l.a !== open && l.b !== open;
      });
      drawMap();
      sheet("node", false);
    });
  });

  $("node-link").addEventListener("click", function () {
    linking = open;
    sheet("node", false);
    $("map").classList.add("is-linking");
    $("map-hint").textContent = "Now tap the one it leads to. Tap the map to stop.";
  });

  function stopLinking() {
    linking = null;
    $("map").classList.remove("is-linking");
    $("map-hint").textContent =
      "Drag to move things. Tap one to open it, or to join it to another.";
  }

  $("map").addEventListener("click", function (event) {
    if (linking && !event.target.closest(".node")) { stopLinking(); }
  });

  function join(a, b) {
    post("/api/projects/" + encodeURIComponent(id) + "/link", { a: a, b: b })
      .then(function (got) {
        stopLinking();
        if (!got.ok) { return; }
        var already = project.map.links.some(function (l) { return l.id === got.d.link.id; });
        if (!already) { project.map.links.push(got.d.link); }
        drawLinks();
      });
  }

  /* A shot on the map, turned into a real capture on the plan board. This is
     the reason the nodes are typed: a note that says "ferry leaving, from the
     rail" is a note, and a *shot* that says it is something the manager can
     put on a board with a date. */
  $("node-plan").addEventListener("click", function () {
    var held = nodes[open];
    if (!held) { return; }
    post("/api/plans", {
      prompt: held.data.text || "from the map",
      project: id,
      title: (project.name || "Project") + " — " + (held.data.text || "a shot")
    }).then(function (got) {
      if (!got.ok) {
        $("node-error").textContent = got.d.error || "The board would not take it.";
        $("node-error").hidden = false;
        return;
      }
      post("/api/projects/" + encodeURIComponent(id) + "/node/" +
           encodeURIComponent(open), { done: true }).then(function () {
        held.data.done = true;
        var fresh = drawNode(held.data);
        held.element.replaceWith(fresh);
        held.element = fresh;
        sheet("node", false);
        if (window.auteurSafety) {
          window.auteurSafety.said("On the plan board. The album has it too.");
        }
        load();
      });
    });
  });

  // -- adding ------------------------------------------------------------

  function drawAdders() {
    $("adders").innerHTML = Object.keys(kinds).map(function (kind) {
      return '<button type="button" class="adder" data-add="' + kind + '">' +
        '<span aria-hidden="true">' + (MARKS[kind] || "•") + "</span>" +
        escaped(kind) + "</button>";
    }).join("");
  }

  $("adders").addEventListener("click", function (event) {
    var button = event.target.closest("[data-add]");
    if (!button) { return; }
    /* Dropped in the middle of what is on screen, not at the world origin —
       otherwise the fifth thing you add appears somewhere you are not
       looking. */
    var box = $("map").getBoundingClientRect();
    var at = toWorld(box.left + box.width / 2, box.top + box.height / 3);
    post("/api/projects/" + encodeURIComponent(id) + "/node", {
      kind: button.dataset.add,
      x: Math.round(at.x - 85),
      y: Math.round(at.y - 30)
    }).then(function (got) {
      if (!got.ok) { return; }
      project.map.nodes.push(got.d.node);
      var element = drawNode(got.d.node);
      $("world").appendChild(element);
      nodes[got.d.node.id] = { data: got.d.node, element: element };
      $("map-blank").hidden = true;
      openNode(got.d.node.id);
    });
  });

  // -- the album ---------------------------------------------------------

  function drawAlbum() {
    var films = project.album.films || [];
    $("films").innerHTML = films.map(function (film) {
      return '<button type="button" class="grid-cell" data-film="' + escaped(film.id) +
        '" aria-label="' + escaped(film.prompt || "a film") + '">' +
        '<img src="' + escaped(film.poster) + '" alt="" loading="lazy"></button>';
    }).join("");
    $("films-blank").hidden = films.length > 0;

    var plans = project.album.plans || [];
    $("plans").innerHTML = plans.map(function (plan) {
      return '<li class="inset-row plan-row"><span class="inset-label">' +
        escaped(plan.title || plan.prompt || "a plan") + "</span>" +
        '<span class="inset-value">' + escaped(day(plan.when || plan.due)) + "</span></li>";
    }).join("");
    $("plans-blank").hidden = plans.length > 0;
  }

  /* Films that are not in any project yet, so one made before the project
     existed can still be filed under it. */
  function drawLoose() {
    fetch("/api/feed?scope=mine", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) { return; }
        var loose = (data.films || []).filter(function (f) { return !f.project; });
        $("loose").innerHTML = loose.map(function (film) {
          return '<li><button type="button" class="person" data-gather="' + escaped(film.id) +
            '"><img class="loose-poster" src="' + escaped(film.poster) + '" alt="">' +
            '<span><span class="person-name">' + escaped(film.heard || film.prompt) +
            "</span></span></button></li>";
        }).join("");
        $("loose-blank").hidden = loose.length > 0;
      })
      .catch(function () {});
  }

  $("loose").addEventListener("click", function (event) {
    var button = event.target.closest("[data-gather]");
    if (!button) { return; }
    button.disabled = true;
    post("/api/projects/" + encodeURIComponent(id) + "/gather", { film: button.dataset.gather })
      .then(function (got) {
        if (!got.ok) { button.disabled = false; return; }
        load();
        if (window.auteurSafety) { window.auteurSafety.said("Added to the album."); }
      });
  });

  $("films").addEventListener("click", function (event) {
    var cell = event.target.closest(".grid-cell");
    if (cell) { location.href = "/feed?film=" + encodeURIComponent(cell.dataset.film); }
  });

  /* A plan's date, as a date. The board stores an ISO-8601 instant, which is
     the right thing to store and the wrong thing to show — the first version
     of this printed "2026-08-22T12:38:45.292250+00:00" into a list row and
     shoved the title into a one-word-per-line column beside it. */
  function day(when) {
    if (!when) { return ""; }
    var at = new Date(when);
    if (isNaN(at.getTime())) { return String(when).slice(0, 10); }
    return at.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  }

  // -- the two faces -----------------------------------------------------

  function face(which) {
    ["map", "album"].forEach(function (name) {
      var on = name === which;
      $("face-" + name).classList.toggle("is-on", on);
      $("face-" + name).setAttribute("aria-selected", on ? "true" : "false");
      $(name + "-face").hidden = !on;
    });
    if (which === "map") { fit(); } else { drawLoose(); }
  }

  $("face-map").addEventListener("click", function () { face("map"); });
  $("face-album").addEventListener("click", function () { face("album"); });

  // -- the project itself ------------------------------------------------

  $("project-more").addEventListener("click", function () {
    $("edit-name").value = project.name || "";
    $("edit-place").value = project.place || "";
    $("edit-starts").value = project.starts || "";
    $("edit-ends").value = project.ends || "";
    $("edit-note").value = project.note || "";
    $("edit-error").hidden = true;
    sheet("edit", true);
  });

  $("edit-save").addEventListener("click", function () {
    post("/api/projects/" + encodeURIComponent(id), {
      name: $("edit-name").value,
      place: $("edit-place").value,
      starts: $("edit-starts").value,
      ends: $("edit-ends").value,
      note: $("edit-note").value
    }).then(function (got) {
      if (!got.ok) {
        $("edit-error").textContent = got.d.error || "That did not save.";
        $("edit-error").hidden = false;
        return;
      }
      sheet("edit", false);
      load();
    });
  });

  $("edit-drop").addEventListener("click", function () {
    post("/api/projects/" + encodeURIComponent(id) + "/drop", {}).then(function (got) {
      if (got.ok) { location.replace("/projects"); }
    });
  });

  // -- plumbing ----------------------------------------------------------

  function post(where, body) {
    return fetch(where, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .catch(function () { return { ok: false, d: {} }; });
  }

  function sheet(name, open_) {
    $(name + "-sheet").hidden = !open_;
    document.body.classList.toggle("sheet-open", !!open_);
  }

  document.addEventListener("click", function (event) {
    var closer = event.target.closest("[data-close]");
    if (closer) { sheet(closer.dataset.close, false); }
  });

  function load() {
    return fetch("/api/projects/" + encodeURIComponent(id), {
      credentials: "same-origin",
      cache: "no-store"
    })
      .then(function (r) {
        if (r.status === 401) { location.href = "/login"; return null; }
        if (r.status === 404) { location.replace("/projects"); return null; }
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data) { return; }
        project = data.project;
        kinds = project.kinds || {};
        document.title = "Auteur — " + project.name;
        $("bar-name").textContent = project.name;
        $("big-name").textContent = project.name;
        var when = [project.dated, project.place].filter(Boolean).join(" · ");
        $("project-when").textContent = when;
        $("project-when").hidden = !when;
        $("project-note").textContent = project.note || "";
        $("project-note").hidden = !project.note;
        drawAdders();
        drawMap();
        drawAlbum();
        if (!$("map-face").hidden) { fit(); }
      })
      .catch(function () {});
  }

  load();
})();
