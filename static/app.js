"use strict";
/* Bankai — Roster & Attendance (vanilla JS, no build step) */

/* ============================== helpers ============================== */
const $ = id => document.getElementById(id);
const qs = (s, r = document) => r.querySelector(s);
const qsa = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sh = s => `<bdi dir="ltr">${esc(s)}</bdi>`;
const num = n => Number(n || 0).toLocaleString("en-US", { maximumFractionDigits: 2 });
const money = n => `${num(n)} ج.م`;
const pad = n => String(n).padStart(2, "0");
const isoOf = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const todayISO = () => isoOf(new Date());
const parseISO = s => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const addDays = (s, n) => { const d = parseISO(s); d.setDate(d.getDate() + n); return isoOf(d); };
const fmtDay = (s, opts) => parseISO(s).toLocaleDateString("ar-EG-u-nu-latn", opts);
const anyDrafts = () => S.draft.size + (S.sch ? S.sch.draft.size : 0);
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const on = (id, ev, fn) => $(id).addEventListener(ev, fn);

const WEEKDAYS = ["Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];
const WD_AR = { Saturday: "السبت", Sunday: "الأحد", Monday: "الاثنين", Tuesday: "الثلاثاء", Wednesday: "الأربعاء", Thursday: "الخميس", Friday: "الجمعة" };
const ST = { Present: ["حاضر", "ok"], Late: ["متأخر", "warn"], Absent: ["غائب", "bad"], Excused: ["بعذر", "info"] };
const STK = ["Present", "Late", "Absent", "Excused"];
const CLR = { present: "var(--ok)", late: "#e0a020", absent: "var(--bad)", excused: "#7f8fb0", none: "var(--none)" };

const S = {
  token: localStorage.getItem("bankai_token") || "",
  role: localStorage.getItem("bankai_role") || "",
  page: "", shown: false,
  settings: { fine_late: 0, fine_absent: 0 },
  employees: [],
  dash: { date: todayISO() },
  roster: null, rmap: new Map(), draft: new Map(), view: { base: [], list: [], groups: new Map() },
  att: { date: todayISO(), branch: "", shift: "", type: "", q: "", status: "", group: true, collapsed: new Set() },
  emp: { q: "", branch: "", shift: "", type: "", day: "", active: "1", sort: { key: "name", dir: 1 }, page: 1, size: 50, selected: new Set(), filtered: [] },
  rep: { sum: null, emps: [], sort: { key: "name", dir: 1 }, page: 1, size: 50, preset: "7" },
  hist: { id: null },
};

/* شريط تقدّم رقيق أعلى الصفحة. بيظهر بس لو الطلب خد أكتر من 220ms عشان مايرمشش
   مع كل تفاعل سريع، والطلبات الخلفية (تحديث الـ feed كل 30 ثانية) بتبعت silent. */
let inflight = 0, barTimer;
function trackStart() {
  inflight++;
  if (inflight === 1) barTimer = setTimeout(() => $("routeBar")?.classList.add("on"), 220);
}
function trackEnd() {
  inflight = Math.max(0, inflight - 1);
  if (!inflight) { clearTimeout(barTimer); $("routeBar")?.classList.remove("on"); }
}

async function api(path, opt = {}) {
  const silent = !!opt.silent;
  const opts = { ...opt };
  delete opts.silent;
  const headers = { ...(opts.headers || {}) };
  if (S.token) headers.Authorization = `Bearer ${S.token}`;
  let body = opts.body;
  if (body !== undefined && typeof body !== "string") { body = JSON.stringify(body); headers["Content-Type"] = "application/json"; }
  if (!silent) trackStart();
  try {
    let res;
    try { res = await fetch(path, { ...opts, headers, body }); }
    catch { throw new Error("تعذر الاتصال بالسيرفر. تأكد من الإنترنت وحاول مرة أخرى."); }
    if (res.status === 401 && path !== "/api/login") { logout(false); throw new Error("انتهت الجلسة. سجّل الدخول مرة أخرى."); }
    if (!res.ok) {
      let msg = "حدث خطأ غير متوقع";
      try { const e = await res.json(); if (typeof e.detail === "string") msg = e.detail; else if (Array.isArray(e.detail)) msg = "بيانات غير صالحة، راجع الحقول وحاول مرة أخرى."; } catch { /* ignore */ }
      throw new Error(msg);
    }
    if ((res.headers.get("content-type") || "").includes("application/json")) return res.json();
    return res;
  } finally { if (!silent) trackEnd(); }
}

async function download(path, filename) {
  const res = await api(path);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

let toastTimer;
function toast(msg, kind = "") {
  const el = $("toast");
  el.textContent = msg; el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), kind === "err" ? 5200 : 2800);
}

/* shared UI pieces */
function stackInner(c) {
  const t = c.scheduled || 0;
  if (!t) return "";
  const seg = (k, n, label) => n ? `<i class="s-${k}" style="width:${n * 100 / t}%" title="${label}: ${n}"></i>` : "";
  return seg("present", c.present, "حاضر") + seg("late", c.late, "متأخر") + seg("absent", c.absent, "غائب") + seg("excused", c.excused, "بعذر") + seg("none", c.unmarked, "لم يُسجل");
}
const stackBar = (c, cls = "") => `<div class="stack ${cls}">${stackInner(c)}</div>`;
const legend = c => `<div class="legend"><span><i class="dot ok"></i>حاضر <b class="num">${c.present}</b></span><span><i class="dot warn"></i>متأخر <b class="num">${c.late}</b></span><span><i class="dot bad"></i>غائب <b class="num">${c.absent}</b></span><span><i class="dot info"></i>بعذر <b class="num">${c.excused}</b></span>${c.unmarked !== undefined ? `<span><i class="dot none"></i>لم يُسجل <b class="num">${c.unmarked}</b></span>` : ""}</div>`;

function renderPager(el, total, st, redraw) {
  const pages = Math.max(1, Math.ceil(total / st.size));
  st.page = Math.min(Math.max(1, st.page), pages);
  const from = total ? (st.page - 1) * st.size + 1 : 0, to = Math.min(total, st.page * st.size);
  el.innerHTML = `<span>عرض <b class="num">${from}–${to}</b> من <b class="num">${total}</b></span>
    <div class="row"><select class="sel" aria-label="عدد الصفوف">${[25, 50, 100, 200].map(n => `<option value="${n}" ${n === st.size ? "selected" : ""}>${n} صف</option>`).join("")}</select>
    <button class="btn sm icon" data-pg="-1" aria-label="السابق" ${st.page <= 1 ? "disabled" : ""}><svg class="i"><use href="#i-right"/></svg></button>
    <span>صفحة <b class="num">${st.page}</b> من <b class="num">${pages}</b></span>
    <button class="btn sm icon" data-pg="1" aria-label="التالي" ${st.page >= pages ? "disabled" : ""}><svg class="i"><use href="#i-left"/></svg></button></div>`;
  qs("select", el).onchange = e => { st.size = +e.target.value; st.page = 1; redraw(); };
  qsa("[data-pg]", el).forEach(b => b.onclick = () => { st.page += +b.dataset.pg; redraw(); });
}

function sortRows(list, st, getter) {
  const { key, dir = 1 } = st.sort || {};
  return list.sort((a, b) => {
    const x = getter(a, key), y = getter(b, key);
    const r = (typeof x === "number" && typeof y === "number") ? x - y : String(x).localeCompare(String(y), "ar");
    return (r * dir) || String(a.name).localeCompare(String(b.name), "ar");
  });
}
function bindSort(selector, attr, st, redraw) {
  qsa(selector).forEach(th => th.addEventListener("click", () => {
    const k = th.dataset[attr];
    st.sort = { key: k, dir: st.sort.key === k ? -st.sort.dir : 1 };
    st.page = 1; redraw();
  }));
}
function markSort(selector, attr, st) {
  qsa(selector).forEach(th => { th.classList.remove("asc", "desc"); if (th.dataset[attr] === st.sort.key) th.classList.add(st.sort.dir > 0 ? "asc" : "desc"); });
}

/* ============================== auth & navigation ============================== */
const isAdmin = () => S.role !== "supervisor"; // الدور الافتراضي أدمن (بيتحدد من السيرفر بعد الدخول)
on("loginForm", "submit", async e => {
  e.preventDefault();
  $("loginError").textContent = "";
  try {
    const r = await api("/api/login", { method: "POST", body: { password: $("password").value } });
    S.token = r.token; localStorage.setItem("bankai_token", S.token);
    S.role = r.role || "admin"; localStorage.setItem("bankai_role", S.role);
    document.documentElement.dataset.auth = "1"; /* نفس القفل بتاع الـ head: شاشة الدخول مقفولة من هنا لقدام */
    $("password").value = "";
    await init();
  } catch (err) { $("loginError").textContent = err.message; }
});

function logout(show = true) {
  S.token = ""; S.role = "";
  localStorage.removeItem("bankai_token"); localStorage.removeItem("bankai_role");
  delete document.documentElement.dataset.auth; /* نشيل قفل إخفاء شاشة الدخول المبكّر بتاع الـ head */
  S.draft.clear(); if (S.sch) S.sch.draft.clear(); S.shown = false; S.page = "";
  $("appView").classList.add("hidden"); $("loginView").classList.remove("hidden");
  if (show) toast("تم تسجيل الخروج");
}
qsa(".js-logout").forEach(b => b.addEventListener("click", () => {
  if (anyDrafts() && !confirm("عندك تغييرات لم تُحفظ. هل تريد الخروج وتجاهلها؟")) return;
  logout();
}));

// ---------- theme (light / dark) ----------
function applyTheme(t, save) {
  document.documentElement.dataset.theme = t;
  const m = document.querySelector('meta[name="theme-color"]'); if (m) m.content = t === "dark" ? "#0f0b1c" : "#2a0f57";
  qsa(".js-theme").forEach(b => b.setAttribute("aria-checked", t === "dark" ? "true" : "false"));
  if (save) { try { localStorage.setItem("bankai-theme", t); } catch (e) {} }
}
applyTheme(document.documentElement.dataset.theme || "light", false);
qsa(".js-theme").forEach(b => b.addEventListener("click", () =>
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true)));

