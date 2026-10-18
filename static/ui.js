/* Bankai V20 — presentation helper. Adds initial-avatars next to names.
   Read-only with respect to data: it never touches events, ids or API calls.

   قبل كده كان فيه MutationObserver على document.body كله (childList + subtree):
   أي تغيير في أي جزء من الصفحة — typing في البحث، تحديث الـ feed كل 30 ثانية،
   فتح/قفل صفوف الحضور — كان بيعمل مسح كامل للـ DOM من أول وجديد. ده اتشال خلاص.

   التوليد الأساسي للأفاتار بقى جوه تمبليتس app.js نفسها (توليد مباشر وقت الرسم).
   الملف ده بقى بيتنادى بشكل مباشر ومحدود على الحاوية اللي اتغيّرت بس:
     BankaiUI.decorate(elOrRoot)  →  أيقونات a11y + أفاتار احتياطي لأي ماركب
                                      مابيتمرّش من تمبليتس app.js.
   مفيش أي مراقب DOM هنا. */
(function () {
  "use strict";
  var API = (window.BankaiUI = window.BankaiUI || {});

  /* مصدر واحد للحساب: Hue جاي من app.js (API.avHue) لو موجود. */
  function hue(name) {
    if (API.avHue) return API.avHue(name);
    var HUES = [212, 262, 158, 24, 340, 190, 40, 290], h = 0, s = String(name || "");
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return HUES[h % HUES.length];
  }
  function avatar(name) {
    var s = document.createElement("span");
    s.className = "av"; s.setAttribute("aria-hidden", "true");
    s.style.setProperty("--h", hue(name));
    s.textContent = (String(name || "").trim()[0] || "؟");
    return s;
  }
  function wrap(host, name) {
    if (!host || !name || host.querySelector(":scope > .wcell")) return;
    var cell = document.createElement("div"), txt = document.createElement("div");
    cell.className = "wcell"; txt.className = "wtxt";
    while (host.firstChild) txt.appendChild(host.firstChild);
    cell.appendChild(avatar(name)); cell.appendChild(txt); host.appendChild(cell);
  }
  function text(el) { return el ? (el.firstChild && el.firstChild.nodeType === 3 ? el.firstChild.nodeValue : el.textContent) : ""; }
  /* الأيقونات زخرفية بحتة (جنبها نص، أو الزرار عليه aria-label) — نخفيها عن قارئ الشاشة */
  function icons(root) {
    (root || document).querySelectorAll("svg.i").forEach(function (s) {
      if (s.getAttribute("aria-hidden") !== "true") {
        s.setAttribute("aria-hidden", "true");
        s.setAttribute("focusable", "false");
      }
    });
  }
  function decorate(root) {
    root = root || document;
    icons(root);
    root.querySelectorAll(".a-who").forEach(function (h) { wrap(h, text(h.querySelector(".a-name"))); });
    root.querySelectorAll("td.who").forEach(function (h) { wrap(h, text(h.querySelector(".link, b"))); });
    root.querySelectorAll(".mini-tbl tr[data-hist] td:first-child").forEach(function (h) { var b = h.querySelector(":scope > b"); if (b) wrap(h, text(b)); });
  }
  API.decorate = decorate;
  API.icons = icons;
  API.avatar = avatar;

  function start() { decorate(document); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
