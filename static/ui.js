/* Bankai V13 — presentation helper. Adds initial-avatars next to names.
   Read-only with respect to data: it never touches events, ids or API calls. */
(function () {
  "use strict";
  var HUES = [212, 262, 158, 24, 340, 190, 40, 290];
  function hue(name) { var h = 0; for (var i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0; return HUES[h % HUES.length]; }
  function avatar(name) {
    var s = document.createElement("span");
    s.className = "av"; s.setAttribute("aria-hidden", "true");
    s.style.setProperty("--h", hue(name));
    s.textContent = (name.trim()[0] || "؟");
    return s;
  }
  function wrap(host, name) {
    if (!name || host.querySelector(":scope > .wcell")) return;
    var cell = document.createElement("div"), txt = document.createElement("div");
    cell.className = "wcell"; txt.className = "wtxt";
    while (host.firstChild) txt.appendChild(host.firstChild);
    cell.appendChild(avatar(name)); cell.appendChild(txt); host.appendChild(cell);
  }
  function text(el) { return el ? (el.firstChild && el.firstChild.nodeType === 3 ? el.firstChild.nodeValue : el.textContent) : ""; }
  function decorate() {
    document.querySelectorAll(".a-who").forEach(function (h) { wrap(h, text(h.querySelector(".a-name"))); });
    document.querySelectorAll("td.who").forEach(function (h) { wrap(h, text(h.querySelector(".link, b"))); });
    document.querySelectorAll(".mini-tbl tr[data-hist] td:first-child").forEach(function (h) { var b = h.querySelector(":scope > b"); if (b) wrap(h, text(b)); });
  }
  var queued = false;
  function schedule() { if (queued) return; queued = true; requestAnimationFrame(function () { queued = false; decorate(); }); }
  function start() {
    decorate();
    new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