function showApp() {
  $("loginView").classList.add("hidden"); $("appView").classList.remove("hidden");
  $("todayPill").textContent = new Date().toLocaleDateString("ar-EG-u-nu-latn", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

/* إظهار/إخفاء عناصر الأدمن حسب الدور */
function applyRole() {
  const sup = !isAdmin();
  document.body.classList.toggle("supervisor", sup);
  const av = $("navAva");
  av.textContent = sup ? "م" : "أ";
  av.title = sup ? "مشرف — مشاهدة كل حاجة وتعديل الحضور اليومي فقط" : "أدمن — كل الصلاحيات";
  $("rolePill").classList.toggle("hidden", !sup);
  $("setFineLate").disabled = $("setFineAbsent").disabled = sup;
}

const PAGES = { dashboard: "نظرة عامة", attendance: "الحضور اليومي", schedule: "الجدول", employees: "الموظفون", reports: "التقارير", settings: "الإعدادات" };
qsa("[data-page]").forEach(b => b.addEventListener("click", () => { location.hash = b.dataset.page; }));
window.addEventListener("hashchange", route);

function route() {
  if (!S.token) return;
  const p = (location.hash || "#dashboard").slice(1);
  const target = PAGES[p] ? p : "dashboard";
  if (S.shown && target === S.page) return;
  if (S.shown && target !== S.page && ((S.page === "attendance" && S.draft.size) || (S.page === "schedule" && S.sch.draft.size))) {
    if (!confirm("عندك تغييرات لم تُحفظ. هل تريد تجاهلها؟")) { location.hash = S.page; return; }
    S.draft.clear(); S.sch.draft.clear(); updateSaveBar(); updateSchSaveBar();
  }
  showPage(target);
}

function showPage(p) {
  S.page = p; S.shown = true;
  qsa(".page").forEach(x => x.classList.add("hidden"));
  $("page-" + p).classList.remove("hidden");
  qsa("[data-page]").forEach(b => b.classList.toggle("active", b.dataset.page === p));
  $("pageTitle").textContent = PAGES[p];
  document.title = `${PAGES[p]} — Bankai`;
  window.scrollTo(0, 0);
  if (p === "dashboard") { loadDashboard(); restartFeedTimer(); } else stopFeedTimer();
  if (p === "attendance") { syncAttControls(); loadRoster(); }
  if (p === "schedule") loadSchedulePage();
  if (p === "employees") { renderEmployees(); }
  if (p === "reports") { if (!$("repStart").value) setPreset(S.rep.preset); else runReport(); }
  if (p === "settings") loadSettings();
}

function goAttendance(o = {}) {
  S.att.branch = o.branch || ""; S.att.shift = o.shift || ""; S.att.status = o.status || ""; S.att.q = "";
  if (o.date) S.att.date = o.date;
  if (S.page === "attendance") { if (!confirmDiscard()) return; syncAttControls(); loadRoster(); }
  else location.hash = "attendance";
}
document.addEventListener("click", ev => {
  const g = ev.target.closest("[data-go]");
  if (g) { goAttendance(JSON.parse(g.dataset.go)); return; }
  const h = ev.target.closest("[data-hist]");
  if (h) openHistory(+h.dataset.hist);
});

on("navSearch", "input", debounce(() => {
  const q = $("navSearch").value.trim();
  S.emp.q = q; if ($("empSearch")) $("empSearch").value = q;
  if (S.page !== "employees") { if (location.hash !== "#employees") location.hash = "#employees"; else showPage("employees"); }
  else empRedraw();
}, 300));

async function init() {
  try {
    /* التلاتة طلبات على التوازي بدل ما /api/me يستنى الأول — بيقصّر زمن هيكل
       التحميل الأول للنص تقريبًا ومفيش أي فرق في الترتيب من ناحية البيانات. */
    const [me, emps, settings] = await Promise.all([api("/api/me"), api("/api/employees"), api("/api/settings")]);
    S.role = me.role || "admin"; localStorage.setItem("bankai_role", S.role);
    applyRole();
    showApp();
    S.employees = emps; S.settings = settings;
    fillShiftLists();
    S.shown = false;
    if (!location.hash) location.hash = isAdmin() ? "dashboard" : "attendance"; else route();
  } catch (e) {
    /* لو طلب البوت فشل (شبكة/401/500) ما نسيبش المستخدم عالق على هيكل التحميل:
       صفحة فاضية + رسالة خطأ أحسن من شاشة واقفة للأبد. 401 كده مخلّص لوحده (logout). */
    if (S.token) { toast(e.message, "err"); S.shown = false; route(); }
  }
}

/* ============================== dashboard ============================== */
on("dashDate", "change", () => { S.dash.date = $("dashDate").value || todayISO(); loadDashboard(); });
on("dashPrev", "click", () => { S.dash.date = addDays(S.dash.date, -1); loadDashboard(); });
on("dashNext", "click", () => { S.dash.date = addDays(S.dash.date, 1); loadDashboard(); });
on("dashToday", "click", () => { S.dash.date = todayISO(); loadDashboard(); });

async function loadDashboard() {
  $("dashDate").value = S.dash.date;
  try { renderDashboard(await api(`/api/dashboard?attendance_date=${S.dash.date}`)); }
  catch (e) { toast(e.message, "err"); }
}

function renderDashboard(d) {
  const t = d.totals, date = d.date;
  $("dashSub").textContent = `${WD_AR[d.weekday] || ""} ${fmtDay(date, { day: "numeric", month: "long", year: "numeric" })}`;
  const kp = [
    ["مجدولون اليوم", t.scheduled, "brand", { date }], ["حاضر", t.present, "ok", { date, status: "Present" }],
    ["متأخر", t.late, "warn", { date, status: "Late" }], ["غائب", t.absent, "bad", { date, status: "Absent" }],
    ["بعذر", t.excused, "info", { date, status: "Excused" }], ["لم يُسجل", t.unmarked, "", { date, status: "none" }],
  ];
  const pct = v => t.scheduled ? Math.round(v * 100 / t.scheduled) : 0;
  $("dashKpis").innerHTML = kp.map(([l, v, c, go]) => `<button class="kpi ${c}" data-go="${esc(JSON.stringify(go))}" style="--p:${pct(v)}" title="${l}: ${v} من ${t.scheduled} مجدول"><span class="kl">${l}</span><span class="kv num${v ? "" : " is-zero"}">${v}</span><span class="ring${v ? "" : " is-zero"}"><b>${pct(v)}%</b></span></button>`).join("");
  $("dashNote").innerHTML = `<span>غرامات اليوم: <b>${money(t.fines)}</b></span><span>في إجازة اليوم: <b class="num">${d.off_today}</b> موظف</span><span>إجمالي الموظفين النشطين: <b class="num">${d.active_total}</b></span>`;
  $("dashBranches").innerHTML = Object.entries(d.branches).map(([name, b]) => branchPanel(name, b, date)).join("");
  renderTrend(d.trend, date);
  $("dashWatch").innerHTML = d.watchlist.length
    ? `<table class="mini-tbl"><thead><tr><th>الموظف</th><th>غياب</th><th>تأخير</th><th>غرامات</th></tr></thead><tbody>${d.watchlist.map(w => `<tr data-hist="${w.id}" style="cursor:pointer"><td><b>${esc(w.name)}</b><br><small class="muted">${esc(w.branch)} · ${sh(w.shift)}</small></td><td class="num">${w.absent}</td><td class="num">${w.late}</td><td class="num">${w.fines ? num(w.fines) : "—"}</td></tr>`).join("")}</tbody></table>`
    : `<p class="muted" style="padding:20px 0">مفيش غياب أو تأخير مسجل في آخر 30 يوم.</p>`;
}

/* ===== live activity feed (dashboard) ===== */
let feedTimer = null;
function relTime(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "الآن";
  const m = Math.floor(s / 60);
  if (m < 60) return `منذ ${m} ${m === 1 ? "دقيقة" : m === 2 ? "دقيقتين" : "دقائق"}`;
  const h = Math.floor(m / 60);
  if (h < 24) return `منذ ${h} ${h === 1 ? "ساعة" : h === 2 ? "ساعتين" : "ساعات"}`;
  return fmtDay(iso.slice(0, 10), { day: "numeric", month: "short" });
}
async function loadFeed() {
  const el = $("liveFeed"); if (!el || S.page !== "dashboard") return;
  try {
    const r = await api(`/api/roster?attendance_date=${S.dash.date}`, { silent: true });
    const rows = (r.employees || []).filter(e => e.attendance && e.attendance.status && e.attendance.updated_at)
      .sort((a, b) => new Date(b.attendance.updated_at) - new Date(a.attendance.updated_at)).slice(0, 8);
    const AV = { Present: "linear-gradient(135deg,#0e9f6e,#34d399)", Late: "linear-gradient(135deg,#d97706,#f0b429)", Absent: "linear-gradient(135deg,#e02424,#f87171)", Excused: "linear-gradient(135deg,#0284c7,#38bdf8)" };
    el.innerHTML = rows.length ? rows.map(e => {
      const a = e.attendance, lbl = (ST[a.status] || [a.status])[0];
      return `<div class="feed-row"><div class="feed-ava" style="background:${AV[a.status] || "#94a3b8"}">${esc(e.name.trim()[0] || "؟")}</div><div class="who"><b>${esc(e.name)}</b><small>سجّل ${lbl} — ${esc(e.branch || "")}</small></div><span class="feed-tm">${relTime(a.updated_at)}</span></div>`;
    }).join("") : `<div class="empty">لا توجد تسجيلات بعد اليوم</div>`;
  } catch (e) { /* keep old feed on error */ }
}
function restartFeedTimer() { stopFeedTimer(); loadFeed(); feedTimer = setInterval(loadFeed, 30000); }
function stopFeedTimer() { if (feedTimer) { clearInterval(feedTimer); feedTimer = null; } }

function branchPanel(name, b, date) {
  const done = b.scheduled - b.unmarked, pct = b.scheduled ? Math.round(done * 100 / b.scheduled) : 0;
  const rows = b.shifts.map(s => `<tr data-go="${esc(JSON.stringify({ branch: name, shift: s.shift, date }))}"><td><b>${sh(s.shift)}</b></td><td class="num">${s.scheduled}</td><td class="num">${s.present}</td><td class="num">${s.late}</td><td class="num">${s.absent}</td><td class="num">${s.excused}</td><td class="num">${s.unmarked}</td><td class="bar">${stackBar(s)}</td></tr>`).join("");
  return `<div class="panel"><div class="panel-h"><div><h3>${esc(name)}</h3><p>${b.scheduled} موظف مجدول اليوم</p></div><button class="btn sm" data-go="${esc(JSON.stringify({ branch: name, date }))}">فتح الحضور</button></div>
    <div class="panel-b"><div class="bp-top"><div><strong class="num">${pct}%</strong> <span class="muted">اكتمل التسجيل</span></div><span class="bp-fine">غرامات: ${money(b.fines)}</span></div>
    ${stackBar(b, "lg")}<div style="margin-top:10px">${legend(b)}</div>
    ${b.shifts.length ? `<table class="mini-tbl"><thead><tr><th>الشيفت</th><th>مجدول</th><th>حاضر</th><th>متأخر</th><th>غائب</th><th>بعذر</th><th>لم يُسجل</th><th></th></tr></thead><tbody>${rows}</tbody></table>` : `<p class="muted" style="margin-top:14px">لا يوجد موظفون مجدولون في هذا اليوم.</p>`}</div></div>`;
}

function renderTrend(trend, cur) {
  const tot = x => x.present + x.late + x.absent + x.excused;
  const max = Math.max(1, ...trend.map(tot));
  $("dashTrend").innerHTML = trend.map(x => {
    const t = tot(x), h = t ? Math.max(6, t / max * 110) : 3;
    const seg = (n, k) => n ? `<i style="height:${n * 100 / t}%;background:${CLR[k]}" title="${n}"></i>` : "";
    return `<button class="${x.date === cur ? "cur" : ""}" data-go="${esc(JSON.stringify({ date: x.date }))}" title="${fmtDay(x.date, { weekday: "long", day: "numeric", month: "long" })}"><b class="num">${t || "–"}</b><div class="col" style="height:${h}px">${seg(x.present, "present")}${seg(x.late, "late")}${seg(x.absent, "absent")}${seg(x.excused, "excused")}</div><small>${fmtDay(x.date, { weekday: "short", day: "numeric" })}</small></button>`;
  }).join("");
}

/* ============================== attendance ============================== */
const curStatus = e => (S.draft.get(e.id)?.status ?? e.attendance?.status ?? "");
const emptyAtt = { status: "", fine: 0, reason: "", note: "" };
const rowState = e => S.draft.get(e.id) ?? e.attendance ?? emptyAtt;

function getDraft(e) {
  let d = S.draft.get(e.id);
  if (!d) {
    const a = e.attendance || {};
    d = { status: a.status || "", fine: a.fine || 0, reason: a.reason || "", note: a.note || "", fineTouched: (a.fine || 0) > 0 };
    S.draft.set(e.id, d);
  }
  return d;
}
function maybeClean(e) {
  const d = S.draft.get(e.id); if (!d) return;
  const a = e.attendance || emptyAtt;
  if (d.status === (a.status || "") && Number(d.fine || 0) === Number(a.fine || 0) && (d.reason || "") === (a.reason || "") && (d.note || "") === (a.note || "")) S.draft.delete(e.id);
}
function confirmDiscard() {
  if (!S.draft.size) return true;
  if (confirm("عندك تسجيلات حضور لم تُحفظ. تجاهلها والمتابعة؟")) { S.draft.clear(); updateSaveBar(); return true; }
  return false;
}

function syncAttControls() {
  $("attDate").value = S.att.date;
  $("attSearch").value = S.att.q;
  $("attType").value = S.att.type;
  $("attGroup").checked = S.att.group;
  $("attDayName").textContent = fmtDay(S.att.date, { weekday: "long", day: "numeric", month: "long" });
  qsa("#attBranchSeg button").forEach(b => b.classList.toggle("on", b.dataset.branch === S.att.branch));
}

async function loadRoster() {
  const date = S.att.date;
  $("attDayName").textContent = fmtDay(date, { weekday: "long", day: "numeric", month: "long" });
  try {
    const r = await api(`/api/roster?attendance_date=${date}`);
    if (date !== S.att.date) return;
    S.roster = r; S.rmap = new Map(r.employees.map(e => [e.id, e])); S.draft.clear();
    fillAttShiftOptions();
    renderAttendance(); renderOff(); updateSaveBar();
  } catch (e) { toast(e.message, "err"); }
}

function fillAttShiftOptions() {
  const sel = $("attShift");
  const pool = S.roster.employees.filter(e => !S.att.branch || e.branch === S.att.branch);
  const shifts = [...new Set(pool.map(e => e.shift))].sort((a, b) => a.localeCompare(b, "ar"));
  if (S.att.shift && !shifts.includes(S.att.shift)) S.att.shift = "";
  sel.innerHTML = `<option value="">كل الشيفتات</option>` + shifts.map(s => `<option>${esc(s)}</option>`).join("");
  sel.value = S.att.shift;
}

function filterRoster(skipStatus) {
  const { branch, shift, type, q, status } = S.att, needle = q.trim().toLowerCase();
  return S.roster.employees.filter(e => {
    if (branch && e.branch !== branch) return false;
    if (shift && e.shift !== shift) return false;
    if (type && e.employment_type !== type) return false;
    if (needle && !`${e.name} ${e.employee_code} ${e.phone}`.toLowerCase().includes(needle)) return false;
    if (!skipStatus && status) { const st = curStatus(e); if (status === "none" ? st !== "" : st !== status) return false; }
    return true;
  });
}

function countsOf(list) {
  const c = { all: list.length, none: 0, Present: 0, Late: 0, Absent: 0, Excused: 0 };
  list.forEach(e => { const s = curStatus(e); if (s) c[s]++; else c.none++; });
  return c;
}
const asStack = c => ({ scheduled: c.all, present: c.Present, late: c.Late, absent: c.Absent, excused: c.Excused, unmarked: c.none });

function contactHTML(e) {
  if (!e.phone) return "";
  let wa = e.phone.replace(/\D/g, "");
  if (wa.startsWith("00")) wa = wa.slice(2); else if (wa.startsWith("0")) wa = "20" + wa.slice(1);
  return `<span class="contact"><a href="tel:${esc(e.phone)}" aria-label="اتصال" title="اتصال"><svg class="i"><use href="#i-phone"/></svg></a><a href="https://wa.me/${wa}" target="_blank" rel="noopener" aria-label="واتساب" title="واتساب"><svg class="i"><use href="#i-msg"/></svg></a></span>`;
}

function rowHTML(e) {
  const d = rowState(e), st = d.status || "";
  const fineOn = st === "Late" || st === "Absent";
  const sub = [esc(e.employee_code || "بدون كود"), e.employment_type];
  if (e.break_start) sub.push(`بريك <span class="num">${esc(e.break_start)}</span>`);
  if (!S.att.group) sub.push(esc(e.branch), sh(e.shift));
  return `<div class="arow${S.draft.has(e.id) ? " dirty" : ""}" data-id="${e.id}" data-st="${st}">
    <div class="a-who"><div class="a-name">${esc(e.name)}${contactHTML(e)}</div><div class="a-sub">${sub.join(" · ")}</div></div>
    <div class="seg" role="group" aria-label="حالة ${esc(e.name)}">${STK.map(k => `<button type="button" class="${k}${st === k ? " on" : ""}" data-set="${k}" aria-pressed="${st === k}">${ST[k][0]}</button>`).join("")}</div>
    <input class="inp f-fine" type="number" min="0" step="1" inputmode="numeric" value="${fineOn && d.fine ? d.fine : ""}" placeholder="غرامة" aria-label="الغرامة" ${fineOn ? "" : "disabled"}>
    <input class="inp f-reason" list="reasonList" maxlength="500" value="${esc(d.reason || "")}" placeholder="السبب" aria-label="السبب" ${st && st !== "Present" ? "" : "disabled"}>
    <button type="button" class="note-btn${d.note ? " has" : ""}" data-note title="ملاحظة" aria-label="ملاحظة"><svg class="i"><use href="#i-note"/></svg></button>
    <input class="inp f-note${d.note ? "" : " hidden"}" maxlength="500" value="${esc(d.note || "")}" placeholder="ملاحظة إضافية (اختياري)" aria-label="ملاحظة" ${st ? "" : "disabled"}>
  </div>`;
}

function sumText(c) {
  const p = [];
  if (c.Present) p.push(`حاضر ${c.Present}`); if (c.Late) p.push(`متأخر ${c.Late}`);
  if (c.Absent) p.push(`غائب ${c.Absent}`); if (c.Excused) p.push(`بعذر ${c.Excused}`);
  if (c.none) p.push(`لم يُسجل ${c.none}`);
  return p.join(" · ");
}

function renderAttendance() {
  if (!S.roster) return;
  const base = filterRoster(true), list = S.att.status ? filterRoster(false) : base;
  S.view = { base, list, groups: new Map() };

  const all = S.roster.employees;
  const bc = { "": all.length, "QCD 1": all.filter(e => e.branch === "QCD 1").length, "QCD 2": all.filter(e => e.branch === "QCD 2").length };
  qsa("#attBranchSeg button").forEach(b => { b.classList.toggle("on", b.dataset.branch === S.att.branch); qs("small", b).textContent = bc[b.dataset.branch] ?? 0; });

  let html = "";
  if (S.att.group) {
    list.forEach(e => { const k = `${e.branch}||${e.shift}`; if (!S.view.groups.has(k)) S.view.groups.set(k, []); S.view.groups.get(k).push(e); });
    for (const [k, arr] of S.view.groups) {
      const [b, s] = k.split("||"), c = countsOf(arr);
      html += `<section class="agroup${S.att.collapsed.has(k) ? " collapsed" : ""}" data-g="${esc(k)}"><header class="ghead" data-gtoggle><svg class="i chev"><use href="#i-chev"/></svg><span class="g-title">${esc(b)}</span><span class="g-shift">${sh(s)}</span><span class="g-n">${arr.length} موظف</span><div class="stack g-bar">${stackInner(asStack(c))}</div><span class="g-sum">${sumText(c)}</span><button type="button" class="btn sm" data-gpresent>حاضر للباقي</button></header><div class="gbody">${arr.map(rowHTML).join("")}</div></section>`;
    }
  } else html = `<div class="gbody">${list.map(rowHTML).join("")}</div>`;
  $("attList").innerHTML = html;
  $("attList").classList.toggle("hidden", !list.length);

  const emp = $("attEmpty");
  emp.classList.toggle("hidden", list.length > 0);
  if (!list.length) emp.innerHTML = all.length ? `<b>لا توجد نتائج مطابقة</b>غيّر الفلاتر أو امسح البحث.` : `<b>لا يوجد موظفون مجدولون في هذا اليوم</b>الموظف اللي يوافق يوم إجازته الأسبوعية بيختفي من قائمة الحضور.`;
  updateAttSummary();
  renderOff();
}

function updateAttSummary() {
  const c = countsOf(S.view.base);
  const defs = [["", "الكل", c.all, ""], ["none", "لم يُسجل", c.none, "none"], ["Present", "حاضر", c.Present, "ok"], ["Late", "متأخر", c.Late, "warn"], ["Absent", "غائب", c.Absent, "bad"], ["Excused", "بعذر", c.Excused, "info"]];
  $("attChips").innerHTML = defs.map(([k, l, n, d]) => `<button class="chip${S.att.status === k ? " on" : ""}" data-status="${k}">${d ? `<i class="dot ${d}"></i>` : ""}${l} <b class="num">${n}</b></button>`).join("");
  $("attProgressText").textContent = c.all ? `تم تسجيل ${c.all - c.none} من ${c.all}` : "";
  $("attProgress").innerHTML = stackInner(asStack(c));
  qsa(".agroup", $("attList")).forEach(g => {
    const arr = S.view.groups.get(g.dataset.g); if (!arr) return;
    const gc = countsOf(arr);
    qs(".g-bar", g).innerHTML = stackInner(asStack(gc));
    qs(".g-sum", g).textContent = sumText(gc);
  });
}

function renderOff() {
  const off = (S.roster?.off_employees || []).filter(e => !S.att.branch || e.branch === S.att.branch);
  $("attOff").classList.toggle("hidden", !off.length);
  $("attOffSum").textContent = `في إجازة اليوم (${off.length})`;
  $("attOffList").innerHTML = off.map(e => `<span class="tag">${esc(e.name)} · ${esc(e.branch)}</span>`).join("");
}

function updateRowUI(e) {
  const row = qs(`.arow[data-id="${e.id}"]`, $("attList")); if (!row) return;
  const d = rowState(e), st = d.status || "";
  row.dataset.st = st; row.classList.toggle("dirty", S.draft.has(e.id));
  qsa(".seg button", row).forEach(b => { const o = b.dataset.set === st; b.classList.toggle("on", o); b.setAttribute("aria-pressed", o); });
  const fineOn = st === "Late" || st === "Absent";
  const fine = qs(".f-fine", row), reason = qs(".f-reason", row), note = qs(".f-note", row);
  fine.disabled = !fineOn; fine.value = fineOn && d.fine ? d.fine : "";
  reason.disabled = !st || st === "Present"; reason.value = d.reason || "";
  note.disabled = !st; note.value = d.note || "";
  qs(".note-btn", row).classList.toggle("has", !!d.note);
  if (d.note) note.classList.remove("hidden");
}

function setStatus(e, st) {
  const d = getDraft(e);
  if (d.status === st) st = "";
  d.status = st;
  if (st === "Present") { d.fine = 0; d.reason = ""; }
  else if (st === "Excused") { d.fine = 0; }
  else if (st === "Late" || st === "Absent") { if (!d.fineTouched) d.fine = S.settings[st === "Late" ? "fine_late" : "fine_absent"] || 0; }
  else { d.fine = 0; d.reason = ""; d.note = ""; }
  maybeClean(e); updateRowUI(e); updateAttSummary(); updateSaveBar();
}

function markPresent(arr) {
  let n = 0;
  arr.forEach(e => {
    if (curStatus(e) !== "") return;
    const d = getDraft(e); d.status = "Present"; d.fine = 0; d.reason = ""; n++;
  });
  if (!n) { toast("كل الموظفين في العرض الحالي ليهم حالة بالفعل."); return; }
  renderAttendance(); updateSaveBar();
  toast(`تم تحديد ${n} موظف كحاضر. اضغط «حفظ الكل» للتأكيد.`, "ok");
}

function updateSaveBar() {
  const n = S.draft.size;
  $("saveBar").classList.toggle("hidden", n === 0);
  $("saveMsg").textContent = `${n} تسجيل لم يُحفظ بعد`;
}

async function saveAttendance() {
  if (!S.draft.size) return;
  const items = [], invalid = [];
  for (const [id, d] of S.draft) {
    const fine = (d.status === "Late" || d.status === "Absent") ? Number(d.fine || 0) : 0;
    if (fine > 0 && !String(d.reason || "").trim()) invalid.push(id);
    items.push({ employee_id: id, status: d.status, fine, reason: d.reason || "", note: d.note || "" });
  }
  qsa(".arow.invalid").forEach(r => r.classList.remove("invalid"));
  if (invalid.length) {
    invalid.forEach(id => qs(`.arow[data-id="${id}"]`)?.classList.add("invalid"));
    const first = S.rmap.get(invalid[0]);
    const row = qs(`.arow[data-id="${invalid[0]}"]`);
    if (row) { row.scrollIntoView({ block: "center", behavior: "smooth" }); qs(".f-reason", row).focus({ preventScroll: true }); }
    toast(`اكتب سبب الغرامة أولًا${invalid.length > 1 ? ` (${invalid.length} موظفين)` : ` لـ ${first?.name || "الموظف"}`}.`, "err");
    return;
  }
  const btn = $("saveBtn"); btn.disabled = true;
  try {
    const r = await api("/api/attendance/bulk", { method: "POST", body: { attendance_date: S.roster.date, items } });
    const failed = new Set(r.errors.map(x => x.employee_id));
    for (const idStr of Object.keys(r.records)) {
      const id = +idStr, e = S.rmap.get(id); if (!e) continue;
      e.attendance = r.records[idStr]; S.draft.delete(id); updateRowUI(e);
    }
    updateAttSummary(); updateSaveBar();
    if (r.errors.length) { const x = r.errors[0]; toast(`تعذر حفظ ${failed.size} موظف — ${x.name ? x.name + ": " : ""}${x.detail}`, "err"); }
    else toast(`تم حفظ ${r.saved + r.cleared} تسجيل.`, "ok");
  } catch (e) { toast(e.message, "err"); }
  finally { btn.disabled = false; }
}

/* attendance events */
on("attDate", "change", () => { if (!confirmDiscard()) { $("attDate").value = S.att.date; return; } S.att.date = $("attDate").value || todayISO(); loadRoster(); });
const shiftDay = n => { if (!confirmDiscard()) return; S.att.date = addDays(S.att.date, n); $("attDate").value = S.att.date; loadRoster(); };
on("attPrev", "click", () => shiftDay(-1));
on("attNext", "click", () => shiftDay(1));
on("attToday", "click", () => { if (!confirmDiscard()) return; S.att.date = todayISO(); $("attDate").value = S.att.date; loadRoster(); });
on("attRefresh", "click", () => { if (confirmDiscard()) loadRoster(); });
on("attBranchSeg", "click", ev => { const b = ev.target.closest("button"); if (!b) return; S.att.branch = b.dataset.branch; fillAttShiftOptions(); renderAttendance(); });
on("attShift", "change", () => { S.att.shift = $("attShift").value; renderAttendance(); });
on("attType", "change", () => { S.att.type = $("attType").value; renderAttendance(); });
on("attSearch", "input", debounce(() => { S.att.q = $("attSearch").value; renderAttendance(); }, 120));
on("attGroup", "change", () => { S.att.group = $("attGroup").checked; renderAttendance(); });
on("attCollapse", "click", () => {
  const gs = qsa(".agroup", $("attList")); if (!gs.length) return;
  const anyOpen = gs.some(g => !g.classList.contains("collapsed"));
  gs.forEach(g => { g.classList.toggle("collapsed", anyOpen); anyOpen ? S.att.collapsed.add(g.dataset.g) : S.att.collapsed.delete(g.dataset.g); });
  $("attCollapse").setAttribute("aria-expanded", String(!anyOpen));
});
on("attChips", "click", ev => { const c = ev.target.closest(".chip"); if (!c) return; S.att.status = c.dataset.status; renderAttendance(); });
on("markPresentBtn", "click", () => { if (S.roster) markPresent(S.view.base); });
on("saveBtn", "click", saveAttendance);
on("saveDiscard", "click", () => { if (!S.draft.size) return; S.draft.clear(); renderAttendance(); updateSaveBar(); });

on("attList", "click", ev => {
  const t = ev.target;
  const sb = t.closest("[data-set]");
  if (sb) { const e = S.rmap.get(+sb.closest(".arow").dataset.id); if (e) setStatus(e, sb.dataset.set); return; }
  const nb = t.closest("[data-note]");
  if (nb) {
    const row = nb.closest(".arow"), inp = qs(".f-note", row);
    if (inp.disabled) { toast("اختر حالة الحضور أولًا."); return; }
    inp.classList.toggle("hidden"); if (!inp.classList.contains("hidden")) inp.focus();
    return;
  }
  const gp = t.closest("[data-gpresent]");
  if (gp) { const arr = S.view.groups.get(gp.closest(".agroup").dataset.g); if (arr) markPresent(arr); return; }
  const gt = t.closest("[data-gtoggle]");
  if (gt && !t.closest("a")) {
    const g = gt.closest(".agroup"), k = g.dataset.g;
    g.classList.toggle("collapsed");
    g.classList.contains("collapsed") ? S.att.collapsed.add(k) : S.att.collapsed.delete(k);
  }
});
on("attList", "input", ev => {
  const row = ev.target.closest(".arow"); if (!row) return;
  const e = S.rmap.get(+row.dataset.id); if (!e) return;
  const d = getDraft(e), cl = ev.target.classList;
  if (cl.contains("f-fine")) { d.fine = ev.target.value === "" ? 0 : Number(ev.target.value); d.fineTouched = true; }
  else if (cl.contains("f-reason")) d.reason = ev.target.value;
  else if (cl.contains("f-note")) { d.note = ev.target.value; qs(".note-btn", row).classList.toggle("has", !!d.note); }
  else return;
  row.classList.remove("invalid");
  maybeClean(e); row.classList.toggle("dirty", S.draft.has(e.id)); updateSaveBar();
});

window.addEventListener("beforeunload", ev => { if (anyDrafts()) { ev.preventDefault(); ev.returnValue = ""; } });
document.addEventListener("keydown", ev => {
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s" && S.page === "attendance") { ev.preventDefault(); saveAttendance(); return; }
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s" && S.page === "schedule") { ev.preventDefault(); schSave(); return; }
  if (ev.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
    const el = S.page === "attendance" ? $("attSearch") : S.page === "employees" ? $("empSearch") : S.page === "schedule" ? $("schSearch") : null;
    if (el) { ev.preventDefault(); el.focus(); }
  }
});

/* ============================== employees ============================== */
function fillShiftLists() {
  const shifts = [...new Set(S.employees.map(e => e.shift))].sort((a, b) => a.localeCompare(b, "ar"));
  $("shiftList").innerHTML = shifts.map(s => `<option value="${esc(s)}"></option>`).join("");
  const sel = $("empShift"), cur = S.emp.shift;
  sel.innerHTML = `<option value="">كل الشيفتات</option>` + shifts.map(s => `<option>${esc(s)}</option>`).join("");
  if (shifts.includes(cur)) sel.value = cur; else { S.emp.shift = ""; sel.value = ""; }
}
async function refreshEmployees() {
  S.employees = await api("/api/employees");
  fillShiftLists();
  if (S.page === "employees") renderEmployees();
}

function empFiltered() {
  const f = S.emp, q = f.q.trim().toLowerCase();
  const list = S.employees.filter(e =>
    (f.active === "" || String(e.active ? 1 : 0) === f.active) && (!f.branch || e.branch === f.branch) && (!f.shift || e.shift === f.shift) &&
    (!f.type || e.employment_type === f.type) && (!f.day || e.day_off === f.day) && (!q || `${e.name} ${e.employee_code} ${e.phone}`.toLowerCase().includes(q)));
  return sortRows(list, f, (e, k) => k === "day_off" ? WEEKDAYS.indexOf(e.day_off) : k === "active" ? (e.active ? 0 : 1) : (e[k] ?? ""));
}

function renderEmployees() {
  const list = S.emp.filtered = empFiltered();
  const act = S.employees.filter(e => e.active).length;
  $("empSub").textContent = `${act} موظف نشط · ${S.employees.length - act} موقوف`;
  const total = list.length, pages = Math.max(1, Math.ceil(total / S.emp.size));
  S.emp.page = Math.min(S.emp.page, pages);
  const slice = list.slice((S.emp.page - 1) * S.emp.size, S.emp.page * S.emp.size);
  $("empRows").innerHTML = slice.map(e => {
    const sel = S.emp.selected.has(e.id);
    const adminActs = isAdmin() ? `<button class="btn sm" data-edit="${e.id}">تعديل</button>${e.active ? `<button class="btn sm danger" data-deact="${e.id}">إيقاف</button>` : `<button class="btn sm" data-act="${e.id}">تفعيل</button>`}` : "";
    return `<tr class="${sel ? "sel" : ""}" data-id="${e.id}"><td class="c"><input type="checkbox" data-sel ${sel ? "checked" : ""} aria-label="تحديد ${esc(e.name)}"></td>
      <td class="who"><button class="link" data-hist="${e.id}">${esc(e.name)}</button><small>${esc(e.employee_code || "بدون كود")}</small></td>
      <td>${esc(e.branch)}</td><td>${sh(e.shift)}</td>
      <td><span class="tag ${e.employment_type === "FT" ? "brand" : ""}">${e.employment_type === "FT" ? "Full Time" : "Part Time"}</span></td>
      <td>${WD_AR[e.day_off] || e.day_off}</td>
      <td>${e.phone ? `<a class="num" href="tel:${esc(e.phone)}">${esc(e.phone)}</a>` : `<span class="muted">—</span>`}</td>
      <td><span class="tag ${e.active ? "ok" : "off"}">${e.active ? "نشط" : "موقوف"}</span></td>
      <td><div class="row-actions"><button class="btn sm" data-hist="${e.id}" title="سجل الحضور"><svg class="i"><use href="#i-clock"/></svg>السجل</button>${adminActs}</div></td></tr>`;
  }).join("");
  $("empEmpty").classList.toggle("hidden", total > 0);
  markSort("#page-employees th.sort", "sort", S.emp);
  renderPager($("empPager"), total, S.emp, renderEmployees);
  updateBulkBar();
}

function updateBulkBar() {
  const n = S.emp.selected.size;
  $("empBulkBar").classList.toggle("hidden", n === 0);
  $("empSelCount").textContent = `تم تحديد ${n} موظف`;
  const f = S.emp.filtered || [], sel = f.filter(e => S.emp.selected.has(e.id)).length;
  const all = $("empCheckAll");
  all.checked = f.length > 0 && sel === f.length; all.indeterminate = sel > 0 && sel < f.length;
}

bindSort("#page-employees th.sort", "sort", S.emp, renderEmployees);
const empRedraw = () => { S.emp.page = 1; renderEmployees(); };
on("empSearch", "input", debounce(() => { S.emp.q = $("empSearch").value; empRedraw(); }, 150));
[["empBranch", "branch"], ["empShift", "shift"], ["empType", "type"], ["empDay", "day"], ["empStatus", "active"]].forEach(([id, k]) => on(id, "change", () => { S.emp[k] = $(id).value; empRedraw(); }));
on("empRows", "change", ev => {
  const cb = ev.target.closest("[data-sel]"); if (!cb) return;
  const tr = cb.closest("tr"), id = +tr.dataset.id;
  cb.checked ? S.emp.selected.add(id) : S.emp.selected.delete(id);
  tr.classList.toggle("sel", cb.checked); updateBulkBar();
});
on("empCheckAll", "change", ev => {
  (S.emp.filtered || []).forEach(e => ev.target.checked ? S.emp.selected.add(e.id) : S.emp.selected.delete(e.id));
  renderEmployees();
});
on("empRows", "click", async ev => {
  const ed = ev.target.closest("[data-edit]"); if (ed) { openEmpDialog(+ed.dataset.edit); return; }
  const de = ev.target.closest("[data-deact]");
  if (de) { if (confirm("سيتم إيقاف الموظف مع الاحتفاظ بسجل حضوره السابق. هل تريد المتابعة؟")) bulkAction([+de.dataset.deact], "deactivate"); return; }
  const ac = ev.target.closest("[data-act]"); if (ac) bulkAction([+ac.dataset.act], "activate");
});

async function bulkAction(ids, action, extra = {}) {
  try {
    const r = await api("/api/employees/bulk", { method: "POST", body: { ids, action, ...extra } });
    toast(action === "update" ? `تم تعديل ${r.updated} موظف.` : action === "activate" ? `تم تفعيل ${r.updated} موظف.` : `تم إيقاف ${r.updated} موظف.`, "ok");
    S.emp.selected.clear(); await refreshEmployees();
    return true;
  } catch (e) { toast(e.message, "err"); return false; }
}
on("empBulkClear", "click", () => { S.emp.selected.clear(); renderEmployees(); });
on("empBulkActivate", "click", () => bulkAction([...S.emp.selected], "activate"));
on("empBulkDeactivate", "click", () => { if (confirm(`إيقاف ${S.emp.selected.size} موظف؟ سجل حضورهم السابق هيفضل محفوظ.`)) bulkAction([...S.emp.selected], "deactivate"); });
on("empBulkEdit", "click", () => {
  $("bulkForm").reset(); $("bulkError").textContent = "";
  $("bulkCount").textContent = `سيتم التعديل على ${S.emp.selected.size} موظف`;
  $("bulkDialog").showModal();
});
on("bulkCancel", "click", () => $("bulkDialog").close());
on("bulkForm", "submit", async e => {
  e.preventDefault();
  const extra = { branch: $("bulkBranch").value || null, employment_type: $("bulkType").value || null, shift: $("bulkShift").value.trim() || null, day_off: $("bulkDay").value || null };
  if (Object.values(extra).every(v => v === null)) { $("bulkError").textContent = "اختر قيمة واحدة على الأقل للتعديل."; return; }
  if (await bulkAction([...S.emp.selected], "update", extra)) $("bulkDialog").close();
});

/* add / edit dialog */
on("empAdd", "click", () => openEmpDialog());
on("fClose", "click", () => $("empDialog").close());
on("fCancel", "click", () => $("empDialog").close());
function openEmpDialog(id = null) {
  $("fError").textContent = ""; $("empForm").reset();
  $("fId").value = ""; $("fActive").checked = true; $("fDay").value = "Friday";
  $("fTitle").textContent = id ? "تعديل بيانات الموظف" : "إضافة موظف";
  if (id) {
    const e = S.employees.find(x => x.id === id); if (!e) return;
    $("fId").value = e.id; $("fName").value = e.name; $("fCode").value = e.employee_code || "";
    $("fBranch").value = e.branch; $("fType").value = e.employment_type; $("fShift").value = e.shift;
    $("fDay").value = e.day_off; $("fPhone").value = e.phone || ""; $("fActive").checked = e.active;
  } else if (S.emp.branch) $("fBranch").value = S.emp.branch;
  $("empDialog").showModal(); $("fName").focus();
}
on("empForm", "submit", async ev => {
  ev.preventDefault();
  const data = { name: $("fName").value.trim(), employee_code: $("fCode").value.trim() || null, branch: $("fBranch").value, employment_type: $("fType").value, shift: $("fShift").value.trim(), day_off: $("fDay").value, phone: $("fPhone").value.trim() || null, active: $("fActive").checked };
  const id = $("fId").value;
  try {
    await api(id ? `/api/employees/${id}` : "/api/employees", { method: id ? "PUT" : "POST", body: data });
    $("empDialog").close(); toast("تم حفظ بيانات الموظف.", "ok");
    await refreshEmployees();
  } catch (err) { $("fError").textContent = err.message; }
});

on("empExport", "click", () => download("/api/employees/export.csv", "bankai_employees.csv").catch(e => toast(e.message, "err")));

/* import */
const nh = s => String(s || "").toLowerCase().replace(/[\s_\-]/g, "").replace(/[أإآ]/g, "ا").replace(/ة/g, "ه");
const IMPORT_ALIASES = {
  name: ["name", "الاسم", "اسم", "اسم الموظف", "الموظف"],
  employee_code: ["employee_code", "code", "الكود", "كود", "كود الموظف", "رقم الموظف", "id"],
  branch: ["branch", "الفرع", "فرع"],
  shift: ["shift", "الشيفت", "شيفت", "الوردية", "وردية"],
  employment_type: ["employment_type", "type", "النوع", "نوع", "نوع التعاقد", "التعاقد"],
  day_off: ["day_off", "off", "الاجازة", "اجازة", "يوم الاجازة", "الإجازة", "يوم الإجازة"],
  phone: ["phone", "mobile", "tel", "الموبايل", "الموبيل", "الهاتف", "هاتف", "التليفون", "تليفون", "رقم الهاتف", "رقم الموبايل"],
  active: ["active", "نشط", "الحالة"],
};
const IMPORT_KEYS = {};
Object.entries(IMPORT_ALIASES).forEach(([k, arr]) => arr.forEach(a => { IMPORT_KEYS[nh(a)] = k; }));
const IMPORT_ORDER = ["name", "employee_code", "branch", "shift", "employment_type", "day_off", "phone"];

function parseDelimited(text) {
  text = text.replace(/^﻿/, "");
  const first = text.split(/\r?\n/).find(l => l.trim()) || "";
  const counts = { "\t": (first.match(/\t/g) || []).length, ",": (first.match(/,/g) || []).length, ";": (first.match(/;/g) || []).length };
  const delim = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0];
  const rows = []; let row = [], cell = "", q = false;
  const endRow = () => { row.push(cell); cell = ""; if (row.some(x => x.trim() !== "")) rows.push(row); row = []; };
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) { if (c === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; } else cell += c; }
    else if (c === '"') q = true;
    else if (c === delim) { row.push(cell); cell = ""; }
    else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; endRow(); }
    else cell += c;
  }
  endRow();
  return rows.map(r => r.map(x => x.trim()));
}
function rowsToObjects(rows) {
  if (!rows.length) return [];
  const map = rows[0].map(h => IMPORT_KEYS[nh(h)] || null);
  const hasHeader = map.includes("name") && map.filter(Boolean).length >= 2;
  const m = hasHeader ? map : IMPORT_ORDER, body = hasHeader ? rows.slice(1) : rows;
  return body.map(r => { const o = {}; m.forEach((k, i) => { if (k) o[k] = r[i] ?? ""; }); return o; });
}

