/* Injected before the page runs, so the page does not have to know it is in an app.
 *
 * The web build already calls `navigator.share` to get a film into Photos and
 * already offers a download link when it cannot. In a web view the first does
 * not exist and the second does not reach the camera roll, so the page's own
 * save button quietly does nothing — which is the worst possible failure for
 * the one control that delivers the product.
 *
 * So rather than a separate native path with its own buttons, this fills in
 * the two APIs the page already reaches for. The page stays one codebase and
 * behaves correctly in a browser, on a desktop, and here.
 */
(function (global) {
  "use strict";

  function send(job, extra) {
    var message = { job: job };
    for (var key in extra) { if (extra.hasOwnProperty(key)) { message[key] = extra[key]; } }
    global.webkit.messageHandlers.auteur.postMessage(message);
  }

  function base64(blob) {
    return new Promise(function (done, fail) {
      var reader = new FileReader();
      reader.onload = function () { done(reader.result); };
      reader.onerror = fail;
      reader.readAsDataURL(blob);
    });
  }

  global.auteurNative = {
    /* Straight to the camera roll, which is what "save" means on a phone. */
    save: function (blob, name) {
      return base64(blob).then(function (data) {
        send("save", { data: data, name: name || "film.mp4" });
        return true;
      });
    },
    share: function (blob, name) {
      return base64(blob).then(function (data) {
        send("share", { data: data, name: name || "film.mp4" });
        return true;
      });
    },
    calendar: function (plan) {
      send("calendar", plan || {});
      return true;
    },
    isApp: true
  };

  /* Web Share, filled in. The page tests `navigator.canShare({files})` before
   * using it, so both have to answer or the test fails and the page falls back
   * to a download that goes nowhere. */
  if (!global.navigator.canShare) {
    global.navigator.canShare = function (what) {
      return !!(what && what.files && what.files.length);
    };
  }
  if (!global.navigator.share) {
    global.navigator.share = function (what) {
      if (!what || !what.files || !what.files.length) {
        return Promise.reject(new TypeError("nothing to share"));
      }
      var file = what.files[0];
      return global.auteurNative.share(file, file.name);
    };
  }

  /* A film is finished. Offer it to Photos directly rather than making
   * somebody go through the share sheet for the ordinary case. */
  global.addEventListener("auteur-film", function (event) {
    var blob = event.detail && event.detail.blob;
    if (blob) { global.auteurNative.save(blob, "auteur-film.mp4"); }
  });

  /* What this web view can actually do. The renderer needs
   * `canvas.captureStream` and `MediaRecorder`; if either is missing the page
   * should say so rather than starting a render that produces nothing. */
  global.addEventListener("DOMContentLoaded", function () {
    var canvas = document.createElement("canvas");
    var able = {
      captureStream: typeof canvas.captureStream === "function",
      mediaRecorder: typeof global.MediaRecorder === "function",
      mp4: typeof global.MediaRecorder === "function"
        && global.MediaRecorder.isTypeSupported("video/mp4;codecs=avc1")
    };
    global.auteurNative.able = able;
    send("capabilities", able);
    if (!able.captureStream || !able.mediaRecorder) {
      var warning = document.createElement("p");
      warning.setAttribute("role", "alert");
      warning.style.cssText =
        "margin:1rem;padding:0.85rem 1rem;border-radius:10px;" +
        "background:var(--surface,#232127);color:var(--rust,#ee8777);" +
        "font:600 0.9rem/1.45 -apple-system,sans-serif";
      warning.textContent =
        "This version of iOS cannot record from a canvas, so a film cannot be " +
        "made here. iOS 16 or later is needed.";
      document.body.insertBefore(warning, document.body.firstChild);
    }
  });
})(window);