let impSeq = 0;
on("empImport", "click", () => { $("impText").value = ""; $("impSummary").innerHTML = ""; $("impErrors").classList.add("hidden"); $("impRun").disabled = true; $("impRun").textContent = "استيراد"; $("impDialog").showModal(); });
on("impClose", "click", () => $("impDialog").close());
on("impCancel", "click", () => $("impDialog").close());
on("impText", "input", debounce(() => runImport(true), 450));
on("impUpdate", "change", () => runImport(true));
on("impFile", "change", async ev => {
  const f = ev.target.files[0]; if (!f) return;
  const buf = await f.arrayBuffer();
  let text;
  try { text = new TextDecoder("utf-8", { fatal: true }).decode(buf); }
  catch { text = new TextDecoder("windows-1256").decode(buf); }
  $("impText").value = text; ev.target.value = "";
  runImport(true);
});
on("impTemplate", "click", () => {
  const csv = "﻿name,employee_code,branch,shift,employment_type,day_off,phone\nأحمد محمد,1001,QCD 1,12 AM - 8 AM,FT,Friday,01012345678\nمحمود علي,1002,QCD 2,8 AM - 4 PM,PT,Saturday,\n";
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a"); a.href = url; a.download = "employees_template.csv"; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
});
on("impRun", "click", () => runImport(false));

async function runImport(dry) {
  const rows = rowsToObjects(parseDelimited($("impText").value));
  const seq = ++impSeq;
  if (!rows.length) { $("impSummary").innerHTML = ""; $("impErrors").classList.add("hidden"); $("impRun").disabled = true; return; }
  try {
    const r = await api("/api/employees/import", { method: "POST", body: { rows, update_existing: $("impUpdate").checked, dry_run: dry } });
    if (seq !== impSeq && dry) return;
    if (!dry) {
      toast(`تم الاستيراد: ${r.created} جديد، ${r.updated} محدّث${r.errors.length ? `، وتم تجاهل ${r.errors.length} صف فيه أخطاء` : ""}.`, "ok");
      $("impDialog").close(); await refreshEmployees(); return;
    }
    $("impSummary").innerHTML = `<span class="tag ok">جديد: ${r.created}</span><span class="tag brand">تحديث: ${r.updated}</span>${r.skipped ? `<span class="tag">متجاهل: ${r.skipped}</span>` : ""}${r.errors.length ? `<span class="tag bad">أخطاء: ${r.errors.length}</span>` : ""}`;
    const box = $("impErrors");
    box.classList.toggle("hidden", !r.errors.length);
    box.innerHTML = r.errors.slice(0, 60).map(x => `<div><b>صف ${x.row}</b>${x.name ? ` (${esc(x.name)})` : ""}: ${esc(x.detail)}</div>`).join("") + (r.errors.length > 60 ? `<div class="muted">… و${r.errors.length - 60} أخطاء أخرى</div>` : "");
    const n = r.created + r.updated;
    $("impRun").disabled = n === 0; $("impRun").textContent = n ? `استيراد ${n} موظف` : "استيراد";
  } catch (e) { toast(e.message, "err"); }
}

/* ============================== history dialog ============================== */
on("histClose", "click", () => $("histDialog").close());
on("histClose2", "click", () => $("histDialog").close());
on("histRange", "change", loadHistory);
$("histDialog").addEventListener("click", e => { if (e.target === $("histDialog")) $("histDialog").close(); });

async function openHistory(id) {
  S.hist.id = id; $("histRange").value = "30";
  $("histTitle").textContent = "…"; $("histSub").textContent = ""; $("histStats").innerHTML = ""; $("histRows").innerHTML = "";
  if (!$("histDialog").open) $("histDialog").showModal();
  await loadHistory();
}
async function loadHistory() {
  const range = $("histRange").value, end = todayISO();
  let start;
  if (range === "month") { const d = new Date(); start = isoOf(new Date(d.getFullYear(), d.getMonth(), 1)); } else start = addDays(end, -(+range - 1));
  try {
    const r = await api(`/api/reports/employee/${S.hist.id}?start=${start}&end=${end}`);
    const e = r.employee, s = r.stats;
    $("histTitle").textContent = e.name;
    $("histSub").innerHTML = `${esc(e.branch)} · ${sh(e.shift)} · ${esc(e.employee_code || "بدون كود")} · إجازة ${WD_AR[e.day_off] || e.day_off}${e.active ? "" : " · موقوف"}`;
    $("histStats").innerHTML = `<span class="tag">تسجيلات: ${s.total}</span><span class="tag ok">حضور: ${s.present}</span><span class="tag warn">تأخير: ${s.late}</span><span class="tag bad">غياب: ${s.absent}</span><span class="tag info">بعذر: ${s.excused}</span><span class="tag brand">غرامات: ${money(s.fine_total)}</span>`;
    $("histRows").innerHTML = r.records.map(a => `<tr><td>${WD_AR[a.weekday] || ""} ${fmtDay(a.attendance_date, { day: "numeric", month: "short" })}</td><td><span class="tag ${a.status}">${ST[a.status]?.[0] || a.status}</span></td><td class="num">${a.fine ? money(a.fine) : "—"}</td><td>${esc(a.reason) || "—"}</td><td>${esc(a.note) || "—"}</td></tr>`).join("");
    $("histEmpty").classList.toggle("hidden", r.records.length > 0);
  } catch (err) { toast(err.message, "err"); }
}

/* ============================== reports ============================== */
function setPreset(p) {
  const t = todayISO(), d = new Date(); let s = t, e = t;
  if (p === "yesterday") s = e = addDays(t, -1);
  else if (p === "7") s = addDays(t, -6);
  else if (p === "30") s = addDays(t, -29);
  else if (p === "month") s = isoOf(new Date(d.getFullYear(), d.getMonth(), 1));
  else if (p === "lastmonth") { s = isoOf(new Date(d.getFullYear(), d.getMonth() - 1, 1)); e = isoOf(new Date(d.getFullYear(), d.getMonth(), 0)); }
  S.rep.preset = p; $("repStart").value = s; $("repEnd").value = e;
  qsa("#repPresets button").forEach(b => b.classList.toggle("on", b.dataset.p === p));
  runReport();
}
on("repPresets", "click", ev => { const b = ev.target.closest("button"); if (b) setPreset(b.dataset.p); });
["repStart", "repEnd"].forEach(id => on(id, "change", () => { S.rep.preset = ""; qsa("#repPresets button").forEach(b => b.classList.remove("on")); }));
on("repRun", "click", runReport);
on("repBranch", "change", runReport);
on("repSearch", "input", debounce(() => { S.rep.page = 1; renderReportRows(); }, 150));
bindSort("#page-reports th.sort", "rsort", S.rep, renderReportRows);

const repQuery = () => { const b = $("repBranch").value; return `start=${$("repStart").value}&end=${$("repEnd").value}${b ? "&branch=" + encodeURIComponent(b) : ""}`; };
on("repExportDetail", "click", () => { if (!$("repStart").value || !$("repEnd").value) return toast("اختر فترة التقرير.", "err"); download(`/api/reports/export.csv?${repQuery()}`, `bankai_attendance_${$("repStart").value}_${$("repEnd").value}.csv`).catch(e => toast(e.message, "err")); });
on("repExportSummary", "click", () => { if (!$("repStart").value || !$("repEnd").value) return toast("اختر فترة التقرير.", "err"); download(`/api/reports/employees.csv?${repQuery()}`, `bankai_employees_summary_${$("repStart").value}_${$("repEnd").value}.csv`).catch(e => toast(e.message, "err")); });
on("repPrint", "click", () => { $("repDaysBox").open = true; window.print(); });

async function runReport() {
  const start = $("repStart").value, end = $("repEnd").value;
  if (!start || !end) return;
  try {
    const [sum, emps] = await Promise.all([api(`/api/reports/summary?${repQuery()}`), api(`/api/reports/employees?${repQuery()}`)]);
    S.rep.sum = sum; S.rep.emps = emps.employees; S.rep.page = 1;
    renderReport();
  } catch (e) { toast(e.message, "err"); }
}

function renderReport() {
  const r = S.rep.sum, branch = $("repBranch").value;
  $("printHeader").textContent = `من ${fmtDay(r.start, { day: "numeric", month: "long", year: "numeric" })} إلى ${fmtDay(r.end, { day: "numeric", month: "long", year: "numeric" })}${branch ? " — " + branch : " — كل الفروع"}`;
  const k = [["إجمالي التسجيلات", r.total, "brand"], ["حضور", r.present, "ok"], ["تأخير", r.late, "warn"], ["غياب", r.absent, "bad"], ["بعذر", r.excused, "info"], ["إجمالي الغرامات", money(r.fine_total), ""]];
  $("repKpis").innerHTML = k.map(([l, v, c]) => `<div class="kpi ${c}"><span>${l}</span><strong class="${typeof v === "number" ? "num" : ""}" style="${typeof v === "number" ? "" : "font-size:22px"}">${v}</strong></div>`).join("");
  const names = Object.keys(r.by_branch).sort();
  $("repBranches").innerHTML = names.length ? names.map(n => {
    const b = r.by_branch[n], c = { scheduled: b.total, present: b.present, late: b.late, absent: b.absent, excused: b.excused, unmarked: 0 };
    return `<div class="panel br-card"><h4><span>${esc(n)}</span><span class="muted num">${b.total} تسجيل</span></h4>${stackBar(c, "lg")}<div style="margin:10px 0">${legend({ ...c, unmarked: undefined })}</div><div class="bp-fine">الغرامات: <b>${money(b.fine_total)}</b></div></div>`;
  }).join("") : "";
  $("repDays").innerHTML = r.by_day.map(d => `<tr data-go="${esc(JSON.stringify({ date: d.date, branch: $("repBranch").value }))}" style="cursor:pointer"><td>${fmtDay(d.date, { weekday: "long", day: "numeric", month: "short" })}</td><td class="num">${d.present}</td><td class="num">${d.late}</td><td class="num">${d.absent}</td><td class="num">${d.excused}</td><td class="num">${d.fine_total ? num(d.fine_total) : "—"}</td><td style="width:140px">${stackBar({ scheduled: d.total, present: d.present, late: d.late, absent: d.absent, excused: d.excused, unmarked: 0 })}</td></tr>`).join("");
  renderReportRows();
}

function renderReportRows() {
  const q = $("repSearch").value.trim().toLowerCase();
  const list = S.rep.emps.filter(e => !q || `${e.name} ${e.employee_code}`.toLowerCase().includes(q));
  sortRows(list, S.rep, (e, k) => e[k] ?? "");
  markSort("#page-reports th.sort", "rsort", S.rep);
  const total = list.length, pages = Math.max(1, Math.ceil(total / S.rep.size));
  S.rep.page = Math.min(S.rep.page, pages);
  const slice = list.slice((S.rep.page - 1) * S.rep.size, S.rep.page * S.rep.size);
  $("repEmpSub").textContent = total ? `${total} موظف لهم تسجيلات في الفترة` : "";
  $("repRows").innerHTML = slice.map(e => {
    const cls = e.rate < 75 ? "low" : e.rate < 90 ? "mid" : "";
    return `<tr data-hist="${e.id}" style="cursor:pointer"><td class="who"><b>${esc(e.name)}</b><small>${esc(e.employee_code || "بدون كود")} · ${sh(e.shift)}</small></td><td>${esc(e.branch)}</td><td class="num">${e.present}</td><td class="num">${e.late}</td><td class="num">${e.absent}</td><td class="num">${e.excused}</td><td><span class="rate"><b class="num">${e.rate}%</b><span class="stack"><i class="${cls}" style="width:${e.rate}%"></i></span></span></td><td class="num">${e.fine_total ? num(e.fine_total) : "—"}</td></tr>`;
  }).join("");
  $("repEmpty").classList.toggle("hidden", total > 0);
  renderPager($("repPager"), total, S.rep, renderReportRows);
}


/* ============================== schedule ============================== */
const JS_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
S.sch = { branch: "QCD 1", data: null, base: new Map(), draft: new Map(), q: "", defaults: {}, built: false };
const schToday = () => JS_DAYS[new Date().getDay()];
const schEmp = e => { const d = S.sch.draft.get(e.id); return d ? { ...e, ...d } : e; };
const findCard = name => qsa(".shcard", $("schShifts")).find(c => c.dataset.shift === name);

function loadSchedulePage() {
  if (!S.sch.built) {
    S.sch.built = true;
    $("autoDays").innerHTML = WEEKDAYS.map(d => `<label><input type="checkbox" value="${d}" checked>${WD_AR[d]}</label>`).join("");
  }
  loadSchedule();
}
async function loadSchedule(keepDraft = false) {
  try {
    const r = await api(`/api/schedule?branch=${encodeURIComponent(S.sch.branch)}`);
    S.sch.data = r; S.sch.base = new Map(r.employees.map(e => [e.id, e]));
    if (!keepDraft) S.sch.draft.clear();
    else for (const id of [...S.sch.draft.keys()]) if (!S.sch.base.has(id)) S.sch.draft.delete(id);
    renderSchedule(); updateSchSaveBar();
  } catch (e) { toast(e.message, "err"); }
}
function schSegCounts() {
  const n = b => S.employees.filter(e => e.active && e.branch === b).length;
  qsa("#schBranchSeg button").forEach(b => { b.classList.toggle("on", b.dataset.branch === S.sch.branch); qs("small", b).textContent = n(b.dataset.branch); });
}
function schGroups() {
  const D = S.sch.data, g = new Map(D.shifts.map(s => [s, []]));
  D.employees.map(schEmp).forEach(e => { if (!g.has(e.shift)) g.set(e.shift, []); g.get(e.shift).push(e); });
  return g;
}
function schCover(list) {
  const c = {}; WEEKDAYS.forEach(d => c[d] = list.length);
  list.forEach(e => { if (c[e.day_off] !== undefined) c[e.day_off]--; });
  return c;
}
function schSpread(list) { const v = Object.values(schCover(list)); return Math.max(...v) - Math.min(...v); }
function parseShiftJS(name) {
  const s = String(name || "").replace(/[٠-٩]/g, d => "٠١٢٣٤٥٦٧٨٩".indexOf(d)), re = /(\d{1,2})(?::(\d{2}))?\s*(am|pm|ص|م)?/gi, t = [];
  let m;
  while ((m = re.exec(s))) {
    let h = +m[1]; const mi = +(m[2] || 0), ap = (m[3] || "").toLowerCase();
    if (mi > 59) continue;
    if (ap === "am" || ap === "ص") { if (h < 1 || h > 12) continue; h = h === 12 ? 0 : h; }
    else if (ap === "pm" || ap === "م") { if (h < 1 || h > 12) continue; h = h === 12 ? 12 : h + 12; }
    else { if (h > 24) continue; h %= 24; }
    t.push(h * 60 + mi);
  }
  if (t.length < 2) return null;
  return [t[0], t[1] <= t[0] ? t[1] + 1440 : t[1]];
}
const hhmmOf = m => { m = ((m % 1440) + 1440) % 1440; return `${pad(Math.floor(m / 60))}:${pad(m % 60)}`; };
const fmt12hm = v => { const [h, m] = v.split(":").map(Number), hh = h % 12 || 12; return `${hh}:${pad(m)} ${h >= 12 ? "م" : "ص"}`; };
function schBreakOptions(shift, current) {
  const p = parseShiftJS(shift), vals = [];
  if (p) for (let t = p[0]; t <= p[1]; t += 30) vals.push(hhmmOf(t));
  else for (let t = 0; t < 1440; t += 30) vals.push(hhmmOf(t));
  const all = [...new Set(vals)];
  if (current && !all.includes(current)) { all.push(current); all.sort(); }
  return `<option value="">بدون بريك</option>` + all.map(v => `<option value="${v}" ${v === current ? "selected" : ""}>${fmt12hm(v)}</option>`).join("");
}

function schRow(e, all) {
  const dirty = S.sch.draft.get(e.id) || {};
  if (!isAdmin()) {
    return `<tr data-id="${e.id}">
      <td class="who"><b>${esc(e.name)}</b><small>${esc(e.employee_code || "")}</small></td>
      <td class="c"><span class="tag ${e.employment_type === "FT" ? "brand" : ""}">${e.employment_type}</span></td>
      <td>${WD_AR[e.day_off] || e.day_off}${e.off_locked ? ' <span class="tag" title="يوم الإجازة مثبّت">مثبّت</span>' : ""}</td>
      <td>${e.break_start ? `<span class="num">${fmt12hm(e.break_start)}</span>` : `<span class="muted">—</span>`}</td>
      <td class="acts no-print"></td></tr>`;
  }
  const moves = all.filter(s => s !== e.shift).map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  return `<tr data-id="${e.id}" class="${S.sch.draft.has(e.id) ? "dirty" : ""}">
    <td class="who"><b>${esc(e.name)}</b><small>${esc(e.employee_code || "")}</small></td>
    <td class="c"><span class="tag ${e.employment_type === "FT" ? "brand" : ""}">${e.employment_type}</span></td>
    <td><select class="sel f-off${dirty.day_off ? " chg" : ""}" aria-label="يوم الإجازة">${WEEKDAYS.map(d => `<option value="${d}" ${d === e.day_off ? "selected" : ""}>${WD_AR[d]}</option>`).join("")}</select></td>
    <td><select class="sel f-bs${"break_start" in dirty ? " chg" : ""}" aria-label="ساعة البريك">${schBreakOptions(e.shift, e.break_start || "")}</select></td>
    <td class="acts no-print"><button type="button" class="btn sm icon pin${e.off_locked ? " on" : ""}" data-pin title="${e.off_locked ? "يوم الإجازة مثبّت (التوزيع التلقائي مش هيغيّره). اضغط للإلغاء" : "ثبّت يوم الإجازة عشان التوزيع التلقائي ما يغيّروش"}" aria-pressed="${!!e.off_locked}"><svg class="i"><use href="#i-pin"/></svg></button><select class="sel tiny f-mv" aria-label="نقل لشيفت آخر"><option value="">نقل…</option>${moves}</select><button type="button" class="btn sm icon danger" data-del title="إزالة من الجدول" aria-label="إزالة ${esc(e.name)}"><svg class="i"><use href="#i-x"/></svg></button></td></tr>`;
}
function schHead(name, list) {
  const sp = schSpread(list);
  return `<div class="shtop"><div><h4><bdi dir="ltr">${esc(name)}</bdi><span class="tag">${list.length} موظف</span>${list.length ? `<span class="tag ${sp <= 1 ? "ok" : "warn"}">${sp <= 1 ? "الإجازات موزّعة بالتساوي" : `فرق ${sp} بين أقل وأكثر يوم`}</span>` : ""}</h4></div>
    ${isAdmin() ? `<div class="row no-print"><button class="btn sm" data-sh="paste">لصق أسماء</button><button class="btn sm" data-sh="auto">وزّع هذا الشيفت</button><button class="btn sm" data-sh="rename">تعديل الاسم</button>${list.length ? "" : `<button class="btn sm danger" data-sh="delete">حذف الشيفت</button>`}</div>` : ""}</div>`;
}
function schTable(name, list, all) {
  const q = S.sch.q.trim().toLowerCase();
  const shown = q ? list.filter(e => `${e.name} ${e.employee_code}`.toLowerCase().includes(q)) : list;
  if (!list.length) return `<div class="shempty">الشيفت فاضي. اكتب اسم موظف تحت واضغط Enter.</div>`;
  return `<table class="sch"><thead><tr><th>الموظف</th><th>النوع</th><th>يوم الإجازة</th><th>ساعة البريك</th><th class="no-print"></th></tr></thead>
    <tbody>${shown.map(e => schRow(e, all)).join("") || `<tr><td colspan="5" class="shempty">لا نتائج مطابقة في هذا الشيفت.</td></tr>`}</tbody></table>`;
}
function schCard(name, list, all) {
  const q = S.sch.q.trim();
  if (q && !list.some(e => `${e.name} ${e.employee_code}`.toLowerCase().includes(q.toLowerCase()))) return "";
  const def = S.sch.defaults[name] || { type: "FT", day: "" };
  return `<section class="shcard" data-shift="${esc(name)}"><header class="shhead">${schHead(name, list)}</header>
    <div class="shwrap">${schTable(name, list, all)}</div>
    <div class="pastebox hidden"><textarea class="inp paste-ta" placeholder="اسم في كل سطر. ممكن تلصق عمود من Excel. ولو كتبت PT بعد الاسم (تاب أو فاصلة) بيتسجل بارت تايم."></textarea><div class="row" style="margin-top:8px"><button class="btn primary sm paste-go">إضافة كل الأسماء</button><button class="btn sm paste-cancel">إلغاء</button></div></div>
    ${isAdmin() ? `<div class="addrow no-print"><input class="inp add-name" list="schNames" placeholder="اكتب اسم موظف واضغط Enter لإضافته" autocomplete="off" aria-label="اسم موظف جديد">
      <select class="sel add-type" aria-label="النوع"><option value="FT" ${def.type === "FT" ? "selected" : ""}>Full Time</option><option value="PT" ${def.type === "PT" ? "selected" : ""}>Part Time</option></select>
      <select class="sel add-day" aria-label="يوم الإجازة"><option value="">إجازة: تلقائي</option>${WEEKDAYS.map(d => `<option value="${d}" ${def.day === d ? "selected" : ""}>${WD_AR[d]}</option>`).join("")}</select>
      <button class="btn primary add-go"><svg class="i"><use href="#i-plus"/></svg>إضافة</button></div>` : ""}</section>`;
}
function renderTotals(emps) {
  const total = emps.length, off = {}; WEEKDAYS.forEach(d => off[d] = 0);
  emps.forEach(e => { if (off[e.day_off] !== undefined) off[e.day_off]++; });
  const today = schToday();
  $("schTotals").innerHTML = WEEKDAYS.map(d => `<div class="dt${d === today ? " today" : ""}"><small>${WD_AR[d]}</small><strong class="num">${total - off[d]}</strong><span>إجازة: ${off[d]}</span></div>`).join("");
}
function renderSchedule() {
  const D = S.sch.data; if (!D) return;
  schSegCounts();
  const groups = schGroups(), names = [...groups.keys()], emps = D.employees.map(schEmp);
  $("schNames").innerHTML = emps.map(e => `<option value="${esc(e.name)}">${esc(e.shift)}</option>`).join("");
  const scope = $("autoScope").value;
  $("autoScope").innerHTML = `<option value="">كل شيفتات الفرع</option>` + names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  $("autoScope").value = names.includes(scope) ? scope : "";
  $("schShifts").innerHTML = names.length ? names.map(n => schCard(n, groups.get(n), names)).join("") : `<div class="empty"><b>مفيش شيفتات في ${esc(S.sch.branch)} لسه</b>أضف أول شيفت من القسم اللي تحت، وبعدها ابدأ إضافة الموظفين.</div>`;
  renderTotals(emps);
  $("schPrintTitle").textContent = `جدول الشيفتات — ${S.sch.branch}`;
  $("schPrintSub").textContent = new Date().toLocaleDateString("ar-EG-u-nu-latn", { day: "numeric", month: "long", year: "numeric" });
}
function schRefreshShift(name) {
  const groups = schGroups(), list = groups.get(name), all = [...groups.keys()], card = findCard(name);
  if (!card || !list) { renderSchedule(); return; }
  qs(".shhead", card).innerHTML = schHead(name, list);
  qs(".shwrap", card).innerHTML = schTable(name, list, all);
  renderTotals(S.sch.data.employees.map(schEmp));
}

/* draft handling */
function schSet(id, patch) {
  const base = S.sch.base.get(id); if (!base) return;
  const d = { ...(S.sch.draft.get(id) || {}), ...patch };
  Object.keys(d).forEach(k => { if (String(d[k] ?? "") === String(base[k] ?? "")) delete d[k]; });
  Object.keys(d).length ? S.sch.draft.set(id, d) : S.sch.draft.delete(id);
}
function updateSchSaveBar() {
  const n = S.sch.draft.size;
  $("schSaveBar").classList.toggle("hidden", n === 0);
  $("schSaveMsg").textContent = `${n} موظف عليهم تعديلات لم تُحفظ بعد`;
}
async function schSave(silent = false) {
  if (!S.sch.draft.size) return true;
  const items = [...S.sch.draft].map(([id, d]) => {
    const o = { id };
    if (d.day_off) o.day_off = d.day_off;
    if ("break_start" in d) o.break_start = d.break_start || "";
    if ("break_min" in d) o.break_min = Number(d.break_min);
    if ("off_locked" in d) o.off_locked = !!d.off_locked;
    if (d.shift) o.shift = d.shift;
    return o;
  });
  $("schSave").disabled = true;
  try {
    const r = await api("/api/schedule/apply", { method: "POST", body: { items } });
    await loadSchedule();
    refreshEmployees().then(schSegCounts).catch(() => {});
    if (r.errors.length) { toast(`تعذر حفظ ${r.errors.length}: ${r.errors[0].name ? r.errors[0].name + " — " : ""}${r.errors[0].detail}`, "err"); return false; }
    if (!silent) toast(`تم حفظ الجدول (${r.updated} موظف).`, "ok");
    return true;
  } catch (e) { toast(e.message, "err"); return false; }
  finally { $("schSave").disabled = false; }
}
on("schSave", "click", () => schSave());
on("schDiscard", "click", () => { if (!S.sch.draft.size) return; S.sch.draft.clear(); renderSchedule(); updateSchSaveBar(); });

/* toolbar */
on("schBranchSeg", "click", ev => {
  const b = ev.target.closest("button"); if (!b || b.dataset.branch === S.sch.branch) return;
  if (S.sch.draft.size && !confirm("عندك تغييرات لم تُحفظ في هذا الفرع. تجاهلها والانتقال؟")) return;
  S.sch.branch = b.dataset.branch; S.sch.draft.clear(); updateSchSaveBar(); loadSchedule();
});
on("schSearch", "input", debounce(() => { S.sch.q = $("schSearch").value; renderSchedule(); }, 150));

// ---------- schedule: import from Excel ----------
const SIMP_F = { shift: "الشيفت", employment_type: "النوع", day_off: "الإجازة", break_start: "البريك", branch: "الفرع", active: "الحالة", off_locked: "تثبيت الإجازة" };
const SIMP_ST = { create: ["جديد", "ok"], update: ["تعديل", "brand"], same: ["بدون تغيير", "off"], review: ["مراجعة", "warn"], skip: ["متخطّى", "bad"] };
const SIMP_RS = { similar: "اسم قريب من موظف موجود", other_branch: "الاسم موجود في فرع تاني", ambiguous: "أكتر من موظف بنفس الاسم" };
S.simp = { b64: "", name: "", plan: null, decisions: {}, filter: "", deact: false, ack: false };

function simpFmt(f, v) { return f === "day_off" ? (WD_AR[v] || v) : f === "employment_type" ? (v === "FT" ? "فول تايم" : "بارت تايم") : v; }
async function simpRun(apply) {
  const m = S.simp;
  return api("/api/schedule/import", { method: "POST", body: { branch: S.sch.branch, filename: m.name, file_b64: m.b64, decisions: m.decisions, deactivate_missing: m.deact, apply } });
}
function simpRender() {
  const m = S.simp, p = m.plan; if (!p) return;
  const s = p.summary;
  $("schImpMismatch").classList.toggle("hidden", !p.branch_mismatch);
  if (p.branch_mismatch) $("schImpMismatch").innerHTML = `الملف باين إنه لفرع <b>${esc(p.branch_hint)}</b> لكن انت واقف على <b>${esc(p.branch)}</b>.<label><input type="checkbox" id="simpAck" ${m.ack ? "checked" : ""}> متأكد، استورد في ${esc(p.branch)}</label>`;
  const notes = (p.notes || []).map(n => `<div class="imp-note"><svg class="i"><use href="#i-info"/></svg>${esc(n)}</div>`).join("");
  const chips = [["", "الكل", p.items.length], ["create", "جديد", s.create], ["update", "تعديل", s.update], ["same", "بدون تغيير", s.same], ["review", "مراجعة", s.review], ["skip", "متخطّى", s.skip]]
    .filter(([k, , n]) => !k || n);
  const rows = p.items.filter(i => !m.filter || i.status === m.filter);
  const tr = rows.slice(0, 500).map(i => {
    const [lbl, kind] = SIMP_ST[i.status];
    let det = "";
    if (i.status === "skip") det = `<div class="chg">${esc(i.reason)}</div>`;
    if (i.status === "review") {
      const opts = [["skip", "تخطّى"], ["new", "ضيفه كموظف جديد"], ...i.candidates.map(c => [String(c.id), `هو نفسه: ${c.name}${c.branch !== p.branch ? " (" + c.branch + ")" : ""}${c.score ? " · " + Math.round(c.score * 100) + "%" : ""}`])];
      det = `<div class="chg">${SIMP_RS[i.reason] || ""}</div><select class="sel" data-dec="${esc(i.key)}">${opts.map(([v, t]) => `<option value="${esc(v)}" ${i.decision === v ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>`;
    }
    if (i.changes.length && i.action === "update") det += `<div class="chg">${i.changes.filter(c => c.field !== "off_locked").map(c => `${SIMP_F[c.field]}: ${esc(simpFmt(c.field, c.from) || "—")} ← ${esc(simpFmt(c.field, c.to))}`).join(" · ")}</div>`;
    if (i.warnings.length && i.status !== "skip") det += `<div class="wn">${i.warnings.map(esc).join(" · ")}</div>`;
    return `<tr><td class="rn num">${i.row}</td><td><b>${esc(i.name || "—")}</b></td><td>${i.type}</td><td>${i.shift ? `<bdi dir="ltr">${esc(i.shift)}</bdi>${i.shift_new ? ' <span class="tag brand">جديد</span>' : ""}` : "—"}</td><td>${i.break ? `<span class="num">${i.break}</span>` : "—"}</td><td>${i.day_off ? WD_AR[i.day_off] : "—"}</td><td><span class="tag ${kind}">${lbl}</span>${det}</td></tr>`;
  }).join("");
  $("schImpBody").innerHTML = `
    ${notes}<div class="simp-chips">${chips.map(([k, t, n]) => `<button type="button" data-f="${k}" class="${m.filter === k ? "on" : ""}">${t} <b class="num">${n}</b></button>`).join("")}</div>
    <div class="simp-shifts"><b>الشيفتات:</b> ${p.shifts.map(x => `<span class="tag ${x.new ? "brand" : ""}"><bdi dir="ltr">${esc(x.name)}</bdi> · ${x.count}</span>`).join("")}</div>
    ${p.missing.length ? `<div class="simp-opt"><label class="check"><input type="checkbox" id="simpDeact" ${m.deact ? "checked" : ""}> أوقف الموظفين اللي موجودين في السيستم ومش في الشيت (${p.missing.length})</label><details><summary>عرض الأسماء</summary>${p.missing.map(x => esc(x.name)).join("، ")}</details></div>` : ""}
    <div class="simp-tbl"><table><thead><tr><th>سطر</th><th>الاسم</th><th>النوع</th><th>الشيفت</th><th>البريك</th><th>الإجازة</th><th>الحالة</th></tr></thead><tbody>${tr}</tbody></table></div>
    ${rows.length > 500 ? `<p class="muted" style="margin-top:6px">معروض أول 500 صف فقط (الاستيراد بيشمل الكل).</p>` : ""}`;
  const todo = s.create + s.update + s.review - p.items.filter(i => i.status === "review" && i.decision === "skip").length;
  const run = $("schImpRun");
  run.disabled = !(todo > 0 || m.deact) || (p.branch_mismatch && !m.ack);
  run.textContent = todo > 0 ? `استورد ${todo} موظف${s.same ? ` (و${s.same} بدون تغيير)` : ""}` : "مفيش حاجة تتغيّر";
}
async function simpLoad(file) {
  const m = S.simp;
  if (!/\.xlsx$/i.test(file.name)) { toast("الملف لازم يكون Excel بصيغة .xlsx", "err"); return; }
  if (file.size > 4_000_000) { toast("الملف كبير جدًا", "err"); return; }
  const b64 = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1] || ""); r.onerror = rej; r.readAsDataURL(file); });
  Object.assign(m, { b64, name: file.name, plan: null, decisions: {}, filter: "", deact: false, ack: false });
  $("schImpName").textContent = file.name; $("schImpBody").innerHTML = '<p class="muted">جاري القراءة…</p>'; $("schImpRun").disabled = true;
  try { m.plan = await simpRun(false); simpRender(); }
  catch (e) { $("schImpBody").innerHTML = `<div class="empty"><b>${esc(e.message)}</b></div>`; }
}
on("schImport", "click", () => {
  if (S.sch.draft.size && !confirm("عندك تغييرات لم تُحفظ في الجدول، والاستيراد هيتجاهلها. تكمّل؟")) return;
  Object.assign(S.simp, { b64: "", name: "", plan: null, decisions: {}, filter: "", deact: false, ack: false });
  $("schImpName").textContent = ""; $("schImpFile").value = ""; $("schImpMismatch").classList.add("hidden");
  $("schImpBody").innerHTML = `<div class="empty"><b>اختار ملف الشيت</b>الأعمدة المتوقعة: FULL TIME / SHIFT / BREAK / DAY OFF ثم PART TIME / SHIFT / DAY OFF. الاستيراد هيكون على فرع <b>${esc(S.sch.branch)}</b>.</div>`;
  $("schImpRun").disabled = true; $("schImpRun").textContent = "استيراد";
  $("schImpDialog").showModal();
});
on("schImpFile", "change", e => { const f = e.target.files[0]; if (f) simpLoad(f); });
on("schImpClose", "click", () => $("schImpDialog").close());
on("schImpCancel", "click", () => $("schImpDialog").close());
on("schImpBody", "click", e => { const b = e.target.closest("[data-f]"); if (b) { S.simp.filter = b.dataset.f; simpRender(); } });
on("schImpBody", "change", async e => {
  const m = S.simp;
  if (e.target.id === "simpDeact") { m.deact = e.target.checked; simpRender(); return; }
  if (e.target.dataset.dec) {
    m.decisions[e.target.dataset.dec] = e.target.value;
    try { m.plan = await simpRun(false); simpRender(); } catch (er) { toast(er.message, "err"); }
  }
});
on("schImpMismatch", "change", e => { if (e.target.id === "simpAck") { S.simp.ack = e.target.checked; simpRender(); } });
on("schImpRun", "click", async () => {
  const m = S.simp, btn = $("schImpRun"); btn.disabled = true; btn.textContent = "جاري الاستيراد…";
  try {
    const r = await simpRun(true), a = r.applied;
    $("schImpDialog").close();
    toast(`تم الاستيراد: ${a.created} جديد، ${a.updated} تعديل${a.same ? "، " + a.same + " بدون تغيير" : ""}${a.deactivated ? "، " + a.deactivated + " موقوف" : ""} — اتأكدت من ${a.verified} موظف`);
    await refreshEmployees(); S.sch.draft.clear(); await loadSchedule();
  } catch (e) { toast(e.message, "err"); simpRender(); }
});

on("schExport", "click", () => download(`/api/schedule/export.csv?branch=${encodeURIComponent(S.sch.branch)}`, `bankai_schedule_${S.sch.branch.replace(" ", "")}.csv`).catch(e => toast(e.message, "err")));
on("schPrint", "click", () => window.print());

/* one-click day off, pin, move, remove, break edits (event delegation) */
on("schShifts", "click", async ev => {
  const t = ev.target, card = t.closest(".shcard"); if (!card) return;
  const name = card.dataset.shift, tr = t.closest("tr[data-id]");
  const day = t.closest("[data-d]");
  if (day && tr) { schSet(+tr.dataset.id, { day_off: day.dataset.d, off_locked: true }); schRefreshShift(name); updateSchSaveBar(); return; }
  if (t.closest("[data-pin]") && tr) {
    const id = +tr.dataset.id, e = schEmp(S.sch.base.get(id));
    schSet(id, { off_locked: !e.off_locked }); schRefreshShift(name); updateSchSaveBar(); return;
  }
  if (t.closest("[data-del]") && tr) {
    const id = +tr.dataset.id, e = S.sch.base.get(id);
    if (!confirm(`إزالة «${e.name}» من الجدول؟ هيتوقف مع الاحتفاظ بسجل حضوره، وتقدر تفعّله من صفحة الموظفين.`)) return;
    try {
      await api(`/api/employees/${id}`, { method: "DELETE" });
      S.sch.data.employees = S.sch.data.employees.filter(x => x.id !== id); S.sch.base.delete(id); S.sch.draft.delete(id);
      renderSchedule(); updateSchSaveBar(); toast(`تمت إزالة ${e.name}.`, "ok");
      refreshEmployees().then(schSegCounts).catch(() => {});
    } catch (err) { toast(err.message, "err"); }
    return;
  }
  if (t.closest(".add-go")) { schAddFromRow(card); return; }
  const sh = t.closest("[data-sh]");
  if (sh) { schShiftAction(sh.dataset.sh, name, card); return; }
  if (t.closest(".paste-cancel")) { qs(".pastebox", card).classList.add("hidden"); return; }
  if (t.closest(".paste-go")) { schPasteAdd(card); }
});
on("schShifts", "change", ev => {
  const t = ev.target, card = t.closest(".shcard"); if (!card) return;
  const name = card.dataset.shift, tr = t.closest("tr[data-id]");
  if (t.classList.contains("add-type") || t.classList.contains("add-day")) { S.sch.defaults[name] = { type: qs(".add-type", card).value, day: qs(".add-day", card).value }; return; }
  if (!tr) return;
  const id = +tr.dataset.id;
  if (t.classList.contains("f-off")) { schSet(id, { day_off: t.value, off_locked: true }); schRefreshShift(name); }
  else if (t.classList.contains("f-bs")) { schSet(id, { break_start: t.value, break_min: Number(S.sch.base.get(id).break_min) || 60 }); schRefreshShift(name); }
  else if (t.classList.contains("f-bm")) { schSet(id, { break_min: Number(t.value) }); schRefreshShift(name); }
  else if (t.classList.contains("f-mv") && t.value) { schSet(id, { shift: t.value }); renderSchedule(); }
  updateSchSaveBar();
});
on("schShifts", "keydown", ev => {
  if (ev.key === "Enter" && ev.target.classList.contains("add-name")) { ev.preventDefault(); schAddFromRow(ev.target.closest(".shcard")); }
});

/* add people inline: type name + Enter, the field stays focused for the next one */
function schUpsert(emp) {
  const arr = S.sch.data.employees, i = arr.findIndex(x => x.id === emp.id);
  if (i >= 0) arr[i] = emp; else arr.push(emp);
  S.sch.base.set(emp.id, emp); S.sch.draft.delete(emp.id);
  arr.sort((a, b) => a.name.localeCompare(b.name, "ar"));
}
async function schAdd(card, items, restore) {
  const shift = card.dataset.shift;
  try {
    const r = await api("/api/schedule/add", { method: "POST", body: { branch: S.sch.branch, shift, items } });
    r.employees.forEach(schUpsert);
    renderSchedule(); updateSchSaveBar();
    const nc = findCard(shift); if (nc) { const f = qs(".add-name", nc); f.focus(); nc.querySelector(".addrow").scrollIntoView({ block: "nearest" }); }
    const same = r.notes.filter(n => n.type === "same").length, moved = r.notes.filter(n => n.type === "moved"), react = r.notes.filter(n => n.type === "reactivated").length;
    const bits = [];
    if (r.created) bits.push(r.created === 1 ? `تمت إضافة ${r.notes.find(n => n.type === "created").name}` : `تمت إضافة ${r.created} موظف`);
    if (moved.length) bits.push(moved.length === 1 ? `تم نقل ${moved[0].name} من «${moved[0].from}»` : `تم نقل ${moved.length} موظف من شيفتات أخرى`);
    if (react) bits.push(`تم تفعيل ${react} موظف موقوف`);
    if (same) bits.push(`${same} موجود بالفعل في الشيفت`);
    toast(bits.join(" · ") || "لا تغييرات", same && !r.created && !moved.length && !react ? "" : "ok");
    refreshEmployees().then(schSegCounts).catch(() => {});
  } catch (e) { toast(e.message, "err"); if (restore) restore(); }
}
function schAddFromRow(card) {
  const input = qs(".add-name", card), name = input.value.trim();
  if (!name) { input.focus(); return; }
  const type = qs(".add-type", card).value, day = qs(".add-day", card).value;
  S.sch.defaults[card.dataset.shift] = { type, day };
  input.value = "";
  schAdd(card, [{ name, employment_type: type, day_off: day || null }], () => { input.value = name; input.focus(); });
}
function schPasteAdd(card) {
  const ta = qs(".paste-ta", card), type = qs(".add-type", card).value, day = qs(".add-day", card).value;
  const items = ta.value.split(/\r?\n/).map(l => l.split(/\t|,|،|;/).map(x => x.trim())).filter(c => c[0])
    .map(c => ({ name: c[0], employment_type: /^(pt|part)/i.test(c[1] || "") || /جزئي/.test(c[1] || "") ? "PT" : /^(ft|full)/i.test(c[1] || "") ? "FT" : type, day_off: day || null }));
  if (!items.length) { toast("الصق اسم واحد على الأقل.", "err"); return; }
  schAdd(card, items, null).then(() => { const nc = findCard(card.dataset.shift); if (nc) qs(".pastebox", nc).classList.add("hidden"); });
}

/* shift header actions */
async function schShiftAction(act, name, card) {
  if (act === "paste") { const pb = qs(".pastebox", card); pb.classList.toggle("hidden"); if (!pb.classList.contains("hidden")) qs(".paste-ta", card).focus(); return; }
  if (act === "auto") { openAuto(name); return; }
  if (act === "rename") {
    const nn = (prompt("اسم الشيفت الجديد (مثال: 8 AM - 4 PM)", name) || "").trim();
    if (!nn || nn === name) return;
    try {
      await api("/api/schedule/shifts/rename", { method: "POST", body: { branch: S.sch.branch, old: name, new: nn } });
      await loadSchedule(true); refreshEmployees().catch(() => {}); toast("تم تعديل اسم الشيفت.", "ok");
    } catch (e) { toast(e.message, "err"); }
    return;
  }
  if (act === "delete") {
    if (!confirm(`حذف الشيفت «${name}»؟`)) return;
    try { await api(`/api/schedule/shifts?branch=${encodeURIComponent(S.sch.branch)}&name=${encodeURIComponent(name)}`, { method: "DELETE" }); await loadSchedule(true); toast("تم حذف الشيفت.", "ok"); }
    catch (e) { toast(e.message, "err"); }
  }
}
const fmt12 = t => { const [h, m] = t.split(":").map(Number), hh = h % 12 || 12; return `${hh}${m ? ":" + String(m).padStart(2, "0") : ""} ${h >= 12 ? "PM" : "AM"}`; };
on("nsAdd", "click", async () => {
  const from = $("nsFrom").value, to = $("nsTo").value, custom = $("nsName").value.trim();
  const name = custom || (from && to ? `${fmt12(from)} - ${fmt12(to)}` : "");
  if (!name) { toast("اختر وقت البداية والنهاية أو اكتب اسم الشيفت.", "err"); return; }
  try {
    await api("/api/schedule/shifts", { method: "POST", body: { branch: S.sch.branch, name } });
    $("nsName").value = ""; await loadSchedule(true); toast(`تم إضافة الشيفت ${name}. اكتب أسماء الموظفين فيه.`, "ok");
    const c = findCard(name); if (c) { c.scrollIntoView({ block: "center", behavior: "smooth" }); qs(".add-name", c).focus({ preventScroll: true }); }
  } catch (e) { toast(e.message, "err"); }
});

/* auto distribution */
function openAuto(scope) {
  $("schAutoPanel").classList.remove("hidden");
  $("autoScope").value = scope || "";
  $("autoHint").textContent = "";
  $("schAutoPanel").scrollIntoView({ block: "nearest", behavior: "smooth" });
}
on("schAutoBtn", "click", () => $("schAutoPanel").classList.contains("hidden") ? openAuto("") : $("schAutoPanel").classList.add("hidden"));
on("schAutoClose", "click", () => $("schAutoPanel").classList.add("hidden"));
on("autoRun", "click", async () => {
  const days = qsa("#autoDays input:checked").map(i => i.value);
  if ($("autoOff").checked && !days.length) { toast("اختر يوم واحد على الأقل متاح للإجازة.", "err"); return; }
  if (!$("autoOff").checked && !$("autoBreaks").checked) { toast("اختر توزيع الإجازات أو البريك.", "err"); return; }
  if (S.sch.draft.size && !(await schSave(true))) return;
  const btn = $("autoRun"); btn.disabled = true;
  try {
    const r = await api("/api/schedule/auto", { method: "POST", body: {
      branch: S.sch.branch, shift: $("autoScope").value || null, include_locked: $("autoLocked").checked, allowed_days: days,
      distribute_off: $("autoOff").checked, distribute_breaks: $("autoBreaks").checked, overwrite_breaks: $("autoOverwrite").checked, break_min: Number($("autoBreakMin").value) } });
    r.changes.forEach(c => {
      const p = {};
      if (c.day_off) p.day_off = c.day_off;
      if (c.break_start) { p.break_start = c.break_start; p.break_min = c.break_min; }
      schSet(c.id, p);
    });
    renderSchedule(); updateSchSaveBar();
    const parts = [];
    if (r.off_changed) parts.push(`${r.off_changed} تغيير في الإجازات`);
    if (r.break_changed) parts.push(`${r.break_changed} في البريك`);
    $("autoHint").innerHTML = (r.changes.length ? `تم اقتراح ${parts.join(" و")}. راجع الجدول (الخانات اللي اتغيّرت بإطار أزرق) وبعدين اضغط «حفظ الجدول».` : (r.employees ? "الجدول متوزّع بالفعل ومفيش حاجة محتاجة تتغيّر." : "مفيش موظفين في النطاق ده.")) + r.warnings.map(w => `<br><span style="color:var(--warn)">${esc(w)}</span>`).join("");
    if (r.changes.length) toast("تم اقتراح التوزيع. راجعه واحفظ.", "ok");
  } catch (e) { toast(e.message, "err"); }
  finally { btn.disabled = false; }
});

/* ============================== settings ============================== */
async function loadSettings() {
  try {
    S.settings = await api("/api/settings");
    $("setFineLate").value = S.settings.fine_late; $("setFineAbsent").value = S.settings.fine_absent;
  } catch (e) { toast(e.message, "err"); }
}
on("setSave", "click", async () => {
  try {
    S.settings = await api("/api/settings", { method: "PUT", body: { fine_late: Number($("setFineLate").value || 0), fine_absent: Number($("setFineAbsent").value || 0) } });
    toast("تم حفظ الإعدادات.", "ok");
  } catch (e) { toast(e.message, "err"); }
});
on("setBackup", "click", async () => {
  try { await download("/api/backup", `bankai_backup_${todayISO()}.db`); toast("تم تنزيل النسخة الاحتياطية.", "ok"); }
  catch (e) { toast(e.message, "err"); }
});
on("setWipeAtt", "click", async () => {
  if (!confirm("هتمسح سجل الحضور بالكامل (كل التسجيلات والغرامات المسجلة). الموظفين والجدول والإعدادات هيفضلوا زي ما هما. متأكد؟")) return;
  const word = prompt("خطوة أخيرة: اكتب كلمة «مسح» عشان تأكّد المسح النهائي.");
  if (word !== "مسح") { if (word !== null) toast("الكلمة غير مطابقة — ما اتمسحش حاجة.", "err"); return; }
  try {
    const r = await api("/api/admin/wipe-attendance", { method: "POST" });
    toast(r.message || "تم مسح سجل الحضور.", "ok");
    if (S.page === "dashboard") loadDashboard();
    else if (S.page === "reports" && $("repStart").value) runReport();
    else if (S.page === "attendance") loadRoster();
  } catch (e) { toast(e.message, "err"); }
});
on("setExportEmp", "click", () => download("/api/employees/export.csv", "bankai_employees.csv").catch(e => toast(e.message, "err")));

/* ============================== boot ============================== */
if (S.token) { showApp(); init(); }


/* ============================== V19 «FOODICS» — motion ==============================
   طبقة حركة عرضية فقط: بتلف دوال العرض الموجودة عشان تضيف العدّاد
   ودخول العناصر ومؤشّر التنقّل. مفيش أي تغيير في منطق البيانات أو
   الـ API أو الـ state — كل الدوال الأصلية بتتنفّذ زي ما هي الأول. */
(function () {
  "use strict";
  var RM = function () { try { return matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { return false; } };
  var qs = function (s, r) { return (r || document).querySelector(s); };
  var qsa = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  /* ---------- 1. مؤشّر التنقّل المنزلق في الشريط الجانبي ---------- */
  var nav = qs(".topnav .tabs"), ind = qs("#navInd");
  var navShown = false, navBusy = false;
  function syncNav() {
    if (!nav || !ind || navBusy) return;
    navBusy = true;
    try {
      var btn = qs(".nav-btn.active", nav);
      if (!btn || !btn.offsetParent) {
        /* ⚠ بنقارن قبل ما نلمس classList: أي كتابة على class — حتى بنفس
           القيمة — بتطلّع MutationRecord، والمراقب تحت بيستدعي syncNav
           تاني، فبندخل في حلقة لا نهائية من غير الشرط ده. */
        if (navShown) { ind.classList.remove("on"); navShown = false; }
        return;
      }
      var nr = nav.getBoundingClientRect(), br = btn.getBoundingClientRect();
      var y = Math.round(br.top - nr.top) + "px", h = Math.round(br.height) + "px";
      if (ind.style.getPropertyValue("--nv-y") !== y) ind.style.setProperty("--nv-y", y);
      if (ind.style.height !== h) ind.style.height = h;
      if (!navShown) { ind.classList.add("on"); navShown = true; }
    } finally { navBusy = false; }
  }
  if (nav) {
    try { new MutationObserver(function () { syncNav(); }).observe(nav, { subtree: true, attributes: true, attributeFilter: ["class", "hidden"] }); } catch (e) { }
    addEventListener("resize", syncNav);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(syncNav).catch(function () { });
    syncNav();
  }

  /* ---------- 2. قراءة الأرقام ----------
     العدّاد التصاعدي (countUp) اتشال: كان بيعمل reset للأرقام على صفر وبياخد
     700ms لحد ما توصل للقيمة الحقيقية، ودي كانت أكبر حاجة بتخلّي الصفحة
     حاسّة إنها «لسه بتتحمّل» حتى بعد ما الداتا تكون وصلت. الأرقام بقت تظهر
     بقيمتها النهائية فورًا، والحركة بقت في دخول الكارت نفسه. */
  function numeric(el) {
    var t = ((el && el.textContent) || "").trim();
    return /^\d{1,7}$/.test(t) ? parseInt(t, 10) : NaN;
  }

  /* ---------- 3. دخول العناصر بتتابع ---------- */
  var RISE = "fdsRise .38s var(--fds-out) both";   /* حركة + شفافية (للكروت والحاويات) */
  var FADE = "fdsFade .26s var(--fds-out) both";   /* شفافية بس (لصفوف الجداول) */
  var enter = null, enterT = 0;
  function markEnter(p) {
    enter = p;
    clearTimeout(enterT);
    enterT = setTimeout(function () { enter = null; }, 1600);
  }
  /* الأداء: بنكتب animation:none على كل العناصر وبعدين بنقرأ offsetWidth مرة
     واحدة بس، عشان المتصفح يعمل layout واحد. قبل كده كان في قراءة جوه اللوب
     (forced reflow لكل عنصر) — 40 صف = 40 layout ورا بعض على الـ main thread.
     وكمان فيه «سقف» للـ delay (cap) عشان الأعمدة الطويلة متبقاش بتاخد ثواني:
     الصف الـ100 مش بيستنى 100 خطوة. */
  function stagger(sel, step, base, max, anim, cap) {
    if (RM() || document.visibilityState !== "visible") return;
    var els = typeof sel === "string" ? qsa(sel) : sel;
    if (max && els.length > max) els = els.slice(0, max);
    if (!els.length) return;
    var a = anim || RISE, lim = cap || 200;
    els.forEach(function (el) { el.style.animation = "none"; });
    void els[0].offsetWidth;
    els.forEach(function (el, i) {
      var d = Math.min(lim, (base || 0) + i * (step || 22));
      el.style.animation = a + " " + d + "ms";
    });
  }
  function enterPage(p) {
    if (RM() || document.visibilityState !== "visible") return;
    var root = qs("#page-" + p);
    if (!root) return;
    /* الحاويات الفاضية بتتسيب من غير حركة: هي دي اللي هتتملي بالداتا بعد شوية,
       فلو حرّكناها هنا بيبقى في حركتين فوق بعض (الحاوية بتطلع والصفوف جواها
       بتتحرك في نفس اللحظة) — وده اللي كان شكله مش نضيف. الصفوف بتتحرك لوحدها.
       DATA_HOSTS = عناصر بتاخد stagger خاص بيها من wrapper العرض نفسه — نسيبها
       من حركة الحاوية عشان مايبقاش في موجتين متزاممتين على نفس المكان. */
    var DATA_HOSTS = { dashKpis: 1, repKpis: 1, attList: 1, schTotals: 1, schShifts: 1 };
    var kids = qsa(":scope > *", root).filter(function (el) {
      if (el.classList.contains("save-bar") || el.classList.contains("hidden") || el.classList.contains("print-only") || el.classList.contains("table-wrap")) return false;
      if (DATA_HOSTS[el.id]) return false;
      return el.textContent.trim().length > 0;
    });
    stagger(kids, 20, 0, 8, RISE, 110);
  }
  function growBars(sel) {
    if (RM()) return;
    qsa(sel).forEach(function (i) {
      var w = i.style.width; if (!w || w === "0%") return;
      i.style.width = "0%";
      var restore = function () { i.style.width = w; };
      requestAnimationFrame(function () { requestAnimationFrame(restore); });
      setTimeout(restore, 140); /* أمان: rAF مابيشتغلش في تاب مخفي */
    });
  }

  /* ---------- 4. هيكل التحميل الأول + لقطة الصفحة عند التبديل ----------
     الهيكل مابيتشالش أول ما صفحة البوت تفتح — بيفضل شغال لحد ما أول داتا
     بتاعة الصفحة توصل (wrapper العرض بيسأل reveal()). الفايدة: المستخدم مايشوفش
     وضاحة فاضية بين الهيكل والداتا، والصفحة بتظهر مكمّلة بالداتا وحركة الدخول
     في نفس اللحظة. صافّي أمان: لو الطلب اتأخر أكتر من 2.5 ثانية بنكشف الصفحة
     زي ما هي بدل ما يقف على الهيكل للأبد. */
  var _showPage = window.showPage;
  var bootSkel = qs("#bootSkel"), bootPage = "", bootT = 0;
  function finishBoot() {
    bootPage = ""; clearTimeout(bootT);
    if (bootSkel && bootSkel.parentNode) { bootSkel.parentNode.removeChild(bootSkel); bootSkel = null; }
  }
  /* بترجّع true لو فعلا الكشف (reveal) حصل هنا — يعني لو الصفحة بتستنى داتا لسه */
  function revealBoot(p) {
    if (bootPage !== p || !bootSkel) return false;
    finishBoot();
    var pg = qs("#page-" + p); if (pg) pg.classList.remove("hidden");
    syncNav();
    return true;
  }
  function syncNavNow() { requestAnimationFrame(syncNav); }
  window.showPage = function (p) {
    if (bootSkel && bootSkel.parentNode) {
      bootPage = p;
      bootT = setTimeout(function () { revealBoot(bootPage); }, 2500); /* أمان: كشف الصفحة لو الداتا اتأخرت جدًا */
      _showPage(p);
      markEnter(p);
      if (bootSkel && bootSkel.parentNode) {
        /* الصفحة لسه فاضية (محتاجة طلب) — نسيب الهيكل شغال ونخفي الصفحة لحد الداتا */
        var pg = qs("#page-" + p); if (pg) pg.classList.add("hidden");
        syncNavNow();
        return;
      }
      /* صفحة بتترندر فورًا (employees/settings) — الهيكل اتشال من جوه _showPage على طول */
    } else { _showPage(p); markEnter(p); }
    requestAnimationFrame(function () { if (!bootPage) enterPage(p); syncNav(); });
  };

  /* ---------- 5. كروت الملخص: شريط + نسبة + عدّاد ---------- */
  var _dash = window.renderDashboard;
  window.renderDashboard = function (d) {
    _dash(d);
    var boot = bootPage === "dashboard" && revealBoot("dashboard"); /* أول داتا وصلت — نبدّل الهيكل بالصفحة الحقيقية */
    var host = qs("#dashKpis"); if (!host) return;
    host.setAttribute("aria-busy", "false");
    host.style.animation = "none"; /* نشيل حركة الحاوية عشان حركة الكروت لوحدها تبان */
    qsa(".kpi", host).forEach(function (card) {
      var kv = qs(".kv", card); if (!kv) return;
      var p = (card.style.getPropertyValue("--p") || "").trim() || "0";
      var f = qs(".kpi-foot", card);
      if (!f) { f = document.createElement("span"); f.className = "kpi-foot"; card.appendChild(f); }
      f.innerHTML = '<span class="kpi-bar"><i style="width:' + p + '%"></i></span><span class="kpi-pct">' + p + "%</span>";
      kv.classList.toggle("is-zero", numeric(kv) === 0 || isNaN(numeric(kv)));
    });
    if (boot || enter === "dashboard") { enter = null; stagger("#dashKpis .kpi", 26, 0, 6, RISE, 130); }
  };

  /* ---------- 6. التقارير ---------- */
  var _rep = window.renderReport;
  window.renderReport = function () {
    _rep.apply(null, arguments);
    var boot = bootPage === "reports" && revealBoot("reports");
    var rh = qs("#repKpis"); if (rh) rh.setAttribute("aria-busy", "false");
    if (rh) rh.style.animation = "none";
    if (boot || enter === "reports") {
      enter = null;
      stagger("#repKpis .kpi", 26, 0, 6, RISE, 130);
      stagger("#repDays tr", 10, 30, 14, FADE, 90);
      stagger("#repRows tr", 9, 40, 26, FADE, 150);
    }
  };

  /* ---------- 7. الحضور اليومي ---------- */
  var _att = window.renderAttendance;
  window.renderAttendance = function () {
    _att.apply(null, arguments);
    var boot = bootPage === "attendance" && revealBoot("attendance");
    if (boot || enter === "attendance") {
      enter = null;
      stagger("#attList .arow", 9, 30, 30, FADE, 150);
      growBars("#attList .ghead .stack i");
    }
  };

  /* ---------- 8. الموظفون ---------- */
  var _emp = window.renderEmployees;
  window.renderEmployees = function () {
    _emp.apply(null, arguments);
    if (bootPage === "employees" && revealBoot("employees")) { enter = null; stagger("#empRows tr", 9, 20, 26, FADE, 140); }
    else if (enter === "employees") { enter = null; stagger("#empRows tr", 9, 20, 26, FADE, 140); }
  };

  /* ---------- 9. الجدول ---------- */
  var _sch = window.renderSchedule;
  window.renderSchedule = function () {
    _sch.apply(null, arguments);
    var boot = bootPage === "schedule" && revealBoot("schedule");
    if (boot || enter === "schedule") {
      enter = null;
      stagger("#schTotals .dt", 22, 20, 8, RISE, 90);
      stagger("#schShifts .shcard", 40, 40, 6, RISE, 140);
    }
  };
  /* صفحة الإعدادات مالهاش render wrapper — أول رد من /api/settings بيكمّل هيكل التحميل */
  var _ls = window.loadSettings;
  window.loadSettings = function () {
    var pr = _ls.apply(null, arguments);
    if (bootPage === "settings") pr.then(function () { revealBoot("settings"); }).catch(function () { });
    return pr;
  };

  /* ---------- 10. شارة المسودّات على شريط التنقّل ---------- */
  var attBtn = qs('.topnav .nav-btn[data-page="attendance"]');
  var schBtn = qs('.topnav .nav-btn[data-page="schedule"]');
  function badge(btn, n) {
    if (!btn) return;
    var b = qs(".nav-badge", btn);
    if (!b) { b = document.createElement("b"); b.className = "nav-badge"; btn.appendChild(b); }
    b.textContent = String(n);
    var want = n > 0;
    if (b.classList.contains("on") !== want) b.classList.toggle("on", want);
  }
  var _usb = window.updateSaveBar;
  window.updateSaveBar = function () {
    _usb.apply(null, arguments);
    badge(attBtn, (typeof S !== "undefined" && S.draft) ? S.draft.size : 0);
  };
  var _uss = window.updateSchSaveBar;
  window.updateSchSaveBar = function () {
    _uss.apply(null, arguments);
    badge(schBtn, (typeof S !== "undefined" && S.sch && S.sch.draft) ? S.sch.draft.size : 0);
  };
  badge(attBtn, (typeof S !== "undefined" && S.draft) ? S.draft.size : 0);
  badge(schBtn, (typeof S !== "undefined" && S.sch && S.sch.draft) ? S.sch.draft.size : 0);
})();
