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
/* الأسبوع: الأحد → السبت. أول أسبوع في السنة هو اللي فيه 1 يناير، يعني 25-31 أكتوبر 2026 = Week 44 */
const weekStartOf = s => { const d = parseISO(s); d.setDate(d.getDate() - d.getDay()); return isoOf(d); };
const weekNoOf = ws => {
  const y = +addDays(ws, 6).slice(0, 4), first = weekStartOf(`${y}-01-01`);
  return Math.round((parseISO(ws) - parseISO(first)) / 86400000 / 7) + 1;
};
const weekLabel = ws => `Week ${weekNoOf(ws)}`;
const weekRangeText = ws => `${fmtDay(ws, { day: "numeric", month: "short" })} – ${fmtDay(addDays(ws, 6), { day: "numeric", month: "short", year: "numeric" })}`;
const curLocale = () => (S.lang === "en" ? "en-US" : "ar-EG-u-nu-latn");
/* نص مزدوج اللغة للقطع اللي بتترسم في JS */
const bi = (ar, en) => (S.lang === "en" ? en : ar);const fmtDay = (s, opts) => parseISO(s).toLocaleDateString(curLocale(), opts);
const anyDrafts = () => S.draft.size + (S.sch ? S.sch.draft.size : 0);
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const on = (id, ev, fn) => $(id).addEventListener(ev, fn);

/* -------- initial-avatars --------
   كانت في ui.js وبتتولّد بعد الرسم عن طريق MutationObserver على الـ body كله،
   يعني أي تغيير في أي حاجة في الصفحة كان بيعيد المسح. بقت دلوقتي جزء من التمبليت
   نفسه (توليد مباشر وقت الرسم) وui.js بقى بيستخدم نفس الدالة دي كـ fallback
   بس للماركب اللي مابيتمرّش من هنا (زي دايلوج السجل). */
const AV_HUES = [212, 262, 158, 24, 340, 190, 40, 290];
function avHue(name) {
  const s = String(name ?? "");
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return AV_HUES[h % AV_HUES.length];
}
function avHTML(name) {
  const s = String(name ?? "").trim();
  return `<span class="av" aria-hidden="true" style="--h:${avHue(name)}">${esc(s[0] || "؟")}</span>`;
}
/* نفس ترتيب ui.js: .wcell > .av + .wtxt(المحتوى الأصلي) */
function whoHTML(name, inner) { return `<div class="wcell">${avHTML(name)}<div class="wtxt">${inner}</div></div>`; }
window.BankaiUI = Object.assign(window.BankaiUI || {}, { avHTML, avHue, whoHTML });

/* -------- micro-interactions --------
   نبضة قصيرة على عنصر/مجموعة عناصر بعد تفاعل (تسجيل حضور، ظهور شريط الحفظ).
   بنشيل الكلاس الأول وبعدين نقرا layout مرة واحدة بس (قراءة واحدة لكل مجموعة،
   مش لكل عنصر) عشان الأنيميشن تبدأ من أول وجديد من غير forced reflow متكرر.
   ولا أي حاجة هنا بتلمس الداتا. */
const reducedMotion = () => { try { return matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { return false; } };
function pulseAll(els, cls, ms = 640) {
  const list = Array.from(els || []).filter(Boolean);
  if (!list.length || reducedMotion()) return;
  list.forEach(el => { el.classList.remove(cls); el.style.animation = ""; });
  void list[0].offsetWidth;
  list.forEach(el => el.classList.add(cls));
  setTimeout(() => list.forEach(el => el.classList.remove(cls)), ms);
}
const pulse = (el, cls, ms) => pulseAll(el ? [el] : [], cls, ms);

const WEEKDAYS = ["Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];
const WD_AR = { Saturday: "السبت", Sunday: "الأحد", Monday: "الاثنين", Tuesday: "الثلاثاء", Wednesday: "الأربعاء", Thursday: "الخميس", Friday: "الجمعة" };
const ST = { Present: ["حاضر", "ok"], Late: ["متأخر", "warn"], Absent: ["غائب", "bad"], Excused: ["بعذر", "info"], Fine: ["غرامة إدارية", "admin"] };
const STK = ["Present", "Late", "Absent", "Excused"];
const CLR = { present: "var(--ok)", late: "#e0a020", absent: "var(--bad)", excused: "#7f8fb0", none: "var(--none)" };

/* ===== Language / الترجمة ===== */
const I18N = {
  ar: {
    // nav
    "نظرة عامة": "نظرة عامة", "الحضور اليومي": "الحضور اليومي", "الجدول": "الجدول", "الموظفون": "الموظفون", "التقارير": "التقارير", "الغرامات": "الغرامات", "الإعدادات": "الإعدادات",
    "البحث عن موظف": "ابحث عن موظف", "الأسبوع السابق": "الأسبوع السابق", "الأسبوع التالي": "الأسبوع التالي", "الأسبوع الحالي": "الأسبوع الحالي",
    "السبت": "السبت", "الأحد": "الأحد", "الاثنين": "الاثنين", "الثلاثاء": "الثلاثاء", "الأربعاء": "الأربعاء", "الخميس": "الخميس", "الجمعة": "الجمعة",
    "حاضر": "حاضر", "متأخر": "متأخر", "غائب": "غائب", "بعذر": "بعذر", "لم يُسجل": "لم يُسجل",
    "الحضور — من سيستم الروستر": "الحضور — من سيستم الروستر", "مفيش تسجيلات في الأسبوع ده": "مفيش تسجيلات في الأسبوع ده",
    "تسجيلات": "تسجيلات", "غرامات": "غرامات", "ج": "ج",
    "الإجمالي": "الإجمالي", "متصل بالروستر": "متصل بالروستر", "غير متصل بالروستر": "غير متصل بالروستر",
    "مش متطابق في الروستر": "مش متطابق في الروستر", "مش عايز ت_TOO_? مش لقى الموظف": "مش متطابق في الروستر",
    "التشغيل": "التشغيل", "إدارة الروستر والحضور للفرعين من مكان واحد.": "إدارة الروستر والحضور للفرعين من مكان واحد.",
    "مركز إدارة العمليات": "مركز إدارة العمليات", "Rawahel": "Rawahel",
    "كلمة المرور": "كلمة المرور", "اكتب كلمة المرور": "اكتب كلمة المرور", "تسجيل الدخول": "تسجيل الدخول",
    "بوابة دعم المناديب": "بوابة دعم المناديب", "سجّل دخولك بكود الـ DAS الخاص بك": "سجّل دخولك بكود الـ DAS الخاص بك",
    "كود المندوب": "كود المندوب", "دخول للسيستم": "دخول للسيستم", "متابعة الشكاوى": "متابعة الشكاوى",
    "لا توجد شكاوى حتى الآن": "لا توجد شكاوى حتى الآن", "اختر قسم من الأعلى لتسجيل أول شكوى": "اختر قسم من الأعلى لتسجيل أول شكوى",
    "الشيفت": "الشيفت", "تعديل شيفت، غياب، تبديل، حضور": "تعديل شيفت، غياب، تبديل، حضور",
    "القبض": "القبض", "طلب سلفة من القبض — تحويل على محفظتك": "طلب سلفة من القبض — تحويل على محفظتك",
    "السلف": "السلف", "متابعة سلفك وحالتها": "متابعة سلفك وحالتها",
    "طلب سلفة": "طلب سلفة", "سبب السلفة": "سبب السلفة", "اكتب سبب السلفة باختصار...": "اكتب سبب السلفة باختصار...",
    "إرسال طلب السلفة": "إرسال طلب السلفة", "سلفك وحالتها": "سلفك وحالتها", "لا توجد سلف حتى الآن": "لا توجد سلف حتى الآن",
    "لو محتاج سلفة اضغط على كارت السلفة فوق": "لو محتاج سلفة اضغط على كارت السلفة فوق",
    "خروج": "خروج", "الإشعارات": "الإشعارات", "تفعيل الإشعارات": "تفعيل الإشعارات", "إلغاء": "إلغاء",
    "الحضور": "الحضور", "الجدول": "الجدول", "الموظفون": "الموظفون", "التقارير": "التقارير", "الغرامات": "الغرامات", "الإعدادات": "الإعدادات",
    "الأسبوع السابق": "الأسبوع السابق", "الأسبوع التالي": "الأسبوع التالي", "الأسبوع الحالي": "الأسبوع الحالي",
    "افتح أسبوع أي يوم": "افتح أسبوع أي يوم", "هذا الأسبوع": "هذا الأسبوع", "نسخ من الأسبوع السابق": "نسخ من الأسبوع السابق",
    "استيراد من Excel": "استيراد من Excel", "تصدير": "تصدير", "Excel": "Excel", "طباعة": "طباعة",
    "شكاوى": "الشكاوى", "السلف": "السلف", "الحضور — من سيستم الروستر": "الحضور — من سيستم الروستر",
    "المبلغ": "المبلغ", "السبب": "السبب", "الحالة": "الحالة", "ملاحظة": "ملاحظة", "عدد السلف": "عدد السلف", "إجمالي السلف (ج)": "إجمالي السلف (ج)",
    "السلف المصروفة": "السلف المصروفة", "قيد المراجعة": "قيد المراجعة", "مرفوض": "مرفوض",
    "الشكاوى": "الشكاوى", "اتحلت": "اتحلت", "تحت المراجعة": "تحت المراجعة", "لازم تيجي المكتب": "لازم تيجي المكتب",
    "متوسط الرضا": "متوسط الرضا", "تقييمات ضعيفة (★2-)": "تقييمات ضعيفة (★2-)",
    "عدد الشكاوى": "عدد الشكاوى", "مصروف (ج)": "مصروف (ج)", "مرفوض (ج)": "مرفوض (ج)",
    "تقرير شهري للغرامات": "تقرير شهري للغرامات", "الأسبوع الحالي": "الأسبوع الحالي", "الأسبوع السابق": "الأسبوع السابق",
    "الأسبوع": "الأسبوع", "نظرة عامة": "نظرة عامة", "الحضور اليومي": "الحضور اليومي", "الجدول": "الجدول",
    "الموظفون": "الموظفون", "التقارير": "التقارير", "الغرامات": "الغرامات", "الإعدادات": "الإعدادات",
    "البحث عن موظف": "ابحث عن موظف", "الأسبوع السابق": "الأسبوع السابق", "الأسبوع التالي": "الأسبوع التالي",
    "الأسبوع الحالي": "الأسبوع الحالي", "افتح أسبوع أي يوم": "افتح أسبوع أي يوم", "هذا الأسبوع": "هذا الأسبوع",
    "نسخ من الأسبوع السابق": "نسخ من الأسبوع السابق", "استيراد من Excel": "استيراد من Excel",
    "تصدير": "تصدير", "Excel": "Excel", "طباعة": "طباعة",
    "الشكاوى": "الشكاوى", "السلف": "السلف", "الحضور — من سيستم الروستر": "الحضور — من سيستم الروستر",
  },
  en: {
    // nav
    "نظرة عامة": "Overview", "الحضور اليومي": "Daily Attendance", "الجدول": "Schedule", "الموظفون": "Employees", "التقارير": "Reports", "الغرامات": "Fines", "الإعدادات": "Settings",
    "البحث عن موظف": "Search employee...", "الأسبوع السابق": "Previous week", "الأسبوع التالي": "Next week", "الأسبوع الحالي": "Current week",
    "السبت": "Saturday", "الأحد": "Sunday", "الاثنين": "Monday", "الثلاثاء": "Tuesday", "الأربعاء": "Wednesday", "الخميس": "Thursday", "الجمعة": "Friday",
    "حاضر": "Present", "متأخر": "Late", "غائب": "Absent", "بعذر": "Excused", "لم يُسجل": "Not marked",
    "الحضور — من سيستم الروستر": "Attendance — from Roster System", "مفيش تسجيلات في الأسبوع ده": "No records this week",
    "تسجيلات": "Records", "غرامات": "Fines", "ج": "EGP",
    "الإجمالي": "Total", "متصل بالروستر": "Connected to roster", "غير متصل بالروستر": "Disconnected from roster",
    "مش متطابق في الروستر": "Not found in roster", "مش عايز ت_TOO_? مش لقى الموظف": "Not found in roster",
    "التشغيل": "Launch", "إدارة الروستر والحضور للفرعين من مكان واحد.": "Roster & attendance management for two branches in one place.",
    "مركز إدارة العمليات": "Operations Control Center", "Rawahel": "Rawahel",
    "كلمة المرور": "Password", "اكتب كلمة المرور": "Enter password", "تسجيل الدخول": "Sign in",
    "بوابة دعم المناديب": "Courier Support Portal", "سجّل دخولك بكود الـ DAS الخاص بك": "Sign in with your DAS code",
    "كود المندوب": "Courier code", "دخول للسيستم": "Enter system", "متابعة الشكاوى": "My tickets",
    "لا توجد شكاوى حتى الآن": "No tickets yet", "اختر قسم من الأعلى لتسجيل أول شكوى": "Pick a category above to file your first ticket",
    "الشيفت": "Shift", "تعديل شيفت، غياب، تبديل، حضور": "Shift, absence, swap, attendance",
    "القبض": "Payroll", "طلب سلفة من القبض — تحويل على محفظتك": "Request advance from payroll — wallet transfer",
    "السلف": "Advances", "متابعة سلفك وحالتها": "Your advances & status",
    "طلب سلفة": "Request advance", "سبب السلفة": "Advance reason", "اكتب سبب السلفة باختصار...": "Write advance reason briefly...",
    "إرسال طلب السلفة": "Submit advance request", "سلفك وحالتها": "Your advances", "لا توجد سلف حتى الآن": "No advances yet",
    "لو محتاج سلفة اضغط على كارت السلفة فوق": "Need an advance? Tap the advance card above",
    "خروج": "Logout", "الإشعارات": "Notifications", "تفعيل الإشعارات": "Enable notifications", "إلغاء": "Cancel",
    "الحضور": "Attendance", "الجدول": "Schedule", "الموظفون": "Employees", "التقارير": "Reports", "الغرامات": "Fines", "الإعدادات": "Settings",
    "الأسبوع السابق": "Previous week", "الأسبوع التالي": "Next week", "الأسبوع الحالي": "Current week",
    "افتح أسبوع أي يوم": "Open any week", "هذا الأسبوع": "This week", "نسخ من الأسبوع السابق": "Copy from previous week",
    "استيراد من Excel": "Import from Excel", "تصدير": "Export", "Excel": "Excel", "طباعة": "Print",
    "الشكاوى": "Tickets", "السلف": "Advances", "الحضور — من سيستم الروستر": "Attendance — from Roster System",
    "المبلغ": "Amount", "السبب": "Reason", "الحالة": "Status", "ملاحظة": "Note", "عدد السلف": "Advances count", "إجمالي السلف (ج)": "Total advances (EGP)",
    "السلف المصروفة": "Paid advances", "قيد المراجعة": "Under review", "مرفوض": "Rejected",
    "الشكاوى": "Tickets", "اتحلت": "Resolved", "تحت المراجعة": "Under review", "لازم تيجي المكتب": "Must visit office",
    "متوسط الرضا": "Avg satisfaction", "تقييمات ضعيفة (★2-)": "Low ratings (★2-)",
    "عدد الشكاوى": "Ticket count", "مصروف (ج)": "Paid (EGP)", "مرفوض (ج)": "Rejected (EGP)",
    "تقرير شهري للغرامات": "Monthly fines report", "الأسبوع الحالي": "Current week", "الأسبوع السابق": "Previous week",
    "الأسبوع": "Week", "نظرة عامة": "Overview", "الحضور اليومي": "Daily Attendance", "الجدول": "Schedule",
    "الموظفون": "Employees", "التقارير": "Reports", "الغرامات": "Fines", "الإعدادات": "Settings",
    "البحث عن موظف": "Search employee...", "الأسبوع السابق": "Previous week", "الأسبوع التالي": "Next week",
    "الأسبوع الحالي": "Current week", "افتح أسبوع أي يوم": "Open any week", "هذا الأسبوع": "This week",
    "نسخ من الأسبوع السابق": "Copy from previous week", "استيراد من Excel": "Import from Excel",
    "تصدير": "Export", "Excel": "Excel", "طباعة": "Print",
    "الشكاوى": "Tickets", "السلف": "Advances", "الحضور — من سيستم الروستر": "Attendance — from Roster System",
    "المبلغ": "Amount", "السبب": "Reason", "الحالة": "Status", "ملاحظة": "Note", "عدد السلف": "Advances count", "إجمالي السلف (ج)": "Total advances (EGP)",
    "السلف المصروفة": "Paid advances", "قيد المراجعة": "Under review", "مرفوض": "Rejected",
    "الشكاوى": "Tickets", "اتحلت": "Resolved", "تحت المراجعة": "Under review", "لازم تيجي المكتب": "Must visit office",
    "متوسط الرضا": "Avg satisfaction", "تقييمات ضعيفة (★2-)": "Low ratings (★2-)",
    "عدد الشكاوى": "Ticket count", "مصروف (ج)": "Paid (EGP)", "مرفوض (ج)": "Rejected (EGP)",
    "تقرير شهري للغرامات": "Monthly fines report", "الأسبوع الحالي": "Current week", "الأسبوع السابق": "Previous week",
    "الأسبوع": "Week",
  }
};

/* توسعة القاموس: نصوص الـ templates الديناميكية عشان EN mode يغطي الصفحة كلها مش جزء منها */
Object.assign(I18N.en, {
  "تخطَّ إلى المحتوى": "Skip to content", "متابعة مباشرة": "Live watch", "مشرف": "Supervisor",
  "ملخص اليوم": "Today summary", "اليوم": "Today", "مجدولون اليوم": "Scheduled today",
  "ج.م": "EGP", "فتح الحضور": "Open attendance", "اكتمل التسجيل": "All marked",
  "لا يوجد موظفون مجدولون في هذا اليوم.": "No employees scheduled on this day.",
  "الحضور — أيام الأسبوع": "Attendance — week days", "أحدث التسجيلات": "Latest entries",
  "لا توجد تسجيلات بعد اليوم": "No entries yet today", "يحتاجون متابعة": "Needs attention",
  "الأكثر غيابًا وتأخيرًا خلال الأسبوع المختار": "Most absent & late in the selected week",
  "مفيش غياب أو تأخير مسجل في الأسبوع ده.": "No absences or lateness recorded this week.",
  "تحديد غير المسجلين كحاضر": "Mark unmarked as present",
  "كل الشيفتات": "All shifts", "كل الأنواع": "All types", "طي/فتح المجموعات": "Collapse/expand groups",
  "تجاهل": "Discard", "جدول الشيفتات": "Shift schedule",
  "ضيف الموظفين على الشيفت مباشرة، وحدّد يوم الإجازة والبريك، وبعدين خلّي النظام يوزّع الأسبوع.": "Add employees to shifts directly, set the day-off & break, and let the system distribute the week.",
  "التوزيع التلقائي": "Auto distribution",
  "النظام بيوزّع الإجازات بالتساوي على أيام الأسبوع جوه كل شيفت، ويوزّع البريك على فترات متدرّجة. هتشوف الاقتراح الأول وبعدين تحفظ.": "The system spreads day-offs evenly across the week within each shift, and staggers breaks. Preview first, then save.",
  "كل شيفتات الفرع": "All branch shifts", "مدة البريك": "Break duration",
  "30 دقيقة": "30 min", "45 دقيقة": "45 min", "ساعة": "1 hour", "ساعة ونص": "1.5 hours",
  "الأيام المتاحة للإجازة": "Day-off options",
  "وزّع الإجازات": "Distribute day-offs", "وزّع البريك": "Distribute breaks",
  "غيّر حتى الأيام المثبّتة": "Change pinned days too", "غيّر البريك الموجود": "Replace existing breaks",
  "اعرض التوزيع المقترح": "Preview suggested spread",
  "إجمالي العاملين كل يوم في الفرع (بعد خصم الإجازات)": "Daily working headcount per branch (after day-offs)",
  "شيفت جديد": "New shift", "اختار وقت البداية والنهاية أو اكتب اسم الشيفت بنفسك.": "Pick start/end times or type your own shift name.",
  "من": "From", "إلى": "To", "الاسم": "Name", "اختياري": "Optional", "إضافة الشيفت": "Add shift",
  "قاعدة بيانات الموظفين": "Employees database", "كل الفروع": "All branches", "كل أيام الإجازة": "All day-offs",
  "النشطون فقط": "Active only", "غير النشطين": "Inactive", "الكل": "All",
  "تعديل جماعي": "Bulk edit", "تفعيل": "Activate", "إيقاف": "Deactivate", "إلغاء التحديد": "Clear selection",
  "الموظف": "Employee", "الفرع": "Branch", "النوع": "Type", "الإجازة": "Day off", "الموبايل": "Mobile",
  "إجراءات": "Actions", "لا توجد نتائج": "No results", "غيّر الفلاتر أو أضف موظفًا جديدًا": "Change filters or add a new employee",
  "تقرير الحضور": "Attendance report", "الأسبوع المختار": "Selected week", "أمس": "Yesterday",
  "آخر 7 أيام": "Last 7 days", "آخر 30 يوم": "Last 30 days", "هذا الشهر": "This month", "الشهر الماضي": "Last month",
  "من تاريخ": "From", "إلى تاريخ": "To", "بحث في الموظفين": "Search employees", "عرض التقرير": "Run report",
  "ملخص الموظفين": "Employees summary", "كل التسجيلات": "All entries", "إجمالي التسجيلات": "Total entries",
  "حضور": "Present", "تأخير": "Late", "غياب": "Absent", "نسبة الحضور": "Attendance rate",
  "التفصيل اليومي": "Daily breakdown", "التوزيع": "Distribution", "تقرير الموظفين": "Employees report",
  "لا توجد تسجيلات في الفترة المحددة": "No records in the selected period",
  "كل الغرامات المسجلة في الفرعين بكل تفاصيلها": "All recorded fines in both branches, full details",
  "آخر 90 يوم": "Last 90 days", "هذه السنة": "This year", "كل الفترات": "All periods",
  "تأخير وغياب": "Late & absent", "تأخير بس": "Late only", "غياب بس": "Absent only",
  "عرض الغرامات": "Run fines report", "إجمالي الغرامات": "Total fines", "عدد الغرامات": "Fines count",
  "غرامات تأخير": "Late fines", "غرامات غياب": "Absent fines", "متوسط الغرامة": "Average fine", "أكبر غرامة": "Largest fine",
  "التفصيل بالأسبوع": "Weekly breakdown", "الفترة": "Period", "التفصيل الشهري": "Monthly breakdown", "الشهر": "Month",
  "سجل الغرامات": "Fines log", "الغرامة": "Fine", "لا توجد غرامات في الفترة المحددة": "No fines in the selected period",
  "الغرامات الافتراضية": "Default fines",
  "تتعبّى تلقائيًا في خانة الغرامة عند اختيار الحالة، وتقدر تعدّلها لكل موظف.": "Auto-filled when picking a status; you can adjust per employee.",
  "الغياب «بعذر» ما عليه غرامة. لو الغرامة أكبر من صفر لازم تكتب السبب قبل الحفظ.": "Excused absence has no fine. A fine above zero requires a reason before saving.",
  "حفظ الإعدادات": "Save settings", "البيانات والنسخ الاحتياطي": "Data & backup", "خد نسخة من بياناتك بشكل دوري.": "Back up your data regularly.",
  "منطقة الخطر": "Danger zone", "إجراءات لا يمكن التراجع عنها.": "Irreversible actions.",
  "سجل الحضور بالكامل": "Full attendance log", "مسح سجل الحضور بالكامل": "Clear the full attendance log",
  "الموظف نشط": "Employee is active", "حفظ": "Save",
  "سيبها «بدون تغيير» للحقول اللي مش عايز تعدّلها.": "Leave \"No change\" for fields you don't want to edit.",
  "بدون تغيير": "No change", "تطبيق على المحددين": "Apply to selected",
  "استيراد موظفين": "Import employees", "انسخ الجدول من Excel والصقه هنا، أو ارفع ملف CSV.": "Paste the sheet from Excel, or upload a CSV file.",
  "الأعمدة المقبولة: الاسم، الكود، الفرع (QCD 1 / QCD 2)، الشيفت، النوع (FT / PT)، الإجازة، الموبايل. لو الصف الأول عناوين بيتعرف عليها تلقائيًا، ولو مفيش بيتقرأ بنفس الترتيب ده.": "Accepted columns: name, code, branch (QCD 1 / QCD 2), shift, type (FT / PT), day-off, mobile. A header row is auto-detected; otherwise columns are read in that order.",
  "استيراد": "Import", "إغلاق": "Close", "استيراد الجدول من Excel": "Import shifts from Excel",
  "الشيت هيتطبّق على أنهي أسبوع؟": "Which week should the sheet apply to?",
  "الأسبوع المفتوح افتراضيًا": "The open week by default",
  "أسبوع جديد": "New week",
  "أسبوع الاستيراد": "Import week",
  "اختر أي يوم في الأسبوع الجديد": "Pick any day in the new week",
  "الشيت بيتقرا تلقائي (فول تايم + بارت تايم). هتشوف المعاينة الأول ومفيش حاجة بتتحفظ إلا لما تأكّد.": "The sheet is auto-read (full-time + part-time). Preview first — nothing saves until you confirm.",
  "لا توجد تسجيلات": "No entries", "في إجازة اليوم": "On leave today",
  "الموظف اللي يوافق يوم إجازته الأسبوعية بيختفي من قائمة الحضور": "Employees whose weekly day-off matches are hidden from today's list",
  "تسجيل لم يُحفظ بعد": "Marking not saved yet",
  "الرئيسية": "Home", "تجميع بالشيفت": "Group by shift", "حفظ الكل": "Save all",
  "النطاق": "Scope", "وزّع الإجازات والبريك": "Distribute day-offs & breaks",
  "حفظ الجدول": "Save schedule", "استيراد من Excel / CSV": "Import from Excel / CSV",
  "عرض": "Show", "صفحة": "Page",
  "ملخص الموظفين CSV": "Employees summary CSV", "كل التسجيلات CSV": "All entries CSV",
  "جرّب فترة أخرى أو سجّل الحضور أولًا.": "Try another period or mark attendance first.",
  "من October 25, 2026 إلى October 31, 2026 — كل الفروع": "All branches · Oct 25, 2026 → Oct 31, 2026",
  "من Oct 25 إلى Oct 31 · كل الفروع": "All branches · Oct 25 → Oct 31",
  "في الفترة المختارة.": "in the selected period.",
  "تصدير CSV": "Export CSV", "نوع الغرامة": "Fine type",
  "مفيش غرامات في الفترة دي.": "No fines in this period.",
  "جرّب توسّع الفترة أو غيّر الفلاتر.": "Try widening the period or changing the filters.",
  "غرامة التأخير (EGP)": "Late fine (EGP)", "غرامة الغياب (EGP)": "Absent fine (EGP)",
  "تنزيل نسخة احتياطية كاملة (.db)": "Download full backup (.db)",
  "تصدير قائمة الموظفين CSV": "Export employees list CSV",
  "كلمة مرور الأدمن بتتغير من متغير": "The admin password is set via a variable",
  "في إعدادات الاستضافة, وكلمة مرور المشرفين من": "in hosting settings, and the supervisors' password via",
  "(لو فاضية يبقى مفيش دخول للمشرفين). بعد تغيير أي واحدة سجّل الدخول من جديد.": "(empty disables supervisor access). Sign in again after changing either.",
  "مسح": "Wipe",
  "(كل التسجيلات المسجلة) والبدء من الصفر. الموظفين والجدول والإعدادات هيفضلوا زي ما هما.": "(all recorded entries) and start from zero. Employees, schedule & settings stay as they are.",
  "اسم الموظف *": "Employee name *", "كود الموظف": "Employee code", "الفرع *": "Branch *",
  "نوع التعاقد *": "Contract type *", "الشيفت *": "Shift *",
  "يوم الإجازة الأسبوعية": "Weekly day-off", "يوم الإجازة": "Day off",
  "رقم الموبايل": "Mobile number",
  "اختيار ملف CSV": "Choose CSV file", "اختيار ملف Excel (.xlsx)": "Choose Excel file (.xlsx)",
  "تنزيل نموذج جاهز": "Download ready template",
  "تحديث الموظفين الموجودين (بالكود أو الاسم)": "Update existing employees (by code or name)",
  "موظف": "employee", "موظفين": "employees", "موقوف": "suspended",
  "الجدول والحضور والغرامات مستقلين لكل أسبوع (من الأحد للسبت)": "Schedule, attendance and fines are independent per week (Sunday–Saturday)",
  "في إجازة اليوم:": "On leave today:", "لا يوجد موظفون مجدولون في هذا اليوم": "No employees scheduled on this day",
  "الموظف اللي يوافق يوم إجازته الأسبوعية بيختفي من قائمة الحضور.": "Employees whose weekly day-off matches are hidden from today's list.",
  "أضف أول شيفت من القسم اللي تحت, وبعدها ابدأ إضافة الموظفين.": "Add the first shift in the section below, then start adding employees.",
  "الاسم (اختياري)": "Name (optional)", "إضافة موظف": "Add employee",
  "غيّر الفلاتر أو أضف موظفًا جديدًا.": "Change filters or add a new employee.",
  "نوع التعاقد": "Contract type",
  "ابحث عن موظف": "Search employee", "ابحث عن موظف في الجدول": "Search an employee in the schedule",
  "ابحث بالاسم أو الكود أو الموبايل": "Search by name, code or mobile",
  "ابحث بالاسم أو الكود أو الموبايل   ( / )": "Search by name, code or mobile   ( / )",
  "يتكوّن تلقائيًا من الوقت": "Auto-generated from the times",
  "الاسم أو الكود": "Name or code", "الاسم بالكامل": "Full name",
  "مثال: 12 AM - 8 AM": "e.g. 12 AM - 8 AM",
  // جدول الموظفين: الشارات والأزرار السطرية (نشط/تعديل/السجل) + مفاتيح صغيرة
  "نشط": "Active", "تعديل": "Edit", "السجل": "History", "سجل الحضور": "Attendance log",
  "بدون كود": "No code", "نقل": "Move",
  // شريط علوي ثابت + أدوات عامة
  "تبديل الوضع الليلي": "Toggle dark mode", "الوضع الداكن": "Dark mode",
  "English / عربي": "English / Arabic", "تغيير للعربية": "Switch to Arabic",
  "تنبيه صوتي عند تغيير من مستخدم تاني — شغّال (اضغط للكتم)": "Sound alert for other users' changes — on (click to mute)",
  "أدمن — كل الصلاحيات": "Admin — full access",
  "تسجيل الخروج": "Sign out", "القائمة الرئيسية": "Main menu", "التنقل": "Navigation",
  "متصل: أي تغيير من أي مستخدم تاني بيظهر هنا فورًا من غير refresh": "Connected: any change by another user shows up instantly, no refresh needed",
  "اليوم السابق": "Previous day", "اليوم التالي": "Next day", "التاريخ": "Date",
  "يحدد كل من لم يُسجل بعد في العرض الحالي كحاضر": "Mark everyone not yet recorded in the current view as present",
  "ينسخ شيفتات وإجازات وبريك الأسبوع اللي قبله فوق الأسبوع ده": "Copies last week's shifts, day-offs & breaks onto this week",
  "تحديد الكل": "Select all", "تحديث": "Refresh", "السابق": "Previous", "التالي": "Next",
  "عدد الصفوف": "Rows per page", "الغرامة": "Fine", "غرامة": "Fine",
  "اتصال": "Call", "واتساب": "WhatsApp",
  // الجدول (schedule)
  "لصق أسماء": "Paste names", "وزّع هذا الشيفت": "Distribute this shift",
  "تعديل الاسم": "Rename", "ساعة البريك": "Break hour", "إضافة": "Add",
  "إزالة من الجدول": "Remove from schedule", "اسم موظف جديد": "New employee name",
  "اكتب اسم موظف واضغط Enter لإضافته": "Type a name and press Enter to add",
  "ثبّت يوم الإجازة عشان التوزيع التلقائي ما يغيّروش": "Pin the day-off so auto-distribution won't change it",
  "يوم الإجازة مثبّت (التوزيع التلقائي مش هيغيّره). اضغط للإلغاء": "Day-off is pinned (auto-distribution won't change it). Click to unpin",
  "نقل لشيفت آخر أو لفرع تاني": "Move to another shift or branch",
  "نقل…": "Move…",
  "حاضر للباقي": "Present the rest",
  "دوام كامل": "Full Time", "دوام جزئي": "Part Time", "بدون بريك": "No break",
});

/* خريطة عكسية: من إجابات EN رجّع عربي عند الرجوع للوضع العربي */
const RO_INV = {};
for (const k of Object.keys(I18N.en)) RO_INV[String(I18N.en[k])] = k;
for (const k of Object.keys(I18N.ar)) { const v = String(I18N.ar[k]); if (v !== k) { RO_INV[v] = k; } }

/* كلمات التواريخ: أيام/شهور بتظهر من الدوال الديناميكية في EN */
const RO_DAYS = { "السبت": "Saturday", "الأحد": "Sunday", "الاثنين": "Monday", "الإثنين": "Monday", "الثلاثاء": "Tuesday", "الأربعاء": "Wednesday", "الخميس": "Thursday", "الجمعة": "Friday" };
const RO_MONTHS = { "يناير": "January", "فبراير": "February", "مارس": "March", "أبريل": "April", "مايو": "May", "يونيو": "June", "يوليو": "July", "أغسطس": "August", "سبتمبر": "September", "أكتوبر": "October", "نوفمبر": "November", "ديسمبر": "December" };
const RO_DAYS_RE = new RegExp("^(السبت|الأحد|الاثنين|الإثنين|الثلاثاء|الأربعاء|الخميس|الجمعة)([\u060C\u060C\\s,]|$)");

/* ---------- ?? translator ???? ??????? (????? applyLang) ---------- */
const RO_TRANS = [
  // regex pattern -> replacement function receiving match
  [/\u0641\u064a \u0625\u062c\u0627\u0632\u0629 \u0627\u0644\u064a\u0648\u0645: (\d+) \u0645\u0648\u0638\u0641/g, (m,a) => "On Leave Today: " + a + " Employees"],
  [/\u0645\u0648\u0638\u0641\u0648\u0646 (Week \d+): (\d+)/g, (m,w,n) => w + " Employees: " + n],
  [/\u063a\u0631\u0627\u0645\u0627\u062a (Week \d+): ([\d.,]+) \u062c\.\u0645/g, (m,w,n) => w + " Fines: " + n + " EGP"],
  [/(\d+) \u0645\u0648\u0638\u0641 \u0645\u062c\u062f\u0648\u0644 \u0627\u0644\u064a\u0648\u0645/g, (m,n) => n + " Employees Scheduled Today"],
  [/^\u0641\u064a \u0625\u062c\u0627\u0632\u0629 \u0627\u0644\u064a\u0648\u0645 \((\d+)\)$/g, (m,n) => "On leave today (" + n + ")"],
  [/^(\d+) \u062a\u0633\u062c\u064a\u0644 \u0644\u0645 \u064a\u064f\u062d\u0641\u0638 \u0628\u0639\u062f$/g, (m,n) => n + " unsaved entry marks"],
  [/^(\d+) \u0645\u0648\u0638\u0641 \u0639\u0644\u064a\u0647\u0645 \u062a\u0639\u062f\u064a\u0644\u0627\u062a \u0644\u0645 \u062a\u064f\u062d\u0641\u0638 \u0628\u0639\u062f$/g, (m,n) => n + " employees with unsaved changes"],
  [/^(\d+) \u0645\u0648\u0638\u0641 \u0646\u0634\u0637 \u00b7 (\d+) \u0645\u0648\u0642\u0648\u0641$/g, (m,a,b) => a + " active \u00b7 " + b + " suspended"],
  [/^\u062a\u0645 \u062a\u062d\u062f\u064a\u062f (\d+) \u0645\u0648\u0638\u0641$/g, (m,n) => n + " employees selected"],
  [/^(\d+) \u063a\u0631\u0627\u0645\u0629$/g, (m,n) => n + " fines"],
  [/^(\d+)% \u0645\u0646 \u0627\u0644\u0625\u062c\u0645\u0627\u0644\u064a$/g, (m,n) => n + "% of total"],
  [/^\u0625\u062c\u0627\u0632\u0629: (\d+)$/g, (m,n) => "Off: " + n],
  [/^\u0645\u0648\u0638\u0641\u064a\u0646 (Week \d+):$/g, (m,w) => w + " Employees:"],
  [/^\u063a\u0631\u0627\u0645\u0627\u062a (Week \d+):$/g, (m,w) => w + " Fines:"],
  [/^\u0645\u0641\u064a\u0634 \u0634\u064a\u0641\u062a\u0627\u062a \u0641\u064a (.+) \u0644\u0633\u0647$/g, (m,b) => "No shifts in " + b + " yet"],
  [/^(\d+) \u0635\u0641$/g, (m,n) => n + " rows"],
  [/^\u062a\u062d\u062f\u064a\u062f (.+)$/g, (m,name) => "Select " + name],
  [/^\u0646\u0642\u0644 \u0644\u0641\u0631\u0639 (.+) \u2014 \u0628\u0646\u0641\u0633 \u0627\u0644\u0634\u064a\u0641\u062a \u0648\u064a\u0648\u0645 \u0627\u0644\u0625\u062c\u0627\u0632\u0629 \u0648\u0627\u0644\u0628\u0631\u064a\u0643$/g, (m,b) => "Move to " + b + " — same shift, day-off & break"],
  [/^\u0646\u0642\u0644 (.+) \u0644\u0641\u0631\u0639 (.+)$/g, (m,n,b) => "Move " + n + " to " + b],
  [/^\u0646\u0642\u0644 \u0644\u0641\u0631\u0639 (.+)$/g, (m,b) => "Move to branch " + b],
  // عدّادات دايلوج السجل والرسوم البيانية: "تسجيلات: 4" / "حاضر: 5" / "حاضر 1 · متأخر 2"
  [/(\u062a\u0633\u062c\u064a\u0644\u0627\u062a|\u062d\u0636\u0648\u0631|\u062d\u0627\u0636\u0631|\u0645\u062a\u0623\u062e\u0631|\u063a\u0627\u0626\u0628|\u0628\u0639\u0630\u0631): (\d+)/g, (m,w,n) => ({ "\u062a\u0633\u062c\u064a\u0644\u0627\u062a": "Records", "\u062d\u0636\u0648\u0631": "Present", "\u062d\u0627\u0636\u0631": "Present", "\u0645\u062a\u0623\u062e\u0631": "Late", "\u063a\u0627\u0626\u0628": "Absent", "\u0628\u0639\u0630\u0631": "Excused" }[w] + ": " + n)],
  [/(\u062d\u0627\u0636\u0631|\u0645\u062a\u0623\u062e\u0631|\u063a\u0627\u0626\u0628|\u0628\u0639\u0630\u0631) (\d+)/g, (m,w,n) => ({ "\u062d\u0627\u0636\u0631": "Present", "\u0645\u062a\u0623\u062e\u0631": "Late", "\u063a\u0627\u0626\u0628": "Absent", "\u0628\u0639\u0630\u0631": "Excused" }[w] + " " + n)],
  [/^\u062a\u0645 \u062a\u0633\u062c\u064a\u0644 (\d+) \u0645\u0646 (\d+)$/g, (m,a,b) => "Recorded " + a + " of " + b],
  [/^(\d+) \u0645\u0648\u0638\u0641$/g, (m,n) => n + " employee" + (n === 1 ? "" : "s")],
  [/^(\d+) \u0645\u0648\u0638\u0641 \u0644\u0647\u0645 \u062a\u0633\u062c\u064a\u0644\u0627\u062a \u0641\u064a \u0627\u0644\u0641\u062a\u0631\u0629$/g, (m,n) => n + " employees with records in the period"],
  [/^(\d+) \u062a\u0633\u062c\u064a\u0644$/g, (m,n) => n + " records"],
  [/^(\d+) \u063a\u0631\u0627\u0645\u0629 \u0628\u0627\u0644\u062a\u0641\u0627\u0635\u064a\u0644$/g, (m,n) => n + " fines with details"],
  [/^\u0641\u0631\u0642 (\d+) \u0628\u064a\u0646 \u0623\u0642\u0644 \u0648\u0623\u0643\u062b\u0631 \u064a\u0648\u0645$/g, (m,n) => "Spread of " + n + " between lightest & busiest day"],
  [/^\u062d\u0627\u0644\u0629 (.+)$/g, (m,name) => "Status: " + name],
  [/^\u0625\u0632\u0627\u0644\u0629 (.+)$/g, (m,name) => "Remove " + name],
  [/^\u062a\u0633\u062c\u064a\u0644\u0627\u062a: (\d+)$/g, (m,n) => "Records: " + n],
  [/^\u062d\u0636\u0648\u0631: (\d+)$/g, (m,n) => "Present: " + n],
  [/^\u062a\u0623\u062e\u064a\u0631: (\d+)$/g, (m,n) => "Late: " + n],
  [/^\u063a\u064a\u0627\u0628: (\d+)$/g, (m,n) => "Absent: " + n],
  [/^\u0628\u0639\u0630\u0631: (\d+)$/g, (m,n) => "Excused: " + n],
  [/\u0625\u062c\u0627\u0632\u0629 (\u0627\u0644\u064a\u0648\u0645|\u0627\u0644\u0633\u0628\u062a|\u0627\u0644\u0623\u062d\u062f|\u0627\u0644\u0625\u062b\u0646\u064a\u0646|\u0627\u0644\u0627\u062b\u0646\u064a\u0646|\u0627\u0644\u062b\u0644\u0627\u062b\u0627\u0621|\u0627\u0644\u0623\u0631\u0628\u0639\u0627\u0621|\u0627\u0644\u062e\u0645\u064a\u0633|\u0627\u0644\u062c\u0645\u0639\u0629)/g, (m,d) => "day-off " + (RO_DAYS[d] || d)],
  [/\u0645\u0648\u0642\u0648\u064f\u0641$|\u0645\u0648\u0642\u0648\u0641$/g, (m) => "suspended"],
  [/\u062c\u062f\u0648\u0644 \u0627\u0644\u0634\u064a\u0641\u062a\u0627\u062a/g, () => "Shift schedule"],
];
/* ترجمة عكسية (EN -> AR) — للنصوص المركّبة اللي بتترجم بـ regex.
   الـ RO_TRANS بيرجمها العربي -> الإنجليزي، والقاموس بيرجم الكلمات المفردة.
   لكن النص المركّب زي «On leave today (3)» مالوش مفتاح في القاموس، فلازم
   نمشي عليه بالـ regex في الاتجاه العكس — وإلا فضل إنجليزي بعد الرجوع عربي. */
const RO_TRANS_INV = [
  [/^On leave today \((\d+)\)$/g, (m,n) => "في إجازة اليوم (" + n + ")"],
  [/^(\d+) unsaved entry marks$/g, (m,n) => n + " تسجيل لم يُحفظ بعد"],
  [/^(\d+) employees with unsaved changes$/g, (m,n) => n + " موظف عليهم تعديلات لم تُحفظ بعد"],
  [/^(\d+) active · (\d+) suspended$/g, (m,a,b) => a + " نشط · " + b + " موقوف"],
  [/^Shift schedule — (.+) — Week (\d+)$/g, (m,b,w) => "جدول الشيفتات — " + b + " — الأسبوع " + w],
  [/^Move to branch (.+)$/g, (m,b) => "نقل لفرع " + b],
  [/^Off: (\d+)$/g, (m,n) => "إجازة: " + n],
];
/* يرجّع النص العربي equivalent لو النص ده إنجليزي مركّب */
function toArabicText(s) {
  try {
    let t = String(s || "");
    for (const [re, fn] of RO_TRANS_INV) t = t.replace(re, fn);
    return t;
  } catch (e) { return s; }
}

function translateText(s) {
  try {
    if (S.lang !== "en") return s;
    const dict = I18N.en;
    let t = s;
    const key = s.trim();
    const keyNoDots = key.replace(/[…\.]{1,3}$/, "").trim();
    if (dict[key]) t = dict[key];
    else if (dict[keyNoDots]) t = dict[keyNoDots];
    for (const [re, fn] of RO_TRANS) t = t.replace(re, fn);
    t = t.replace(RO_DAYS_RE, (m, d, sep) => RO_DAYS[d] + (sep === "\u060C" ? ", " : sep));
    for (const [ar, en] of Object.entries(RO_MONTHS)) if (t.indexOf(ar) !== -1) t = t.split(ar).join(en);
    if (t.indexOf("ج.م") !== -1) t = t.split("ج.م").join("EGP");
    if (t.indexOf("\u060C") !== -1) t = t.split("\u060C").join(",");
    return t;
  } catch (e) { return s; }
}

// walking pass
function translateTree(root) {
  try {
    if (!root) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
  let n;
  while ((n = walker.nextNode())) {
    // نعدّل nodeValue مباشرة — آمن حتى لو الأب فيه عناصر تانية (مش textContent).
    // SCRIPT/STYLE بس المستثناة.
    const owner = n.parentNode;
    if (owner && owner.nodeName !== "SCRIPT" && owner.nodeName !== "STYLE") {
        const trimmed = n.nodeValue.trim();
        if (trimmed) {
          const tr = translateText(trimmed);
          if (tr !== trimmed) {
            n.nodeValue = n.nodeValue.replace(trimmed, tr);
          }
        }
      }
    }
    // attributes
    const els = root.querySelectorAll("*");
    if (els) els.forEach(el => {
      if (el.hasAttribute("placeholder")) { const v = el.getAttribute("placeholder"); const t = translateText(v); if (t !== v) el.setAttribute("placeholder", t); }
      if (el.hasAttribute("title")) { const v = el.getAttribute("title"); const t = translateText(v); if (t !== v) el.setAttribute("title", t); }
      if (el.hasAttribute("aria-label")) { const v = el.getAttribute("aria-label"); const t = translateText(v); if (t !== v) el.setAttribute("aria-label", t); }
    });
  } catch (e) { console.warn("translateTree error:", e); }
}
// hook into applyLang by overriding? Instead, monkey: wrap original applyLang not accessible. We'll add a separate scheduler.
window.__roTranslate = function() { translateTree(document.body); };
// ترجمة تلقائية لأي DOM يتولد بعد الترجمة الأولانية (جدول/شيتات/توست) — عشان
// العربية في EN mode متفضلش إلآ أجزاء صغيرة. نقطع الرصد وقت الترجمة نفسها عشان
// مش نعمل حلقة مفتوحة (الترجمة نفسها بتعدّل DOM).
(function () {
  if (!("MutationObserver" in window)) return;
  /* الترجمة هنا فورية في نفس الـ microtask — قبل ما المتصفح يرسم أي فريم.
     زمان كانت بتتأخر بـ setTimeout 120ms فالمستخدم كان بيشوف فريمات عربي
     الأول وبعدين يترجع EN (فلاش) وانتقالات الصفحات بيلقط الفريم العربي ده. */
  var mo = new MutationObserver(function () {
    if (S.lang !== "en") return;
    try { mo.disconnect(); } catch (e) {}
    try { window.__roTranslate(); } catch (e) {}
    try { mo.observe(document.body, { childList: true, subtree: true }); } catch (e) {}
  });
  function startI18nMO() { try { mo.observe(document.body, { childList: true, subtree: true }); } catch (e) {} }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", startI18nMO); else startI18nMO();
})();

/* دالة التبديل للغة */
function setLang(lang) {
  S.lang = lang;
  try { sessionStorage.setItem("bankaicode_lang", lang); } catch (e) {}
  applyLang();
  refreshLangView();
  const btn = $("js-lang") || qs(".js-lang");
  if (btn) btn.setAttribute("aria-label", lang === "ar" ? "Switch to English" : "تغيير للعربية");
}

/* بعد تبديل اللغة: بنعيد رسم الصفحة الحالية. السبب: التواريخ والأرقام
   (شريط الأسبوع، جداول الغرامات والتقارير) بتترجم وقت الرسم حسب لغة الصفحة،
   فلو ما أعدنا الرسم كانت هتفضل باللغة القديمة. */
function refreshLangView() {
  try { updateWeekBar(); } catch (e) {}
  try { updateTodayPill(); } catch (e) {}
  try { syncAttControls(); } catch (e) {}
  try { updateSaveBar(); updateSchSaveBar(); } catch (e) {}
  try { if (S.sch.data) renderSchedule(); } catch (e) {}
  // دايلوج استيراد الشيت: النصوص اللي بتترسم بالـJS (الأسبوع/الخيارات/الزر) بتتعاد بلغة الصفحة
  try {
    if (S.simp && $("schImpDialog") && $("schImpDialog").open) { simpWeeksRender(); if (S.simp.plan) simpRender(); }
  } catch (e) {}
  const p = S.page;
  if (!p || !S.shown) return;
  try {
    if (p === "dashboard") loadDashboard();
    else if (p === "attendance") { renderAttendance(); }
    else if (p === "schedule") { if (S.sch.data) renderSchedule(); }
    else if (p === "employees") renderEmployees();
    else if (p === "reports") { if (S.rep.sum) renderReport(); renderReportRows(); }
    else if (p === "fines") loadFines();
  } catch (e) { /* رسم اختياري — لو فشل ما نوقفش التبديل */ }
}

/* تاريخ النهارده في شريط الصفحة — بلغة الصفحة (عربي/إنجليزي) */
function updateTodayPill() {
  const el = $("todayPill"); if (!el) return;
  el.textContent = new Date().toLocaleDateString(curLocale(),
    { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

/* مثال الأعمدة في دايلوج الاستيراد — بلغة الصفحة */
function applyImportPlaceholder() {
  const el = $("impText"); if (!el) return;
  el.placeholder = S.lang === "en"
    ? "Name\tCode\tBranch\tShift\tType\tDay off\tMobile\nAhmed Mohamed\t1001\tQCD 1\t12 AM - 8 AM\tFT\tFriday\t01012345678"
    : "الاسم\tالكود\tالفرع\tالشيفت\tالنوع\tالإجازة\tالموبايل\nأحمد محمد\t1001\tQCD 1\t12 AM - 8 AM\tFT\tالجمعة\t01012345678";
}

/* تطبيق الترجمة على كل العناصر */
function applyLang() {
  // تبديل اتجاه الصفحة (RTL للعربية، LTR للانكليزية)
  const isAr = S.lang === "ar";
  document.documentElement.lang = isAr ? "ar" : "en";
  document.documentElement.dir = isAr ? "rtl" : "ltr";
  const dict = isAr ? I18N.ar : I18N.en;
  // fallback مختلف لكل لغة: في AR بنعكس قاموس EN عشان نرجّع النصوص الأصلية
  const fb = isAr ? RO_INV : I18N.en;
  // العناصر اللي فيها data-i18n
  document.querySelectorAll("[data-i18n]").forEach(el => {
    const key = el.getAttribute("data-i18n");
    if (dict[key]) el.textContent = dict[key];
    // في وضع العربية: قيمة data-i18n هي النص العربي الأصلي. لو المفتاح مش في
    // قاموس AR (زي النصوص الديناميكية) نرجّعه كده، وإلا النص الإنجليزي هيفضل
    // متعلّق في الصفحة بعد الرجوع للعربي.
    else if (isAr) el.textContent = key;
  });
  // الـ placeholders
  document.querySelectorAll("[data-i18n-placeholder]").forEach(el => {
    const key = el.getAttribute("data-i18n-placeholder");
    if (dict[key]) el.placeholder = dict[key];
    else if (isAr) el.placeholder = key;
  });
  // placeholders من غير data-i18n: رجّعها لو كانت اتحوّلت لإنجليزي
  document.querySelectorAll("[placeholder]").forEach(el => {
    if (el.hasAttribute("data-i18n-placeholder")) return;
    const v = el.getAttribute("placeholder");
    if (v && fb[v]) el.setAttribute("placeholder", fb[v]);
  });
  // الـ titles
  document.querySelectorAll("[data-i18n-title]").forEach(el => {
    const key = el.getAttribute("data-i18n-title");
    if (dict[key]) el.title = dict[key];
  });
  // الـ aria-label
  document.querySelectorAll("[data-i18n-aria]").forEach(el => {
    const key = el.getAttribute("data-i18n-aria");
    if (dict[key]) el.setAttribute("aria-label", dict[key]);
  });
  // العناصر اللي متعرفش data-i18n بس نصها مطابق للمفتاح (fallback للنصوص الثابتة)
  document.querySelectorAll("a, button, span, div, p, label, h1, h2, h3, h4, h5, h6, b, strong, em, li, dd, dt, th, td, summary, option, legend, figcaption, small").forEach(el => {
    if (el.hasAttribute("data-i18n") || el.closest("[data-i18n]")) return;
    const txt = el.textContent.trim();
    if (!txt) return;
    // النص العربي المقابل: من القاموس ( fb ) أو من الـ regex العكسي (نص مركّب)
    const ar = fb[txt] || toArabicText(txt);
    // 1) عنصر نصه بس (من غير أيقونة ولا عناصر جوه): نغيّر النص كله
    if (!el.children.length && !el.querySelector("svg, i, .i, .av")) {
      if (ar && ar !== txt) el.textContent = ar;
      return;
    }
    // 2) عنصر فيه أيقونة أو عناصر جوه (زي زرار «تصدير CSV» أو label فيه select):
    //    نرجّع كل نص جواه لوحده من غير ما نمسس الأيقونة ولا التنسيق.
    //    لازم نمشي على كل node جواه: لو العنصر كله نصه مركّب (زي label فيه select
    //    وOptions بتاعته) مش هيطابق مفتاح واحد — فمطابقة الـ node هو الحل.
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    let n;
    while ((n = w.nextNode())) {
      const owner = n.parentNode;
      if (owner && owner.hasAttribute && owner.hasAttribute("data-i18n")) continue;
      const t = n.nodeValue.trim();
      if (!t) continue;
      const back = fb[t] || toArabicText(t);
      if (back && back !== t) n.nodeValue = n.nodeValue.replace(t, back);
    }
  });
  // تحديث placeholder للبحث
  const searchInp = $("navSearch") || qs("#navSearch");
  if (searchInp && dict["البحث عن موظف"]) searchInp.placeholder = dict["البحث عن موظف"];
  applyImportPlaceholder();
  // تحديث زرار اللغة
  const langBtn = $("js-lang") || qs(".js-lang");
  if (langBtn) langBtn.setAttribute("aria-label", S.lang === "ar" ? "Switch to English" : "تغيير للعربية");
  // عنوان التبويب (document.title) لازم يتبع اللغة — كان بيفضل عربي
  // بعد التحويل للإنجليزي ويفضح إن الترجمة متبكّرة
  const ptEl = $("pageTitle");
  if (ptEl && ptEl.textContent) document.title = `${ptEl.textContent} — Bankai`;
}
/* كنا بنادي translateTree(document.body) هنا على مستوى الملف، وده كان بيشيل
   الـ input جوه الـ label وقت الرسم (textContent بيمسح العناصر الأبناء) — اشتغل
   مرة واحدة من loadLang بس. */

/* تبديل اللغة */
function toggleLang() {
  setLang(S.lang === "ar" ? "en" : "ar");
}

/* تحميل اللغة المحفوظة */
function loadLang() {
  try {
    const saved = sessionStorage.getItem("bankaicode_lang");
    if (saved) S.lang = saved;
  } catch (e) {}
  if (!S.lang) S.lang = "ar";
  applyLang();
}

const S = {
  token: localStorage.getItem("bankai_token") || "",
  role: localStorage.getItem("bankai_role") || "",
  page: "", shown: false,
  settings: { fine_late: 0, fine_absent: 0 },
  employees: [],
  week: "",
  dash: { date: todayISO() },
  roster: null, rmap: new Map(), draft: new Map(), view: { base: [], list: [], groups: new Map() },
  att: { date: todayISO(), branch: "", shift: "", type: "", q: "", status: "", group: true, collapsed: new Set() },
  emp: { q: "", branch: "", shift: "", type: "", day: "", active: "1", sort: { key: "name", dir: 1 }, page: 1, size: 50, selected: new Set(), filtered: [] },
  rep: { sum: null, emps: [], sort: { key: "name", dir: 1 }, page: 1, size: 50, preset: "week" },
  fines: { data: null, sort: { key: "date", dir: -1 }, page: 1, size: 50, preset: "week" },
  hist: { id: null },
  lang: "ar",
  stale: new Set(), /* صفوف اتغيّرت من مستخدم تاني وعندنا عليها مسودة مش محفوظة */
};
/* معرّف التاب الحالي: بيتبعت مع كل طلب والسيرفر بيرجّعه في حدث التحديث المباشر
   عشان التاب تتجاهل التغييرات اللي هي مصدرها (مفيش تنبيه لنفسك). */
S.week = weekStartOf(todayISO());
const wq = () => `week=${S.week}`;
S.clientId = (() => {
  try {
    let v = sessionStorage.getItem("bankai_cid");
    if (!v) { v = Math.random().toString(36).slice(2) + Date.now().toString(36); sessionStorage.setItem("bankai_cid", v); }
    return v;
  } catch (e) { return "tab" + Math.random().toString(36).slice(2); }
})();

/* شريط تقدّم رقيق أعلى الصفحة. بيظهر بس لو الطلب خد وقت (130ms) عشان مايرمشش
   مع كل تفاعل سريع، والطلبات الخلفية (تحديث الـ feed كل 30 ثانية) بتبعت silent. */
let inflight = 0, barTimer;
function trackStart() {
  inflight++;
  if (inflight === 1) barTimer = setTimeout(() => $("routeBar")?.classList.add("on"), 130);
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
  if (S.clientId) headers["X-Client-Id"] = S.clientId;
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
  stopLive();
  S.token = ""; S.role = "";
  localStorage.removeItem("bankai_token"); localStorage.removeItem("bankai_role");
  delete document.documentElement.dataset.auth; /* نشيل قفل إخفاء شاشة الدخول المبكّر بتاع الـ head */
  S.draft.clear(); if (S.sch) S.sch.draft.clear(); S.shown = false; S.page = ""; S.stale.clear();
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
  const m = document.querySelector('meta[name="theme-color"]'); if (m) m.content = t === "dark" ? "#0b1220" : "#1e40af";
  qsa(".js-theme").forEach(b => b.setAttribute("aria-checked", t === "dark" ? "true" : "false"));
  if (save) { try { sessionStorage.setItem("bankai-theme", t); } catch (e) {} }
}
applyTheme(document.documentElement.dataset.theme || "light", false);
qsa(".js-theme").forEach(b => b.addEventListener("click", () =>
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true)));

// ---------- language (ar / en) ----------
loadLang();
qsa(".js-lang").forEach(b => b.addEventListener("click", () => toggleLang()));

function showApp() {
  $("loginView").classList.add("hidden"); $("appView").classList.remove("hidden");
  updateTodayPill();
  if (window.syncNavInd) window.syncNavInd();
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
  if (window.syncNavInd) window.syncNavInd();
}

const PAGES = { dashboard: "نظرة عامة", attendance: "الحضور اليومي", schedule: "الجدول", employees: "الموظفون", reports: "التقارير", fines: "الغرامات", settings: "الإعدادات" };
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

/* ===== انتقالات الصفحات: View Transitions =====
   لو المتصفح بيدعم startViewTransition بنلف تبديل الصفحة جواه، فيبقى في انتقال
   ناعم بين الصفحتين بدل القطعة الجافة. بنتخطاها:
     • وقت هيكل التحميل الأول (الصفحة بتفضل مخفية وراء الهيكل لحد الداتا توصل)،
     • لأي حد طالب prefers-reduced-motion.
   وبنعلّم .vt-skip على الصفحة الجديدة عشان أنيميشن pageIn مايبدأش قبل ما لقطة
   الانتقال تتّاخد (لو بدأ، اللقطة هتبقى صفحة شبه فاضية والانتقال هيبان كأنه فلاش).
   وطبقة الحركة بتقرا window.bkVtActive عشان تأجّل دخول العناصر لحد ما يخلص. */
let vtPending = null, vtToken = 0, vtGuard = null, vtLast = null;
function showPage(p) {
  S.page = p; S.shown = true;
  let swapped = false;
  const swap = () => {
    if (swapped) return;
    swapped = true;
    const pg = $("page-" + p);
    qsa(".page").forEach(x => { if (x !== pg) x.classList.add("hidden"); });
    if (window.bkVtActive) pg.classList.add("vt-skip");
    pg.classList.remove("hidden");
    qsa("[data-page]").forEach(b => b.classList.toggle("active", b.dataset.page === p));
    /* عنوان الصفحة بيتحط من قاموس اللغة الحالية مباشرة (مش عربي ثابت) —
       عشان في وضع EN ميتلقطش النص العربي لحظة قبل ما يترجم */
    const pgTitle = (S.lang === "en" && I18N.en[PAGES[p]]) ? I18N.en[PAGES[p]] : PAGES[p];
    $("pageTitle").textContent = pgTitle;
    $("wkBar").classList.toggle("hidden", p === "settings");
    document.title = `${pgTitle} — Bankai`;
    /* في وضع EN نترجم الصفحة الجديدة فوراً قبل ما تتعرض — عشان انتقال
       الصفحات ميلقطش فريم عربي (فلاش "عربي ورجع إنجليزي") */
    if (S.lang === "en") { try { translateTree(pg); } catch (e) {} }
    /* النزول لأعلى صفحة لحظي — html scroll-behavior:smooth كان بيعمل سكرول
       متحرك وقت تنقّل الصفحات وده كان بيخرب إحساس الانتقال */
    window.scrollTo({ top: 0, behavior: "instant" });
  };
  if (document.startViewTransition && !reducedMotion() && !$("bootSkel")) {
    /* لو فيه انتقال لسه شغال من تنقّل قبله، نلغيه فورًا — غير كده الانتقال
       القديم بيمسك الجديد والصفحة الجديدة بتتأخر (تنقّل سريع = صفحات متلخبطة) */
    if (vtLast) { try { vtLast.skipTransition(); } catch (e) {} }
    window.bkVtActive = true;
    const my = ++vtToken;
    let myGuard = null;
    const off = () => {
      if (vtToken === my) window.bkVtActive = false;
      if (vtGuard === myGuard) { clearTimeout(myGuard); vtGuard = null; }
    };
    /* لقطة الصفحة الجديدة بتتاخد لحظة ما الـ update callback يخلص. لو ده حصل
       قبل ما الداتا توصل، المستخدم بيشوف إطار الصفحة فاضي وبعدين الداتا تنطّ
       عليه — إحساس إن الصفحة حمّلت مرتين. هنا بنستنى أول رندر للصفحة (بسقف
       زمني قصير من طبقة الحركة) وبعدها بس نبدأ الانتقال، فالصفحة تظهر مكمّلة
       في حركة واحدة. */
    const wait = window.bkVtWait ? window.bkVtWait(p) : null;
    const upd = wait ? async () => { swap(); await wait; } : swap;
    let vt = null;
    try {
      vt = document.startViewTransition(upd);
      vtLast = vt;
      /* كل وعود الانتقال (ready/updateCallbackDone/finished) ممنوع تسيبها
         من غير معالج: لو الانتقال اتلغى (تبديل سريع ورا بعض، أو تاب اتخفي)
         الرفض بيطلع في الكونسول كـ unhandled rejection. */
      if (vt.ready && vt.ready.catch) vt.ready.catch(() => { });
      if (vt.updateCallbackDone && vt.updateCallbackDone.catch) vt.updateCallbackDone.catch(() => { });
      vtPending = vt.finished.then(off, off);
    }
    catch (e) { window.bkVtActive = false; vtPending = null; swap(); }
    /* حماية من التباطؤ: لو المتصفح اتأخر في تشغيل الانتقال (تب مخفي،
       جهاز بطيء، أو رسم متجمد) الصفحة كانت هتفضل الشاشة القديمة لثانية
       كاملة — وده اللي بيخلي التنقّل "بايظ". هنا بعد 160ms من غيرSwap
       بنلغي الانتقال ونبدّل الصفحة فوراً (الحركة بتضيع بس السرعة بتربح). */
    if (vtGuard) clearTimeout(vtGuard);
    myGuard = setTimeout(() => {
      if (swapped) return;
      try { if (vt && vt.skipTransition) vt.skipTransition(); } catch (e) {}
      swap();
    }, 160);
    vtGuard = myGuard;
  } else { window.bkVtActive = false; vtPending = null; swap(); }
  if (p === "dashboard") { loadDashboard(); restartFeedTimer(); } else stopFeedTimer();
  if (p === "attendance") { syncAttControls(); loadRoster(); }
  if (p === "schedule") loadSchedulePage();
  if (p === "employees") { renderEmployees(); }
  if (p === "reports") { if (!$("repStart").value || S.rep.preset === "week") setPreset(S.rep.preset || "week"); else runReport(); }
  if (p === "fines") loadFines();
  if (p === "settings") loadSettings();
}
/* طبقة الحركة بتسألها: تشغّل دخول العناصر بعد ما الانتقال يخلص (أو في الفريم الجاي) */
function afterSwap(fn) {
  const pr = vtPending; vtPending = null;
  if (pr && pr.then) pr.then(fn, fn); else requestAnimationFrame(fn);
}
window.bankaiAfterSwap = afterSwap;

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
    const [me, emps, settings] = await Promise.all([api("/api/me"), api(`/api/employees?${wq()}`), api("/api/settings")]);
    S.role = me.role || "admin"; localStorage.setItem("bankai_role", S.role);
    applyRole();
    showApp();
    updateWeekBar();
    startLive();
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

/* ============================== week bar ============================== */
function updateWeekBar() {
  $("wkNum").textContent = weekLabel(S.week);
  $("wkRange").textContent = weekRangeText(S.week);
  $("wkPick").value = S.week;
  const now = S.week === weekStartOf(todayISO());
  qs("#wkBar .wk-cur").classList.toggle("now", now);
  $("wkNow").disabled = now;
}
/* لو التاريخ اللي المستخدم فتحه (يوم الحضور/الداشبورد) في أسبوع تاني، الشريط والموظفين بيتبعوه */
function adoptWeek(date) {
  const ws = weekStartOf(date);
  if (ws === S.week) return;
  S.week = ws; updateWeekBar();
  if (S.page === "reports" && S.rep.preset === "week") setPreset("week");
  refreshEmployees().catch(() => {});
}
function hasDrafts() { return S.draft.size > 0 || (S.sch && S.sch.draft.size > 0); }
function setWeek(ws) {
  ws = weekStartOf(ws);
  if (ws === S.week) return;
  if (hasDrafts() && !confirm("عندك تغييرات لم تُحفظ. هل تريد تجاهلها والانتقال لأسبوع تاني؟")) { updateWeekBar(); return; }
  S.draft.clear(); S.sch.draft.clear(); updateSaveBar(); updateSchSaveBar();
  S.week = ws; updateWeekBar();
  const end = addDays(ws, 6), t = todayISO();
  const fit = d => (d >= ws && d <= end) ? d : ((t >= ws && t <= end) ? t : ws);
  S.dash.date = fit(S.dash.date); S.att.date = fit(S.att.date);
  S.rep.preset = "week"; S.fines.preset = "week";
  refreshEmployees().catch(() => {});
  if (S.page === "dashboard") { loadDashboard(); loadFeed(); }
  else if (S.page === "attendance") { syncAttControls(); loadRoster(); }
  else if (S.page === "schedule") loadSchedule();
  else if (S.page === "reports") setPreset("week");
  else if (S.page === "fines") { setFinePreset("week"); loadFines(); }
}
on("wkPrev", "click", () => setWeek(addDays(S.week, -7)));
on("wkNext", "click", () => setWeek(addDays(S.week, 7)));
on("wkNow", "click", () => setWeek(todayISO()));
on("wkPick", "change", () => { if ($("wkPick").value) setWeek($("wkPick").value); else updateWeekBar(); });
on("schCopyPrev", "click", async () => {
  const prev = addDays(S.week, -7);
  if (S.sch.draft.size && !confirm("عندك تعديلات في الجدول لم تُحفظ وهتتجاهل. المتابعة؟")) return;
  if (!confirm(`نسخ جدول ${weekLabel(prev)} (الشيفتات والإجازات والبريك) فوق جدول ${weekLabel(S.week)}؟\nأي تعديل عملته في ${weekLabel(S.week)} هيتستبدل. الحضور والغرامات ما بتتأثرش.`)) return;
  try {
    const r = await api("/api/schedule/copy-week", { method: "POST", body: { from_week: prev, to_week: S.week } });
    S.sch.draft.clear(); await loadSchedule(); refreshEmployees().catch(() => {});
    toast(`تم نسخ جدول ${r.from.label} لـ ${r.to.label} (${r.copied} موظف).`, "ok");
  } catch (e) { toast(e.message, "err"); }
});

/* ============================== dashboard ============================== */
on("dashDate", "change", () => { S.dash.date = $("dashDate").value || todayISO(); loadDashboard(); });
on("dashPrev", "click", () => { S.dash.date = addDays(S.dash.date, -1); loadDashboard(); });
on("dashNext", "click", () => { S.dash.date = addDays(S.dash.date, 1); loadDashboard(); });
on("dashToday", "click", () => { S.dash.date = todayISO(); loadDashboard(); });

async function loadDashboard() {
  adoptWeek(S.dash.date);
  $("dashDate").value = S.dash.date;
  try { renderDashboard(await api(`/api/dashboard?attendance_date=${S.dash.date}`)); }
  catch (e) { toast(e.message, "err"); }
}

function renderDashboard(d) {
  const t = d.totals, date = d.date;
  $("dashSub").textContent = `${WD_AR[d.weekday] || ""} ${fmtDay(date, { day: "numeric", month: "long", year: "numeric" })} · ${weekLabel(d.week.start)}`;
  $("dashTrendSub").textContent = `${weekLabel(d.week.start)} · ${weekRangeText(d.week.start)}`;
  const kp = [
    ["مجدولون اليوم", t.scheduled, "brand", { date }], ["حاضر", t.present, "ok", { date, status: "Present" }],
    ["متأخر", t.late, "warn", { date, status: "Late" }], ["غائب", t.absent, "bad", { date, status: "Absent" }],
    ["بعذر", t.excused, "info", { date, status: "Excused" }], ["لم يُسجل", t.unmarked, "", { date, status: "none" }],
  ];
  const pct = v => t.scheduled ? Math.round(v * 100 / t.scheduled) : 0;
  $("dashKpis").innerHTML = kp.map(([l, v, c, go]) => `<button class="kpi ${c}" data-go="${esc(JSON.stringify(go))}" style="--p:${pct(v)}" title="${l}: ${v} من ${t.scheduled} مجدول"><span class="kl">${l}</span><span class="kv num${v ? "" : " is-zero"}">${v}</span><span class="ring${v ? "" : " is-zero"}"><b>${pct(v)}%</b></span></button>`).join("");
  if (S.lang === "en") {
    dashNote.innerHTML = '<span>On Leave Today: <b class="num">' + d.off_today + '</b> Employees</span><span>Week ' + weekNoOf(d.week.start) + ' Employees: <b class="num">' + d.active_total + '</b></span><span>Week ' + weekNoOf(d.week.start) + ' Fines: <b class="num">' + num(d.week_fines) + '</b> EGP</span>';
  } else {
  $("dashNote").innerHTML = `<span>في إجازة اليوم: <b class="num">${d.off_today}</b> موظف</span><span>موظفين ${weekLabel(d.week.start)}: <b class="num">${d.active_total}</b></span><span>غرامات ${weekLabel(d.week.start)}: <b class="num">${money(d.week_fines)}</b></span>`;
  }
  $("dashBranches").innerHTML = Object.entries(d.branches).map(([name, b]) => branchPanel(name, b, date)).join("");
  renderTrend(d.trend, date);
  $("dashWatch").innerHTML = d.watchlist.length
    ? `<table class="mini-tbl"><thead><tr><th>الموظف</th><th>غياب</th><th>تأخير</th></tr></thead><tbody>${d.watchlist.map(w => `<tr data-hist="${w.id}" style="cursor:pointer"><td>${whoHTML(w.name, `<b>${esc(w.name)}</b><br><small class="muted">${esc(w.branch)} · ${sh(w.shift)}</small>`)}</td><td class="num">${w.absent}</td><td class="num">${w.late}</td></tr>`).join("")}</tbody></table>`
    : `<p class="muted" style="padding:20px 0">مفيش غياب أو تأخير مسجل في الأسبوع ده.</p>`;
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
    // دخول متتابع لصفوف الفيد أول مرة الداشبورد بيفتح بس — التحديثات بعد كده بتنزل من غير رفرفة
    const firstIn = !S.feedAnimated && rows.length > 0;
    if (firstIn) S.feedAnimated = true;
    el.innerHTML = rows.length ? rows.map((e, i) => {
      const a = e.attendance, lbl = (ST[a.status] || [a.status])[0];
      const anim = (firstIn && i < 8 && (function(){ try { return !matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e2) { return true; } })()) ? ` style="animation:fdsRise .34s var(--fds-out) both;animation-delay:${i * 45}ms"` : "";
      return `<div class="feed-row"${anim}><div class="feed-ava" style="background:${AV[a.status] || "#94a3b8"}">${esc(e.name.trim()[0] || "؟")}</div><div class="who"><b>${esc(e.name)}</b><small>سجّل ${lbl} — ${esc(e.branch || "")}</small></div><span class="feed-tm">${relTime(a.updated_at)}</span></div>`;
    }).join("") : `<div class="empty">لا توجد تسجيلات بعد اليوم</div>`;
  } catch (e) { /* keep old feed on error */ }
}
function restartFeedTimer() { stopFeedTimer(); loadFeed(); feedTimer = setInterval(loadFeed, 30000); }
function stopFeedTimer() { if (feedTimer) { clearInterval(feedTimer); feedTimer = null; } }

function branchPanel(name, b, date) {
  const done = b.scheduled - b.unmarked, pct = b.scheduled ? Math.round(done * 100 / b.scheduled) : 0;
  const rows = b.shifts.map(s => `<tr data-go="${esc(JSON.stringify({ branch: name, shift: s.shift, date }))}"><td><b>${sh(s.shift)}</b></td><td class="num">${s.scheduled}</td><td class="num">${s.present}</td><td class="num">${s.late}</td><td class="num">${s.absent}</td><td class="num">${s.excused}</td><td class="num">${s.unmarked}</td><td class="bar">${stackBar(s)}</td></tr>`).join("");
  return `<div class="panel"><div class="panel-h"><div><h3>${esc(name)}</h3><p>${b.scheduled} موظف مجدول اليوم</p></div><button class="btn sm" data-go="${esc(JSON.stringify({ branch: name, date }))}">فتح الحضور</button></div>
    <div class="panel-b"><div class="bp-top"><div><strong class="num">${pct}%</strong> <span class="muted">اكتمل التسجيل</span></div></div>
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
  adoptWeek(date);
  $("attDayName").textContent = fmtDay(date, { weekday: "long", day: "numeric", month: "long" });
  try {
    const r = await api(`/api/roster?attendance_date=${date}`);
    if (date !== S.att.date) return;
    S.roster = r; S.rmap = new Map(r.employees.map(e => [e.id, e])); S.draft.clear(); S.stale.clear();
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
  return `<div class="arow${S.draft.has(e.id) ? " dirty" : ""}${S.stale.has(e.id) ? " stale" : ""}" data-id="${e.id}" data-st="${st}">
    <div class="a-who">${whoHTML(e.name, `<div class="a-name">${esc(e.name)}${contactHTML(e)}</div><div class="a-sub">${sub.join(" · ")}</div>`)}</div>
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
  $("attOffSum").textContent = S.lang === "en" ? `On leave today (${off.length})` : `في إجازة اليوم (${off.length})`;
  $("attOffList").innerHTML = off.map(e => `<span class="tag">${esc(e.name)} · ${esc(e.branch)}</span>`).join("");
}

function updateRowUI(e) {
  const row = qs(`.arow[data-id="${e.id}"]`, $("attList")); if (!row) return;
  const d = rowState(e), st = d.status || "";
  row.dataset.st = st; row.classList.toggle("dirty", S.draft.has(e.id));
  row.classList.toggle("stale", S.stale.has(e.id));
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
  S.stale.delete(e.id); /* المستخدم بيعدّل الصف بنفسه: التنبيه بتاع «اتغيّر من حد تاني» خلص */
  if (d.status === st) st = "";
  d.status = st;
  if (st === "Present") { d.fine = 0; d.reason = ""; }
  else if (st === "Excused") { d.fine = 0; }
  else if (st === "Late" || st === "Absent") { if (!d.fineTouched) d.fine = S.settings[st === "Late" ? "fine_late" : "fine_absent"] || 0; }
  else { d.fine = 0; d.reason = ""; d.note = ""; }
  maybeClean(e); updateRowUI(e); updateAttSummary(); updateSaveBar();
  /* نبضة على الصف نفسه: تأكيد بصري إن التسجيل وقع (الزرار نفسه بينبض بـ fdsPop) */
  pulse(qs(`.arow[data-id="${e.id}"]`, $("attList")), "just-set");
}

function markPresent(arr) {
  let n = 0;
  arr.forEach(e => {
    if (curStatus(e) !== "") return;
    const d = getDraft(e); d.status = "Present"; d.fine = 0; d.reason = ""; n++;
  });
  if (!n) { toast("كل الموظفين في العرض الحالي ليهم حالة بالفعل."); return; }
  renderAttendance(); updateSaveBar();
  /* نبضة جماعية على الصفوف اللي اتسجلت (قراءة layout واحدة للمجموعة كلها) */
  pulseAll(qsa("#attList .arow.dirty"), "just-set");
  toast(`تم تحديد ${n} موظف كحاضر. اضغط «حفظ الكل» للتأكيد.`, "ok");
}

function updateSaveBar() {
  const n = S.draft.size;
  $("saveBar").classList.toggle("hidden", n === 0);
  $("saveMsg").textContent = S.lang === "en" ? `${n} unsaved entry marks` : `${n} تسجيل لم يُحفظ بعد`;
  /* bump خفيف على العدّاد كل مرة العدد يتغيّر (الشريط نفسه ليه حركة دخول أصلًا) */
  if (n) pulse($("saveMsg"), "bump");
}

async function saveAttendance() {
  if (!S.draft.size) return;
  const items = [], invalid = [];
  for (const [id, d] of S.draft) {
    const fine = (d.status === "Late" || d.status === "Absent") ? Number(d.fine || 0) : 0;
    if (fine > 0 && !String(d.reason || "").trim()) invalid.push(id);
    /* بنبعت الـstamp اللي شفناه وقت التحميل: لو السجل اتعدّل بعده، السيرفر يرفض
       الحفظ بتعارض واضح بدل ما يكتب فوق شغل حد تاني بالغلط. لو شفناه من غير أي
       تسجيل، بنقول للسيرفر كده صريح عشان يمسك حالة إن حد سجّله في نفس اللحظة. */
    const seen = S.rmap.get(id)?.attendance;
    const stamp = seen ? (seen.updated_at || "") : "-";
    items.push({ employee_id: id, status: d.status, fine, reason: d.reason || "", note: d.note || "", expected_updated_at: stamp });
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
    const conflicts = r.errors.filter(x => x.conflict);
    /* بنسيب نسخة المستخدم زي ما هي على الصفوف اللي فشلت (سواء تعارض أو أي سبب
       تاني) عشان مايضيّعش شغله، وبنسيب التعارض نفسه للـsoft refresh يحدّث الصف
       بقيمة السيرفر الحالية. */
    conflicts.forEach(x => S.draft.delete(x.employee_id));
    for (const idStr of Object.keys(r.records)) {
      const id = +idStr; if (failed.has(id)) continue;
      const e = S.rmap.get(id); if (!e) continue;
      e.attendance = r.records[idStr]; S.draft.delete(id); S.stale.delete(id); updateRowUI(e);
    }
    updateAttSummary(); updateSaveBar();
    if (conflicts.length) {
      const x = conflicts[0];
      toast(`التسجيل اتعدّل من مستخدم تاني — ${x.name ? x.name + " · " : ""}جرى تحديث الصف بآخر نسخة. راجعها واحفظ تاني لو لسه عايز تعدّل.`, "err");
      loadRosterSoft();
    } else if (r.errors.length) {
      const x = r.errors[0];
      toast(`تعذر حفظ ${failed.size} موظف — ${x.name ? x.name + ": " : ""}${x.detail}`, "err");
    } else if (r.saved || r.cleared) {
      toast(`تم حفظ ${r.saved + r.cleared} تسجيل.`, "ok");
    }
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
on("saveDiscard", "click", () => { if (!S.draft.size) return; S.draft.clear(); S.stale.clear(); renderAttendance(); updateSaveBar(); });

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





// ---- dynamic translation observer ----
let __roTransTimer = null;
function scheduleTranslate() {
  clearTimeout(__roTransTimer);
  __roTransTimer = setTimeout(() => { try { window.__roTranslate && window.__roTranslate(); } catch (e) {} }, 400);
}
if (typeof MutationObserver !== "undefined") {
  try {
    const mo = new MutationObserver(() => scheduleTranslate());
    mo.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  } catch (e) {}
}
// initial
setTimeout(scheduleTranslate, 800);

window.addEventListener("beforeunload", ev => { if (anyDrafts()) { ev.preventDefault(); ev.returnValue = ""; } });
document.addEventListener("keydown", ev => {
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s" && S.page === "attendance") { ev.preventDefault(); saveAttendance(); return; }
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s" && S.page === "schedule") { ev.preventDefault(); schSave(); return; }
  if (ev.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
    /* اختصار البحث في كل الصفحات اللي فيها خانة بحث */
    const searchMap = { attendance: "attSearch", employees: "empSearch", schedule: "schSearch", reports: "repSearch", fines: "fineSearch" };
    const el = $(searchMap[S.page]);
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
  S.employees = await api(`/api/employees?${wq()}`);
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
      <td class="who">${whoHTML(e.name, `<button class="link" data-hist="${e.id}">${esc(e.name)}</button><small>${esc(e.employee_code || "بدون كود")}</small>`)}</td>
      <td class="br">${esc(e.branch)}${isAdmin() ? ` <button class="link br-mv" data-move="${e.id}" title="نقل لفرع ${esc(otherBranchOf(e.branch))} — بنفس الشيفت ويوم الإجازة والبريك" aria-label="نقل ${esc(e.name)} لفرع ${esc(otherBranchOf(e.branch))}">نقل</button>` : ""}</td><td>${sh(e.shift)}</td>
      <td><span class="tag ${e.employment_type === "FT" ? "brand" : ""}">${e.employment_type === "FT" ? "دوام كامل" : "دوام جزئي"}</span></td>
      <td>${WD_AR[e.day_off] || e.day_off}</td>
      <td>${e.phone ? `<a class="num" href="tel:${esc(e.phone)}">${esc(e.phone)}</a>` : `<span class="muted">—</span>`}</td>
      <td><span class="tag ${e.active ? "ok" : "off"}">${e.active ? "نشط" : "موقوف"}</span></td>
      <td><div class="row-actions"><button class="btn sm" data-hist="${e.id}" title="سجل الحضور"><svg class="i"><use href="#i-clock"/></svg>السجل</button>${isAdmin() ? `<button class="btn sm" data-fine="${e.id}" title="تسجيل غرامة إدارية على ${esc(e.name)}"><svg class="i"><use href="#i-plus"/></svg>غرامة</button>` : ""}${adminActs}</div></td></tr>`;
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
  const fn = ev.target.closest("[data-fine]"); if (fn) { openFineDialog(+fn.dataset.fine); return; }
  const mv = ev.target.closest("[data-move]");
  if (mv) { const e = S.employees.find(x => x.id === +mv.dataset.move); if (e) await moveEmployeeBranch(e.id, otherBranchOf(e.branch)); return; }
  const de = ev.target.closest("[data-deact]");
  if (de) { if (confirm("سيتم إيقاف الموظف مع الاحتفاظ بسجل حضوره السابق. هل تريد المتابعة؟")) bulkAction([+de.dataset.deact], "deactivate"); return; }
  const ac = ev.target.closest("[data-act]"); if (ac) bulkAction([+ac.dataset.act], "activate");
});

async function bulkAction(ids, action, extra = {}) {
  try {
    const r = await api(`/api/employees/bulk?${wq()}`, { method: "POST", body: { ids, action, ...extra } });
    toast(action === "update" ? `تم تعديل ${r.updated} موظف.` : action === "activate" ? `تم تفعيل ${r.updated} موظف.` : `تم إيقاف ${r.updated} موظف.`, "ok");
    S.emp.selected.clear(); await refreshEmployees();
    return true;
  } catch (e) { toast(e.message, "err"); return false; }
}

/* ========================== نقل موظف لفرع آخر ==========================
   الفرع هو الحاجة الوحيدة اللي بتتغيّر: الشيفت، يوم الإجازة، البريك، نوع التعاقد،
   والبريك المثبّت كلهم بيفضلوا زي ما هما، وسجل الحضور والغرامات بيفضل محفوظ. */
const otherBranchOf = b => (b === "QCD 1" ? "QCD 2" : "QCD 1");

async function moveEmployeeBranch(id, to) {
  const e = S.employees.find(x => x.id === id) || S.sch.base.get(id) || null;
  const name = e ? e.name : "الموظف";
  if (S.sch.draft && S.sch.draft.size) { toast("فيه تعديلات في الجدول لسه مش محفوظة — احفظها الأول وبعدين انقل الموظف.", "err"); return false; }
  if (!confirm(`نقل «${name}» من ${e ? e.branch : ""} إلى ${to}؟\n\nهيتنقل بنفس الشيفت ويوم الإجازة والبريك ونوع التعاقد، وسجل الحضور والغرامات بتاعه هيفضل محفوظ.`)) return false;
  try {
    await api(`/api/employees/${id}/move`, { method: "POST", body: { branch: to } });
    toast(`تم نقل ${name} إلى ${to} بنفس الشيفت والإجازة.`, "ok");
    await refreshEmployees();
    if (S.sch.data && S.page === "schedule") await loadSchedule(true);
    return true;
  } catch (err) { toast(err.message, "err"); return false; }
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
    await api(id ? `/api/employees/${id}?${wq()}` : `/api/employees?${wq()}`, { method: id ? "PUT" : "POST", body: data });
    $("empDialog").close(); toast("تم حفظ بيانات الموظف.", "ok");
    await refreshEmployees();
  } catch (err) { $("fError").textContent = err.message; }
});

on("empExport", "click", () => download(`/api/employees/export.csv?${wq()}`, `bankai_employees_${weekLabel(S.week).replace(" ", "")}.csv`).catch(e => toast(e.message, "err")));

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
    const r = await api(`/api/employees/import?${wq()}`, { method: "POST", body: { rows, update_existing: $("impUpdate").checked, dry_run: dry } });
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

/* دروب مينيو السجل = الأسابيع بس، وكل أسبوع له تسجيلات في السيستم بيظهر لوحده.
   السيرفر بيرجّع weeks من كل التاريخ، وهنا بنملي القايمة ونختار افتراضيًا
   الأسبوع اللي فيهجا النهارده لو موجود، وإلا الأحدث. */
function histWeekOptions(weeks) {
  const cur = weekStartOf(todayISO()), starts = weeks.map(w => w.start);
  if (!starts.includes(cur)) starts.unshift(cur);
  return starts.map(ws => {
    const y = +addDays(ws, 6).slice(0, 4), no = weekNoOf(ws);
    return `<option value="${ws}">Week ${no}${y !== new Date().getFullYear() ? " · " + y : ""} · ${weekRangeText(ws)}</option>`;
  }).join("");
}

async function openHistory(id) {
  S.hist.id = id;
  $("histTitle").textContent = "…"; $("histSub").textContent = ""; $("histStats").innerHTML = ""; $("histRows").innerHTML = ""; $("histRange").innerHTML = "";
  if (!$("histDialog").open) $("histDialog").showModal();
  try {
    const r = await api(`/api/reports/employee/${S.hist.id}`);
    $("histRange").innerHTML = histWeekOptions(r.weeks || []);
    await loadHistory();
  } catch (err) { toast(err.message, "err"); }
}
async function loadHistory() {
  const ws = weekStartOf($("histRange").value), start = ws, end = addDays(ws, 6);
  try {
    const r = await api(`/api/reports/employee/${S.hist.id}?start=${start}&end=${end}`);
    const e = r.employee, s = r.stats;
    $("histTitle").textContent = e.name;
    $("histSub").innerHTML = `${esc(e.branch)} · ${sh(e.shift)} · ${esc(e.employee_code || "بدون كود")} · إجازة ${WD_AR[e.day_off] || e.day_off}${e.active ? "" : " · موقوف"}`;
    $("histStats").innerHTML = `<span class="tag">تسجيلات: ${s.total}</span><span class="tag ok">حضور: ${s.present}</span><span class="tag warn">تأخير: ${s.late}</span><span class="tag bad">غياب: ${s.absent}</span><span class="tag info">بعذر: ${s.excused}</span><span class="tag admin">غرامات إدارية: ${s.standalone || 0} — ${money(s.standalone_total || 0)}</span>`;
    $("histRows").innerHTML = r.records.map(a => `<tr><td>${WD_AR[a.weekday] || ""} ${fmtDay(a.attendance_date, { day: "numeric", month: "short" })}</td><td><span class="tag ${a.status}">${ST[a.status]?.[0] || a.status}</span></td><td>${esc(a.reason) || "—"}</td><td>${esc(a.note) || "—"}</td></tr>`).join("");
    $("histEmpty").classList.toggle("hidden", r.records.length > 0);
  } catch (err) { toast(err.message, "err"); }
}

/* ============================== reports ============================== */
function setPreset(p) {
  const t = todayISO(), d = new Date(); let s = t, e = t;
  if (p === "week") { s = S.week; e = addDays(S.week, 6); }
  else if (p === "yesterday") s = e = addDays(t, -1);
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
  const rate = r.total ? Math.round((r.present + r.late) * 100 / r.total) : 0;
  const k = [["إجمالي التسجيلات", r.total, "brand"], ["حضور", r.present, "ok"], ["تأخير", r.late, "warn"], ["غياب", r.absent, "bad"], ["بعذر", r.excused, "info"], ["نسبة الحضور", rate + "%", "ok"]];
  $("repKpis").innerHTML = k.map(([l, v, c]) => `<div class="kpi ${c}"><span>${l}</span><strong class="${typeof v === "number" ? "num" : ""}" style="${typeof v === "number" ? "" : "font-size:22px"}">${v}</strong></div>`).join("");
  const names = Object.keys(r.by_branch).sort();
  $("repBranches").innerHTML = names.length ? names.map(n => {
    const b = r.by_branch[n], c = { scheduled: b.total, present: b.present, late: b.late, absent: b.absent, excused: b.excused, unmarked: 0 };
    return `<div class="panel br-card"><h4><span>${esc(n)}</span><span class="muted num">${b.total} تسجيل</span></h4>${stackBar(c, "lg")}<div style="margin:10px 0">${legend({ ...c, unmarked: undefined })}</div></div>`;
  }).join("") : "";
  $("repDays").innerHTML = r.by_day.map(d => `<tr data-go="${esc(JSON.stringify({ date: d.date, branch: $("repBranch").value }))}" style="cursor:pointer"><td>${fmtDay(d.date, { weekday: "long", day: "numeric", month: "short" })}</td><td class="num">${d.present}</td><td class="num">${d.late}</td><td class="num">${d.absent}</td><td class="num">${d.excused}</td><td style="width:140px">${stackBar({ scheduled: d.total, present: d.present, late: d.late, absent: d.absent, excused: d.excused, unmarked: 0 })}</td></tr>`).join("");
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
    return `<tr data-hist="${e.id}" style="cursor:pointer"><td class="who">${whoHTML(e.name, `<b>${esc(e.name)}</b><small>${esc(e.employee_code || "بدون كود")} · ${sh(e.shift)}</small>`)}</td><td>${esc(e.branch)}</td><td class="num">${e.present}</td><td class="num">${e.late}</td><td class="num">${e.absent}</td><td class="num">${e.excused}</td><td><span class="rate"><b class="num">${e.rate}%</b><span class="stack"><i class="${cls}" style="width:${e.rate}%"></i></span></span></td></tr>`;
  }).join("");
  $("repEmpty").classList.toggle("hidden", total > 0);
  renderPager($("repPager"), total, S.rep, renderReportRows);
}


/* ============================== fines (الغرامات) ==============================
   مكان واحد لكل الغرامات في الفرعين: التفاصيل الكاملة (الموظف، الفرع، الشيفت،
   النوع، اليوم، الغرامة، السبب، الملاحظة) + إجماليات الفرعين والتفصيل الشهري.
   التجميع كله على السيرفر من /api/fines، وهنا الفلترة بالتاريخ/الفرع/النوع وترتيب الجدول. */
const fineQuery = () => {
  const s = $("fineStart").value, e = $("fineEnd").value, b = $("fineBranch").value, k = $("fineKind").value;
  const parts = [];
  if (s) parts.push("start=" + s);
  if (e) parts.push("end=" + e);
  if (b) parts.push("branch=" + encodeURIComponent(b));
  if (k) parts.push("status=" + k);
  return { qs: parts.join("&"), tag: `${s || "all"}_${e || "all"}` };
};

function setFinePreset(p) {
  const t = todayISO(), d = new Date(); let s = t, e = t;
  if (p === "week") { s = S.week; e = addDays(S.week, 6); }
  else if (p === "lastmonth") { s = isoOf(new Date(d.getFullYear(), d.getMonth() - 1, 1)); e = isoOf(new Date(d.getFullYear(), d.getMonth(), 0)); }
  else if (p === "30") s = addDays(t, -29);
  else if (p === "90") s = addDays(t, -89);
  else if (p === "year") s = isoOf(new Date(d.getFullYear(), 0, 1));
  else if (p === "all") s = e = "";
  else s = isoOf(new Date(d.getFullYear(), d.getMonth(), 1)); /* هذا الشهر */
  S.fines.preset = p; $("fineStart").value = s; $("fineEnd").value = e;
  qsa("#finePresets button").forEach(b => b.classList.toggle("on", b.dataset.p === p));
}

on("finePresets", "click", ev => { const b = ev.target.closest("button"); if (b) { setFinePreset(b.dataset.p); loadFines(); } });
["fineStart", "fineEnd"].forEach(id => on(id, "change", () => { S.fines.preset = ""; qsa("#finePresets button").forEach(b => b.classList.remove("on")); }));
on("fineRun", "click", loadFines);
on("fineBranch", "change", loadFines);
on("fineBranchSeg", "click", ev => {
  const b = ev.target.closest("button"); if (!b) return;
  $("fineBranch").value = b.dataset.branch; /* المفتاح بيكتب على نفس الفلتر الأصلي عشان الاتنين يفضلوا متزامنين */
  loadFines();
});
on("fineKind", "change", loadFines);
on("fineSearch", "input", debounce(() => { S.fines.page = 1; renderFineRows(); }, 150));
bindSort("#page-fines th.sort", "fsort", S.fines, renderFineRows);
on("fineExport", "click", () => {
  const { qs: query, tag } = fineQuery();
  download(`/api/fines/export.csv${query ? "?" + query : ""}`, `bankai_fines_${tag}.csv`).catch(e => toast(e.message, "err"));
});

async function loadFines() {
  /* أول فتح للتاب: نعبّي الفترة الافتراضية (الأسبوع المختار) قبل ما نطلب الداتا */
  if (S.fines.preset === "week") setFinePreset("week");
  else if (!$("fineStart").value && !$("fineEnd").value && S.fines.preset && S.fines.preset !== "all") setFinePreset(S.fines.preset);
  const { qs: query } = fineQuery();
  try {
    S.fines.data = await api(`/api/fines${query ? "?" + query : ""}`);
    S.fines.page = 1;
    renderFines();
  } catch (e) { toast(e.message, "err"); }
}

function finePeriodLabel(d) {
  if (!d.start && !d.end) return "كل الفترات";
  const f = s => fmtDay(s, { day: "numeric", month: "short" });
  return `من ${d.start ? f(d.start) : "البداية"} إلى ${d.end ? f(d.end) : "اليوم"}`;
}

function renderFines() {
  const d = S.fines.data; if (!d) return;
  $("fineSub").textContent = `${finePeriodLabel(d)} · ${d.branch || "كل الفروع"}${d.status ? ` · ${(ST[d.status] || [d.status])[0]} بس` : ""}`;
  const kp = [
    ["إجمالي الغرامات", money(d.total), "brand", true], ["عدد الغرامات", num(d.count), "", false],
    ["غرامات تأخير", money(d.late_total), "warn", true], ["غرامات غياب", money(d.absent_total), "bad", true],
    ["غرامات إدارية", money(d.standalone_total), "admin", true],
    ["متوسط الغرامة", money(d.avg), "info", true], ["أكبر غرامة", money(d.max), "ok", true],
  ];
  $("fineKpis").innerHTML = kp.map(([l, v, c, big]) =>
    `<div class="kpi ${c}"><span>${l}</span><strong class="${big ? "" : "num"}" style="font-size:${big ? 20 : 26}px">${v}</strong></div>`).join("");
  const picked = $("fineBranch").value;
  /* مفتاح الفروع: نفس فلتر قائمة «الفرع» بالظبط، والعدّاد لكل فرع من by_branch */
  const brCount = b => (d.by_branch.find(x => x.branch === b) || {}).count || 0;
  qsa("#fineBranchSeg button").forEach(b => {
    b.classList.toggle("on", b.dataset.branch === picked);
    qs("small", b).textContent = b.dataset.branch ? brCount(b.dataset.branch) : d.by_branch.reduce((s, x) => s + x.count, 0);
  });
  $("fineBranches").innerHTML = d.by_branch.filter(b => !picked || b.branch === picked).map(b => {
    const share = d.total ? Math.round(b.total * 100 / d.total) : 0;
    const seg = (cls, n, tip) => n ? `<i class="s-${cls}" style="width:${n * 100 / b.count}%" title="${tip}: ${n}"></i>` : "";
    return `<div class="panel br-card"><h4><span>${esc(b.branch)}</span><span class="muted num">${b.count} غرامة</span></h4>
      <div class="bp-top"><div><strong class="num">${money(b.total)}</strong> <span class="muted">إجمالي الغرامات</span></div>${picked ? "" : `<span class="muted num">${share}% من الإجمالي</span>`}</div>
      ${b.count ? `<div class="stack lg">${seg("late", b.late, "تأخير")}${seg("absent", b.absent, "غياب")}${seg("admin", b.standalone, "غرامة إدارية")}</div><div class="legend" style="margin-top:10px"><span><i class="dot warn"></i>تأخير <b class="num">${b.late}</b> — ${money(b.late_total)}</span><span><i class="dot bad"></i>غياب <b class="num">${b.absent}</b> — ${money(b.absent_total)}</span><span><i class="dot admin"></i>إدارية <b class="num">${b.standalone}</b> — ${money(b.standalone_total)}</span></div>` : `<p class="muted">مفيش غرامات في الفترة دي.</p>`}</div>`;
  }).join("");
  $("fineWeeks").innerHTML = (d.by_week || []).map(w => `<tr><td><b>${esc(w.label)}</b></td><td>${weekRangeText(w.start)}</td><td class="num">${w.count}</td><td class="num">${w.late} — ${money(w.late_total)}</td><td class="num">${w.absent} — ${money(w.absent_total)}</td><td class="num">${w.standalone} — ${money(w.standalone_total)}</td><td class="num"><b>${money(w.total)}</b></td></tr>`).join("");
  $("fineWeeksBox").classList.toggle("hidden", !(d.by_week || []).length);
  $("fineMonths").innerHTML = d.by_month.map(m => `<tr><td>${fmtDay(m.month + "-01", { month: "long", year: "numeric" })}</td><td class="num">${m.count}</td><td class="num">${m.late}</td><td class="num">${m.absent}</td><td class="num">${m.standalone} — ${money(m.standalone_total)}</td><td class="num">${money(m.total)}</td></tr>`).join("");
  $("fineMonthsBox").classList.toggle("hidden", !d.by_month.length);
  renderFineRows();
}

function renderFineRows() {
  const d = S.fines.data; if (!d) return;
  const q = $("fineSearch").value.trim().toLowerCase();
  const list = d.items.filter(x => !q || `${x.name} ${x.employee_code}`.toLowerCase().includes(q));
  sortRows(list, S.fines, (x, k) => x[k] ?? "");
  markSort("#page-fines th.sort", "fsort", S.fines);
  const total = list.length;
  $("fineSub2").textContent = d.truncated
    ? `أحدث ${d.items.length} غرامة من إجمالي ${d.count} — ضيّق الفترة لو محتاج السجل كامل`
    : (total ? `${total} غرامة بالتفاصيل` : "");
  $("fineRows").innerHTML = list.slice((S.fines.page - 1) * S.fines.size, S.fines.page * S.fines.size).map(x => {
    const st = ST[x.status] || [x.status, ""];
    /* الغرامات الإدارية بس هي اللي ليها إجراءات (تعديل/مسح) — تأخير والغياب بيتبعتوا من الحضور */
    const admin = x.source === "standalone" && isAdmin()
      ? `<div class="row-actions no-print"><button class="btn sm" data-fedit="${x.id}" title="تعديل الغرامة"><svg class="i"><use href="#i-edit"/></svg></button><button class="btn sm danger" data-fdel="${x.id}" title="مسح الغرامة"><svg class="i"><use href="#i-trash"/></svg></button></div>`
      : "";
    return `<tr data-hist="${x.employee_id}" style="cursor:pointer"><td class="who">${whoHTML(x.name, `<b>${esc(x.name)}</b><small>${esc(x.employee_code || "بدون كود")} · ${sh(x.shift)} · ${esc(x.employment_type)}</small>`)}</td><td>${esc(x.branch)}</td><td>${WD_AR[x.weekday] || ""} ${fmtDay(x.date, { day: "numeric", month: "short" })} <small class="muted">W${x.week}</small></td><td><span class="tag ${st[1]}">${st[0]}</span></td><td class="num"><b>${money(x.fine)}</b></td><td>${esc(x.reason) || "—"}</td><td>${esc(x.note) || "—"}</td><td class="no-print">${admin}</td></tr>`;
  }).join("");
  $("fineEmpty").classList.toggle("hidden", total > 0);
  renderPager($("finePager"), total, S.fines, renderFineRows);
}

/* ================== الغرامات الإدارية المستقلة ==================
   غرامة على موظف في أي تاريخ — من غير تسجيل أي حالة حضور.
   الكتابة للأدمن بس، والقراءة لأي مستخدم مسجّل. */
function fineEmpOptions() {
  const list = [...S.employees].sort((a, b) =>
    (b.active ? 1 : 0) - (a.active ? 1 : 0) || a.name.localeCompare(b.name, "ar"));
  return list.map(e => `<option value="${e.id}">${esc(e.name)}${e.employee_code ? " — " + esc(e.employee_code) : ""} — ${esc(e.branch)}${e.active ? "" : " (موقوف)"}</option>`).join("");
}

function openFineDialog(employeeId = null, fineId = null) {
  if (!isAdmin()) return toast("العملية دي متاحة للأدمن فقط.", "err");
  $("fineError").textContent = "";
  $("fineId").value = fineId || "";
  $("fineEmpSel").innerHTML = fineEmpOptions();
  $("fineDate").value = todayISO();
  $("fineAmount").value = "";
  $("fineReason").value = "";
  $("fineNote").value = "";
  if (fineId) {
    /* تعديل: الموظف مش بيتغير — بنثبّ الاختيار ونقفله */
    const it = (S.fines.data && S.fines.data.items || []).find(x => x.id === fineId && x.source === "standalone");
    if (!it) return toast("الغرامة مش موجودة في الصفحة دي.", "err");
    $("fineTitle").textContent = "تعديل غرامة إدارية";
    $("fineSub").textContent = `${it.name} · ${it.branch}`;
    $("fineEmpSel").value = it.employee_id;
    $("fineEmpSel").disabled = true;
    $("fineDate").value = it.date;
    $("fineAmount").value = it.fine;
    $("fineReason").value = it.reason || "";
    $("fineNote").value = it.note || "";
  } else {
    $("fineTitle").textContent = "تسجيل غرامة إدارية";
    $("fineSub").textContent = "غرامة على موظف في أي تاريخ — من غير تسجيل حالة حضور";
    $("fineEmpSel").disabled = false;
    if (employeeId) $("fineEmpSel").value = employeeId;
  }
  $("fineDialog").showModal();
  $("fineAmount").focus();
}

on("fineForm", "submit", async ev => {
  ev.preventDefault();
  const id = $("fineId").value;
  const empSel = $("fineEmpSel");
  const body = {
    employee_id: +empSel.value,
    fine_date: $("fineDate").value,
    amount: parseFloat($("fineAmount").value),
    reason: $("fineReason").value.trim(),
    note: $("fineNote").value.trim(),
  };
  /* التحقق كله هنا مش Validity API: عشان الرسايل تطلع بالعربي زي باقي التطبيق
     مش رسائل المتصفح اللي بتختلف حسب لغة المتصفح. */
  if (!body.employee_id) return ($("fineError").textContent = "اختر الموظف.");
  if (!body.fine_date) return ($("fineError").textContent = "اختر تاريخ الغرامة.");
  if (!(body.amount > 0)) return ($("fineError").textContent = "قيمة الغرامة لازم تكون أكبر من صفر.");
  if (!body.reason) return ($("fineError").textContent = "اكتب سبب الغرامة.");
  try {
    if (id) {
      await api(`/api/fines/manual/${id}`, { method: "PUT", body: { fine_date: body.fine_date, amount: body.amount, reason: body.reason, note: body.note } });
      toast("تم تعديل الغرامة الإدارية.", "ok");
    } else {
      await api("/api/fines/manual", { method: "POST", body });
      toast("تم تسجيل الغرامة الإدارية.", "ok");
    }
    $("fineDialog").close();
    await loadFines();
  } catch (e) { $("fineError").textContent = e.message; }
});

on("fineCancel", "click", () => $("fineDialog").close());
on("fineClose", "click", () => $("fineDialog").close());
on("fineAdd", "click", () => openFineDialog());
on("histFineAdd", "click", () => openFineDialog(S.hist.id));

on("fineRows", "click", async ev => {
  const del = ev.target.closest("[data-fdel]");
  if (del) {
    ev.stopPropagation();
    const id = +del.dataset.fdel;
    if (!confirm("تمسح الغرامة الإدارية دي نهائيًا؟")) return;
    try {
      await api(`/api/fines/manual/${id}`, { method: "DELETE" });
      toast("تم مسح الغرامة الإدارية.", "ok");
      await loadFines();
    } catch (e) { toast(e.message, "err"); }
    return;
  }
  const ed = ev.target.closest("[data-fedit]");
  if (ed) { ev.stopPropagation(); openFineDialog(null, +ed.dataset.fedit); return; }
});


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
    const r = await api(`/api/schedule?branch=${encodeURIComponent(S.sch.branch)}&${wq()}`);
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
      <td class="who">${whoHTML(e.name, `<b>${esc(e.name)}</b><small>${esc(e.employee_code || "")}</small>`)}</td>
      <td class="c"><span class="tag ${e.employment_type === "FT" ? "brand" : ""}">${e.employment_type}</span></td>
      <td>${WD_AR[e.day_off] || e.day_off}${e.off_locked ? ' <span class="tag" title="يوم الإجازة مثبّت">مثبّت</span>' : ""}</td>
      <td>${e.break_start ? `<span class="num">${fmt12hm(e.break_start)}</span>` : `<span class="muted">—</span>`}</td>
      <td class="acts no-print"></td></tr>`;
  }
  const movedTo = otherBranchOf(e.branch);
  const moves = all.filter(s => s !== e.shift).map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join("")
    + `<option value="branch:${esc(movedTo)}">نقل لفرع ${esc(movedTo)}</option>`;
  return `<tr data-id="${e.id}" class="${S.sch.draft.has(e.id) ? "dirty" : ""}">
    <td class="who">${whoHTML(e.name, `<b>${esc(e.name)}</b><small>${esc(e.employee_code || "")}</small>`)}</td>
    <td class="c"><span class="tag ${e.employment_type === "FT" ? "brand" : ""}">${e.employment_type}</span></td>
    <td><select class="sel f-off${dirty.day_off ? " chg" : ""}" aria-label="يوم الإجازة">${WEEKDAYS.map(d => `<option value="${d}" ${d === e.day_off ? "selected" : ""}>${WD_AR[d]}</option>`).join("")}</select></td>
    <td><select class="sel f-bs${"break_start" in dirty ? " chg" : ""}" aria-label="ساعة البريك">${schBreakOptions(e.shift, e.break_start || "")}</select></td>
    <td class="acts no-print"><button type="button" class="btn sm icon pin${e.off_locked ? " on" : ""}" data-pin title="${e.off_locked ? "يوم الإجازة مثبّت (التوزيع التلقائي مش هيغيّره). اضغط للإلغاء" : "ثبّت يوم الإجازة عشان التوزيع التلقائي ما يغيّروش"}" aria-pressed="${!!e.off_locked}"><svg class="i"><use href="#i-pin"/></svg></button><select class="sel tiny f-mv" aria-label="نقل لشيفت آخر أو لفرع تاني"><option value="">نقل…</option>${moves}</select><button type="button" class="btn sm icon danger" data-del title="إزالة من الجدول" aria-label="إزالة ${esc(e.name)}"><svg class="i"><use href="#i-x"/></svg></button></td></tr>`;
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
    <div class="pastebox hidden"><textarea class="inp paste-ta" placeholder="${S.lang === "en" ? "One name per line. You can paste a column from Excel. If you write PT after the name (tab or comma) it is recorded as part-time." : "اسم في كل سطر. ممكن تلصق عمود من Excel. ولو كتبت PT بعد الاسم (تاب أو فاصلة) بيتسجل بارت تايم."}"></textarea><div class="row" style="margin-top:8px"><button class="btn primary sm paste-go">إضافة كل الأسماء</button><button class="btn sm paste-cancel">إلغاء</button></div></div>
    ${isAdmin() ? `<div class="addrow no-print"><input class="inp add-name" list="schNames" placeholder="اكتب اسم موظف واضغط Enter لإضافته" autocomplete="off" aria-label="اسم موظف جديد">
      <select class="sel add-type" aria-label="النوع"><option value="FT" ${def.type === "FT" ? "selected" : ""}>دوام كامل</option><option value="PT" ${def.type === "PT" ? "selected" : ""}>دوام جزئي</option></select>
      <select class="sel add-day" aria-label="يوم الإجازة"><option value="">إجازة: تلقائي</option>${WEEKDAYS.map(d => `<option value="${d}" ${def.day === d ? "selected" : ""}>${WD_AR[d]}</option>`).join("")}</select>
      <button class="btn primary add-go"><svg class="i"><use href="#i-plus"/></svg>إضافة</button></div>` : ""}</section>`;
}
function renderTotals(emps) {
  const total = emps.length, off = {}; WEEKDAYS.forEach(d => off[d] = 0);
  emps.forEach(e => { if (off[e.day_off] !== undefined) off[e.day_off]++; });
  const today = S.week === weekStartOf(todayISO()) ? schToday() : ""; /* نعلّم النهارده بس في الأسبوع الحالي */
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
  $("schPrintTitle").textContent = S.lang === "en"
    ? `Shift schedule — ${S.sch.branch} — ${weekLabel(S.week)}`
    : `جدول الشيفتات — ${S.sch.branch} — ${weekLabel(S.week)}`;
  $("schPrintSub").textContent = weekRangeText(S.week);
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
  $("schSaveMsg").textContent = S.lang === "en" ? `${n} employees with unsaved changes` : `${n} موظف عليهم تعديلات لم تُحفظ بعد`;
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
    const r = await api("/api/schedule/apply", { method: "POST", body: { items, week: S.week } });
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
S.simp = { b64: "", name: "", plan: null, decisions: {}, filter: "", deact: false, ack: false,
           week: "", weeks: null, isNew: false, busy: false };

function simpFmt(f, v) { return f === "day_off" ? (WD_AR[v] || v) : f === "employment_type" ? (v === "FT" ? "فول تايم" : "بارت تايم") : v; }

/* ---------------- اختيار أسبوع الاستيراد ---------------- */

/** يجيب الأسابيع الموجودة (مرة كل فتح للدايلوج). */
async function simpWeeksLoad() {
  const m = S.simp;
  try {
    const r = await api("/api/schedule/weeks");
    m.weeks = r.weeks || [];
    m.today = r.today || null;
  } catch (e) {
    m.weeks = m.weeks || [];
  }
}

/** هل الأسبوع ده موجود فعلًا (ليه صفوف)؟ */
function simpWeekExists(ws) {
  return (S.simp.weeks || []).some(w => w.start === ws);
}

/** أقرب أسبوع قبله (أو بعده لو مفيش) — ده اللي الأسبوع الجديد بيتنسخ منه. */
function simpWeekSource(ws) {
  const starts = (S.simp.weeks || []).map(w => w.start).sort();
  const before = starts.filter(s => s < ws).pop();
  return before || starts.find(s => s > ws) || "";
}

function simpWeekInfoHtml() {
  const m = S.simp, ws = m.week;
  if (!ws) return "";
  const label = weekLabel(ws), range = weekRangeText(ws);
  if (m.isNew) {
    const src = simpWeekSource(ws);
    const tag = m.createdNow
      ? `<span class="tag ok">${bi("أسبوع جديد ✔", "New week ✔")}</span>`
      : `<span class="tag brand">${bi("أسبوع جديد", "New week")}</span>`;
    const how = m.createdNow ? bi("اتعمل", "created") : bi("هيتعمل", "will be created");
    const srcTxt = src
      ? `<b>${esc(weekLabel(src))}</b> (${esc(weekRangeText(src))})`
      : bi("بيانات الموظفين الحالية", "the current employee data");
    return `${tag}<b>${esc(label)}</b> · <span class="num">${esc(range)}</span>
      <span class="muted">— ${how} ${bi("بنفس طريقة الأسابيع: نسخة من", "the same way as the other weeks: a copy of")} ${srcTxt}${bi("، وتقدر تعدّله بعده عادي.", ", and you can edit it afterwards as usual.")}</span>`;
  }
  const w = (m.weeks || []).find(x => x.start === ws);
  return `<span class="tag ok">${bi("موجود", "Exists")}</span> <b>${esc(label)}</b> · <span class="num">${esc(range)}</span>
    <span class="muted">${w ? `— ${w.employees} ${bi("موظف", "employees")}` : ""}</span>`;
}

function simpWeeksRender() {
  const m = S.simp;
  const starts = new Set((m.weeks || []).map(w => w.start));
  starts.add(S.week);                                  // الأسبوع المفتوح دايمًا موجود كخيار
  if (m.today && m.today.start) starts.add(m.today.start);
  if (m.week) starts.add(m.week);
  const list = [...starts].sort();

  const sel = $("schImpWeek");
  sel.innerHTML = list.map(ws => {
    const w = (m.weeks || []).find(x => x.start === ws);
    const bits = [weekLabel(ws), weekRangeText(ws)];
    if (ws === S.week) bits.push(bi("المفتوح", "Open"));
    else if (w) bits.push(`${w.employees} ${bi("موظف", "employees")}`);
    return `<option value="${ws}" ${ws === m.week ? "selected" : ""}>${esc(bits.join(" · "))}</option>`;
  }).join("") + `<option value="__new__" ${m.isNew ? "selected" : ""}>${bi("➕ أسبوع جديد…", "➕ New week…")}</option>`;

  $("schImpWeekDate").classList.toggle("hidden", !m.isNew);
  if (m.isNew) $("schImpWeekDate").value = m.week || "";
  $("schImpWeekNote").textContent = m.isNew ? bi("أسبوع جديد", "New week") : bi("الأسبوع المفتوح افتراضيًا", "The open week by default");
  $("schImpWeekInfo").innerHTML = simpWeekInfoHtml();
}

/** يختار أسبوع ويحدّث المعاينة لو فيه ملف متحمّل. */
async function simpPickWeek(ws, isNew) {
  const m = S.simp;
  m.week = ws;
  m.isNew = !!isNew;
  m.createdNow = false;
  simpWeeksRender();
  if (m.b64) await simpPreview();
}

/** إعادة حساب المعاينة (بعد تغيير الأسبوع أو قرار). */
async function simpPreview() {
  const m = S.simp;
  if (!m.b64 || m.busy) return;
  m.busy = true;
  $("schImpBody").innerHTML = '<p class="muted">جاري قراءة الشيت…</p>';
  $("schImpRun").disabled = true;
  try {
    m.plan = await simpRun(false);
    m.decisions = {};
    const pw = m.plan && m.plan.week;
    if (pw && pw.start) {
      // خلّي قائمة الأسابيع محدّثة: المعاينة نفسها بتعمل الأسبوع الجديد
      if (!(m.weeks || []).some(w => w.start === pw.start)) {
        m.weeks = [...(m.weeks || []), { ...pw, employees: 0 }].sort((a, b) => (a.start < b.start ? -1 : 1));
      }
      if (m.plan.week_created) m.createdNow = true;
      if (m.plan.week_created) await simpWeeksLoad();   // نجيب العدد الحقيقي للموظفين بعد إنشاء الأسبوع
    }
    simpRender();
  } catch (e) {
    $("schImpBody").innerHTML = `<div class="empty"><b>${esc(e.message)}</b></div>`;
  } finally { m.busy = false; }
}

async function simpRun(apply) {
  const m = S.simp;
  return api("/api/schedule/import", { method: "POST", body: { week: m.week || S.week, branch: S.sch.branch, filename: m.name, file_b64: m.b64, decisions: m.decisions, deactivate_missing: m.deact, apply } });
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
  $("schImpWeekInfo").innerHTML = simpWeekInfoHtml();   // يوضّح لو الأسبوع اتعمل دلوقتي
  const todo = s.create + s.update + s.review - p.items.filter(i => i.status === "review" && i.decision === "skip").length;
  const run = $("schImpRun");
  run.disabled = !(todo > 0 || m.deact) || (p.branch_mismatch && !m.ack);
  run.textContent = todo > 0
    ? `${bi("استورد", "Import")} ${todo} ${bi("موظف", "employees")}${s.same ? bi(` (و${s.same} بدون تغيير)`, ` (${s.same} unchanged)`) : ""} ${bi("في", "into")} ${weekLabel(m.week)}`
    : (m.isNew ? bi("الأسبوع جاهز — مفيش تغييرات", "Week ready — no changes") : bi("مفيش حاجة تتغيّر", "Nothing to change"));
}
async function simpLoad(file) {
  const m = S.simp;
  if (!/\.xlsx$/i.test(file.name)) { toast("الملف لازم يكون Excel بصيغة .xlsx", "err"); return; }
  if (file.size > 4_000_000) { toast("الملف كبير جدًا", "err"); return; }
  const b64 = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1] || ""); r.onerror = rej; r.readAsDataURL(file); });
  Object.assign(m, { b64, name: file.name, plan: null, decisions: {}, filter: "", deact: false, ack: false });
  $("schImpName").textContent = file.name;
  await simpPreview();
}
on("schImport", "click", async () => {
  if (S.sch.draft.size && !confirm("عندك تغييرات لم تُحفظ في الجدول، والاستيراد هيتجاهلها. تكمّل؟")) return;
  Object.assign(S.simp, { b64: "", name: "", plan: null, decisions: {}, filter: "", deact: false, ack: false,
                          week: S.week, isNew: false });
  $("schImpName").textContent = ""; $("schImpFile").value = ""; $("schImpMismatch").classList.add("hidden");
  $("schImpBody").innerHTML = `<div class="empty"><b>اختار ملف الشيت</b>الأعمدة المتوقعة: FULL TIME / SHIFT / BREAK / DAY OFF ثم PART TIME / SHIFT / DAY OFF. الاستيراد هيكون على فرع <b>${esc(S.sch.branch)}</b> — والأسبوع اللي تختاره فوق.</div>`;
  $("schImpRun").disabled = true; $("schImpRun").textContent = "استيراد";
  simpWeeksRender();
  $("schImpDialog").showModal();
  await simpWeeksLoad();
  simpWeeksRender();
});
on("schImpFile", "change", e => { const f = e.target.files[0]; if (f) simpLoad(f); });
on("schImpWeek", "change", e => {
  const v = e.target.value;
  if (v === "__new__") {
    // أول أسبوع جديد مقترح = الأسبوع اللي بعد آخر أسبوع موجود
    const starts = (S.simp.weeks || []).map(w => w.start).sort();
    const next = addDays(starts.length ? starts[starts.length - 1] : S.week, 7);
    simpPickWeek(next, true);
  } else {
    simpPickWeek(v, false);
  }
});
on("schImpWeekDate", "change", e => {
  if (!e.target.value) return;
  const ws = weekStartOf(e.target.value);
  simpPickWeek(ws, !simpWeekExists(ws));
});
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
  const wasNew = !!m.isNew;
  try {
    const r = await simpRun(true), a = r.applied;
    const target = (r.week && r.week.start) || m.week;
    $("schImpDialog").close();
    const sep = bi("، ", ", ");
    toast(`${bi("تم الاستيراد على", "Imported into")} ${r.week ? r.week.label : weekLabel(target)}${wasNew ? bi(" (أسبوع جديد)", " (new week)") : ""}: ${a.created} ${bi("جديد", "new")}${sep}${a.updated} ${bi("تعديل", "updated")}${a.same ? sep + a.same + " " + bi("بدون تغيير", "unchanged") : ""}${a.deactivated ? sep + a.deactivated + " " + bi("موقوف", "suspended") : ""} — ${bi("اتأكدت من", "verified")} ${a.verified} ${bi("موظف", "employees")}`);
    await refreshEmployees(); S.sch.draft.clear();
    if (target && target !== S.week) setWeek(target);   // نفتح الأسبوع اللي استوردنا فيه
    else await loadSchedule();
  } catch (e) { toast(e.message, "err"); simpRender(); }
});
on("schExport", "click", () => download(`/api/schedule/export.csv?branch=${encodeURIComponent(S.sch.branch)}&${wq()}`, `bankai_schedule_${weekLabel(S.week).replace(" ", "")}_${S.sch.branch.replace(" ", "")}.csv`).catch(e => toast(e.message, "err")));

on("schExcel", "click", () => download(`/api/schedule/export.xlsx?branch=${encodeURIComponent(S.sch.branch)}&${wq()}`, `bankai_schedule_${weekLabel(S.week).replace(" ", "")}_${S.sch.branch.replace(" ", "")}.xlsx`).catch(e => toast(e.message, "err")));


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
    if (!confirm(`إزالة «${e.name}» من جدول ${weekLabel(S.week)} وما بعده؟ الأسابيع اللي قبله وسجل حضوره هيفضلوا زي ما هما.`)) return;
    try {
      await api(`/api/employees/${id}?from_week=${S.week}`, { method: "DELETE" });
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
  else if (t.classList.contains("f-mv") && t.value) {
    if (t.value.startsWith("branch:")) { const to = t.value.slice(7); t.value = ""; moveEmployeeBranch(id, to); }
    else { schSet(id, { shift: t.value }); renderSchedule(); }
  }
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
    const r = await api("/api/schedule/add", { method: "POST", body: { branch: S.sch.branch, shift, items, week: S.week } });
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
      await api("/api/schedule/shifts/rename", { method: "POST", body: { branch: S.sch.branch, old: name, new: nn, week: S.week } });
      await loadSchedule(true); refreshEmployees().catch(() => {}); toast("تم تعديل اسم الشيفت.", "ok");
    } catch (e) { toast(e.message, "err"); }
    return;
  }
  if (act === "delete") {
    if (!confirm(`حذف الشيفت «${name}»؟`)) return;
    try { await api(`/api/schedule/shifts?branch=${encodeURIComponent(S.sch.branch)}&name=${encodeURIComponent(name)}&${wq()}`, { method: "DELETE" }); await loadSchedule(true); toast("تم حذف الشيفت.", "ok"); }
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
      week: S.week, branch: S.sch.branch, shift: $("autoScope").value || null, include_locked: $("autoLocked").checked, allowed_days: days,
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
  if (!confirm("هتمسح سجل الحضور بالكامل (كل التسجيلات المسجلة). الموظفين والجدول والإعدادات هيفضلوا زي ما هما. متأكد؟")) return;
  const word = prompt("خطوة أخيرة: اكتب كلمة «مسح» عشان تأكّد المسح النهائي.");
  if (word !== "مسح") { if (word !== null) toast("الكلمة غير مطابقة — ما اتمسحش حاجة.", "err"); return; }
  try {
    const r = await api("/api/admin/wipe-attendance", { method: "POST" });
    toast(r.message || "تم مسح سجل الحضور.", "ok");
    /* الغرامات كلها بتيجي من سجل الحضور، فبياناتها المخزّنة اتفسحت: نهيّئها عشان
       تتحدّث من أول وجديد أول ما صفحة الغرامات تفتح تاني. */
    S.fines.data = null;
    if (S.page === "dashboard") loadDashboard();
    else if (S.page === "reports" && $("repStart").value) runReport();
    else if (S.page === "fines") loadFines();
    else if (S.page === "attendance") loadRoster();
  } catch (e) { toast(e.message, "err"); }
});
on("setExportEmp", "click", () => download(`/api/employees/export.csv?${wq()}`, `bankai_employees_${weekLabel(S.week).replace(" ", "")}.csv`).catch(e => toast(e.message, "err")));

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

  /* ---------- 0. الزخرفة البصرية (أيقونات + أفاتار احتياطي) ----------
     بديل الـ MutationObserver الشامل اللي كان في ui.js: بدل ما نراقب الـ body
     كله ونعيد المسح مع كل تغيير، بننده ui.js على الحاوية اللي اتغيّرت بس.
     الأفاتار الأساسي بيتولّد جوه تمبليتس app.js نفسها؛ النداء هنا بيمشّط
     العناصر الاحتياطية والأيقونات الجديدة بس. */
  function decorate(sel) {
    var UI = window.BankaiUI;
    if (!UI || !UI.decorate) return;
    try { UI.decorate(typeof sel === "string" ? qs(sel) : (sel || document)); } catch (e) { }
  }
  /* انتقال صفحات (View Transition) شغال دلوقتي؟ الداتا اللي بتوصل في نصّه
     مش محتاجة stagger — لقطة الانتقال نفسها هي الحركة، ولو حرّكنا الصفوف
     وهي تحت اللقطة الحركة بتتحرق وما حدش بيشوفها. */
  function vtRun() { return !!window.bkVtActive; }

  /* ---------- استنى أول رندر للصفحة قبل ما نبدأ الانتقال ----------
     الانتقال بيصوّر الصفحة الجديدة لحظة بدايته؛ لو الداتا لسه جاية بعده،
     المستخدم بيشوف إطار فاضي وبعدين الصفوف تنطّ — وده اللي بيدّي إحساس إن
     الصفحة بتحمّل مرتين. الطريقة هنا: showPage بتنده bkVtWait *قبل* ما تبدأ
     طلب الداتا، وwrapper العرض بتاع الصفحة بينده bkVtDataReady لما أول رندر
     يخلص، وساعتها بس الانتقال بيتصوّر. فيه سقف زمني بسيط عشان لو الشبكة
     اتأخرت نبدأ الانتقال زي الأول وما نعلّقش التبديل. */
  var vtWait = null, vtWaitPage = "";
  function vtRelease() {
    if (!vtWait) return;
    clearTimeout(vtWait.t);
    var r = vtWait.res;
    vtWait = null; vtWaitPage = "";
    r();
  }
  window.bkVtWait = function (p) {
    if (vtWait) vtRelease(); /* تبديل سريع ورا بعض: حرّر الانتقال اللي قبله */
    var o = { t: 0, res: null };
    var pr = new Promise(function (res) { o.res = res; });
    vtWait = o; vtWaitPage = p;
    o.t = setTimeout(function () { if (vtWait === o) vtRelease(); }, 260);
    return pr;
  };
  window.bkVtDataReady = function (p) {
    if (!vtWait || (p && vtWaitPage && p !== vtWaitPage)) return;
    vtRelease();
  };

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
  window.syncNavInd = syncNav;
  if (nav) {
    /* ⚠ اتشال MutationObserver اللي كان مراقب الـ .tabs: كان بيصحى مع كل كتابة
       على class على أي زرار جوه الشريط — بما فيها الكتابة اللي إحنا نفسنا بنعملها
       في syncNav — وكان محتاج boolean guard عشان مايدخلش في حلقة لا نهائية.
       دلوقتي في نداءات مباشرة من المسارات اللي بتغيّر الاختيار فعلاً. */
    addEventListener("resize", syncNav);
    document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") syncNav(); });
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
  /* بارات النسبة في كروت الملخص عندها أنيميشن نمو من الـ CSS. وقت انتقال الصفحات
     الداتا بتوصل قبل لقطة الانتقال، فالنمو بيكمل بعد ما الانتقال يخلص ويبان
     كأن الصفحة بتحمّل تاني — بنقفله والبارات تظهر مليانة على طول. */
  function noBarGrow(host) {
    if (!vtRun() || !host) return;
    qsa(".kpi-bar i", host).forEach(function (b) { b.style.animation = "none"; });
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
    var ranVt = false;
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
    } else {
      _showPage(p);
      /* لو فيه انتقال صفحات (View Transition) بدأ في النداء ده، لقطة الانتقال
         نفسها هي حركة الدخول: الصفحة وصلت وهي بتتبدّل. فلو حرّكنا كمان الصفحة
         وصفوفها بعد ما الانتقال يخلص، المستخدم بيشوف حركتين ورا بعض على نفس
         المحتوى — وده اللي بيحسّ إن الصفحة بتحمّل مرتين. عشان كده بنقفل دخول
         العناصر خالص ساعتها، والصفوف بتظهر جاهزة مع اللقطة (المحتوى وصل، خلاص
         مافيش داعي ندوّر عليه تاني). */
      ranVt = !!window.bkVtActive;
      if (ranVt) enter = null; else markEnter(p);
    }
    /* لو فيه انتقال صفحات شغال، بنأجّل دخول العناصر لحد ما يخلص بدل ما يتحرق تحت لقطة الانتقال ويتشال بدون ما حد يشوفه. */
    var done = function () { if (!bootPage && !ranVt) enterPage(p); syncNav(); };
    if (window.bankaiAfterSwap) window.bankaiAfterSwap(done); else requestAnimationFrame(done);
  };

  /* ---------- 5. كروت الملخص: شريط + نسبة + عدّاد ---------- */
  var _dash = window.renderDashboard;
  window.renderDashboard = function (d) {
    _dash(d);
    decorate("#page-dashboard");
    var boot = bootPage === "dashboard" && revealBoot("dashboard"); /* أول داتا وصلت — نبدّل الهيكل بالصفحة الحقيقية */
    var host = qs("#dashKpis");
    if (!host) { window.bkVtDataReady("dashboard"); return; }
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
    noBarGrow(host);
    if (!vtRun() && (boot || enter === "dashboard")) { enter = null; stagger("#dashKpis .kpi", 26, 0, 6, RISE, 130); }
    window.bkVtDataReady("dashboard"); /* لو فيه انتقال مستني الداتا، ابدأه دلوقتي */
  };

  /* ---------- 6. التقارير ---------- */
  var _rep = window.renderReport;
  window.renderReport = function () {
    _rep.apply(null, arguments);
    decorate("#page-reports");
    var boot = bootPage === "reports" && revealBoot("reports");
    var rh = qs("#repKpis"); if (rh) rh.setAttribute("aria-busy", "false");
    if (rh) rh.style.animation = "none";
    noBarGrow(rh);
    if (!vtRun() && (boot || enter === "reports")) {
      enter = null;
      stagger("#repKpis .kpi", 26, 0, 6, RISE, 130);
      stagger("#repDays tr", 10, 30, 14, FADE, 90);
      stagger("#repRows tr", 9, 40, 26, FADE, 150);
    }
    window.bkVtDataReady("reports");
  };

  /* ---------- 7. الحضور اليومي ---------- */
  var _att = window.renderAttendance;
  window.renderAttendance = function () {
    _att.apply(null, arguments);
    decorate("#page-attendance");
    var boot = bootPage === "attendance" && revealBoot("attendance");
    if (!vtRun() && (boot || enter === "attendance")) {
      enter = null;
      stagger("#attList .arow", 9, 30, 30, FADE, 150);
      growBars("#attList .ghead .stack i");
    }
    window.bkVtDataReady("attendance");
  };

  /* ---------- 8. الموظفون ---------- */
  var _emp = window.renderEmployees;
  window.renderEmployees = function () {
    _emp.apply(null, arguments);
    decorate("#page-employees");
    if (bootPage === "employees" && revealBoot("employees")) { enter = null; if (!vtRun()) stagger("#empRows tr", 9, 20, 26, FADE, 140); }
    else if (enter === "employees") { enter = null; if (!vtRun()) stagger("#empRows tr", 9, 20, 26, FADE, 140); }
    window.bkVtDataReady("employees");
  };

  /* ---------- 9. الجدول ---------- */
  var _sch = window.renderSchedule;
  window.renderSchedule = function () {
    _sch.apply(null, arguments);
    decorate("#page-schedule");
    var boot = bootPage === "schedule" && revealBoot("schedule");
    if (!vtRun() && (boot || enter === "schedule")) {
      enter = null;
      stagger("#schTotals .dt", 22, 20, 8, RISE, 90);
      stagger("#schShifts .shcard", 40, 40, 6, RISE, 140);
    }
    window.bkVtDataReady("schedule");
  };
  /* صفحة الإعدادات مالهاش render wrapper — أول رد من /api/settings بيكمّل هيكل التحميل */
  var _ls = window.loadSettings;
  window.loadSettings = function () {
    var pr = _ls.apply(null, arguments);
    if (pr && pr.then) pr.then(function () {
      decorate("#page-settings");
      if (bootPage === "settings") revealBoot("settings");
      window.bkVtDataReady("settings");
    }).catch(function () { window.bkVtDataReady("settings"); });
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

  /* ---------- 11. الغرامات ---------- */
  var _fin = window.renderFines;
  window.renderFines = function () {
    _fin.apply(null, arguments);
    decorate("#page-fines");
    var boot = bootPage === "fines" && revealBoot("fines");
    var fh = qs("#fineKpis"); if (fh) fh.setAttribute("aria-busy", "false");
    if (fh) fh.style.animation = "none";
    noBarGrow(fh);
    if (!vtRun() && (boot || enter === "fines")) {
      enter = null;
      stagger("#fineKpis .kpi", 26, 0, 6, RISE, 130);
      stagger("#fineRows tr", 9, 40, 26, FADE, 150);
    }
    window.bkVtDataReady("fines");
  };
})();

/* ============================== التحديث المباشر (live) ==============================
   كل تاب بتعمل long-poll على `/api/events`: أي تسجيل أو تعديل من أي مستخدم تاني
   بيوصل هنا في نفس اللحظة، فالصفحة بتحدّث نفسها لوحدها من غير أي refresh، ومعاها
   تنبيه مرئي (وسمعي لو حابب). لو الاتصال وقع بيتكرر من نفسه، ومفيش أي تأثير على
   الشغل العادي: أسوأ حالة إن التنبيه يتأخر شوية. */

/* -------- التنبيه الصوتي: نبرتين قصيرتين بـ WebAudio (من غير أي ملف صوت) -------- */
const soundOn = () => { try { return localStorage.getItem("bankai-live-sound") !== "0"; } catch (e) { return true; } };
let audioCtx = null;
function initAudio() {
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
  } catch (e) { audioCtx = null; }
}
/* المتصفحات بتمنع الصوت قبل أي تفاعل من المستخدم: بنجهّز الصوت من أول كليكة */
document.addEventListener("pointerdown", initAudio, { once: true });
document.addEventListener("keydown", initAudio, { once: true });

function beep() {
  if (!soundOn()) return;
  /* التنبيه بيتكرر بسرعة مع كل حدث، فبنمنع الرنّ المتكرر (نبرة واحدة كل ~1.2 ث) */
  const now = Date.now();
  if (now - (beep._last || 0) < 1200) return;
  beep._last = now;
  try {
    initAudio(); if (!audioCtx) return;
    const t0 = audioCtx.currentTime;
    [[880, 0], [1180, 0.11]].forEach(([f, d]) => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t0 + d);
      g.gain.exponentialRampToValueAtTime(0.14, t0 + d + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + d + 0.17);
      o.connect(g); g.connect(audioCtx.destination);
      o.start(t0 + d); o.stop(t0 + d + 0.2);
    });
  } catch (e) { /* الصوت مش أساسي */ }
}

function applySoundBtn() {
  const b = $("soundBtn"); if (!b) return;
  const on = soundOn();
  b.classList.toggle("off", !on);
  b.setAttribute("aria-pressed", on ? "true" : "false");
  b.title = on ? "تنبيه صوتي عند تغيير من مستخدم تاني — شغّال (اضغط للكتم)" : "التنبيه الصوتي مكتوم (اضغط للتشغيل)";
}
on("soundBtn", "click", () => {
  try { localStorage.setItem("bankai-live-sound", soundOn() ? "0" : "1"); } catch (e) { }
  applySoundBtn();
  if (soundOn()) beep();
  toast(soundOn() ? "التنبيه الصوتي اتشغّل" : "التنبيه الصوتي اتكتم");
});

/* -------- مؤشّر الاتصال في الشريط العلوي -------- */
function setLive(up, text) {
  const p = $("livePill"); if (!p) return;
  p.classList.toggle("off", !up);
  const __d = (S.lang === "en") ? I18N.en : I18N.ar;
  $("liveText").textContent = __d[text] || text;
  p.title = up ? "متصل: أي تغيير من أي مستخدم تاني بيظهر هنا فورًا من غير refresh"
    : "الاتصال بالتحديث المباشر انقطع — بنحاول نرجع تاني";
}

/* -------- حلقة الاستماع (long-poll) -------- */
let liveRun = false, liveRev = 0, liveFails = 0, liveFirst = true;
async function liveLoop() {
  while (liveRun) {
    try {
      /* أول اتصال بياخد لقطة فورية (wait=0) عشان يعرف مكانه من غير ما يستنى،
         وبعدها long-poll عادي. `liveFirst` بدل ما نعتمد على liveRev===0 لأن rev
         صفر نفسها قيمة صحيحة (سيرفر جديد لسه مفيش أحداث فيه).
         مدة الانتظار أقل من أي timeout على البروكسي (Railway ~30 ث) بمargin. */
      const hide = document.visibilityState === "hidden";
      const wait = liveFirst ? 0 : (hide ? 20 : 15);
      const r = await api(`/api/events?since=${liveRev}&wait=${wait}`, { silent: true });
      liveFails = 0; setLive(true, "متابعة مباشرة");
      if (!liveFirst) { if (r.reset) liveRefreshPage(); else applyEvents(r.events || []); }
      liveFirst = false;
      liveRev = (typeof r.rev === "number") ? r.rev : liveRev;
    } catch (e) {
      if (!S.token) { stopLive(); return; }
      liveFails++;
      setLive(false, liveFails > 2 ? "إعادة الاتصال…" : "الاتصال انقطع");
      await new Promise(res => setTimeout(res, Math.min(12000, 1500 * liveFails)));
    }
  }
}
function startLive() {
  if (liveRun || !S.token) return;
  liveRun = true; liveRev = 0; liveFails = 0; liveFirst = true;
  setLive(true, "متابعة مباشرة");
  liveLoop();
}
function stopLive() { liveRun = false; if ($("livePill")) setLive(false, "متوقف"); }

/* -------- تطبيق الأحداث على الشاشة -------- */
const KIND_AR = { attendance: "تسجيلات الحضور", employees: "بيانات الموظفين", schedule: "الجدول", settings: "الإعدادات", fines: "الغرامات" };
function applyEvents(events) {
  /* بنتجاهل بس الأحداث اللي مصدرها التاب نفسها. أي حدث من غير معرّف تاب (كتابة
     من بره الواجهة مثلًا) بنعتبره من حد تاني وبنطبّقه. */
  const mine = ev => !!ev.client && ev.client === S.clientId;
  const fresh = events.filter(ev => !mine(ev));
  if (!fresh.length) return;
  let hit = false;
  for (const ev of fresh) if (liveRefresh(ev)) hit = true;
  if (!hit) return;
  const names = [...new Set(fresh.flatMap(ev => ev.names || []))].filter(Boolean);
  const what = names.length ? names.slice(0, 2).join("، ") + (names.length > 2 ? ` +${names.length - 2}` : "")
    : (KIND_AR[fresh[0].kind] || "بيانات");
  toast(`تحديث مباشر — ${fresh[0].by || "مستخدم"}: ${what}`, "ok");
  beep();
  pulse($("livePill"), "bump");
}

/* بيحدّث الصفحة الحالية لو الحدث يعنيها، وبيرجّع true لو اتأثرت فعلاً */
function liveRefresh(ev) {
  const kind = ev.kind;
  if (S.page === "attendance") {
    if (kind === "attendance" && (!ev.date || ev.date === S.att.date)) { loadRosterSoft(ev); return true; }
    if (kind !== "attendance") { loadRosterSoft(ev); return true; } /* بيانات موظفين/جدول: الروستر كله */
    return false;
  }
  if (S.page === "dashboard") {
    if (kind === "attendance" && ev.date && ev.date !== S.dash.date) return false;
    loadDashboard(); if (kind === "attendance") loadFeed();
    return true;
  }
  if (S.page === "employees") { loadEmployeesSoft(); return true; }
  if (S.page === "fines") { loadFines(); return true; }
  if (S.page === "reports") { if ($("repStart").value) runReport(); return true; }
  if (S.page === "schedule") { if (S.sch && !S.sch.draft.size) loadSchedulePage(); return true; }
  return false; /* صفحة الإعدادات: تنبيه بس من غير ما نلمس اللي المستخدم بيكتبه */
}
function liveRefreshPage() {
  if (S.page === "attendance") loadRosterSoft();
  else if (S.page === "dashboard") { loadDashboard(); loadFeed(); }
  else if (S.page === "employees") loadEmployeesSoft();
  else if (S.page === "fines") loadFines();
  else if (S.page === "reports" && $("repStart").value) runReport();
  else if (S.page === "schedule" && S.sch && !S.sch.draft.size) loadSchedulePage();
}

/* -------- تحديث ناعم لصفحة الحضور: بيمسّ الصفوف اللي اتغيّرت بس -------- */
async function loadRosterSoft(ev = {}) {
  if (S.page !== "attendance" || !S.roster) return;
  const date = S.att.date, y = window.scrollY;
  let r;
  try { r = await api(`/api/roster?attendance_date=${date}`, { silent: true }); }
  catch (e) { return; }                        /* التحديث التلقائي مش أساسي: نسيب الشاشة زي ما هي */
  if (S.page !== "attendance" || date !== S.att.date) return;
  const only = (ev.ids && ev.ids.length) ? new Set(ev.ids) : null;
  let full = !!ev.reset;
  const hit = id => !only || only.has(id);
  const list = [];
  for (const e of r.employees) {
    const old = S.rmap.get(e.id);
    if (!old) { full = true; list.push(e); continue; }
    if (S.draft.has(e.id)) {
      /* الصف اللي عليه مسودة بنسيب نسختنا زي ما هي عشان القفل المتفائل يشتغل عند
         الحفظ، بس بنعلّمه إن حد غيّره من بره. */
      if (hit(e.id) && JSON.stringify(e.attendance || null) !== JSON.stringify(old.attendance || null)) {
        S.stale.add(e.id);
        if (!full) updateRowUI(old);
      }
    } else {
      Object.assign(old, e);
      S.stale.delete(e.id);
      if (!full && hit(e.id)) updateRowUI(old);
    }
    list.push(old);
  }
  if (r.employees.length !== S.roster.employees.length) full = true;
  S.roster = r; S.roster.employees = list;
  S.rmap = new Map(list.map(e => [e.id, e]));
  for (const id of [...S.draft.keys()]) { const e = S.rmap.get(id); if (!e) S.draft.delete(id); else maybeClean(e); }
  if (full) { renderAttendance(); } else { updateAttSummary(); renderOff(); }
  updateSaveBar();
  if (full && window.scrollY !== y) window.scrollTo(0, y);
}

async function loadEmployeesSoft() {
  try {
    S.employees = await api(`/api/employees?${wq()}`, { silent: true });
    if (S.page === "employees") renderEmployees();
    if (S.page === "attendance") loadRosterSoft();
  } catch (e) { /* زي فوق: تحديث تلقائي مش أساسي */ }
}

applySoundBtn();
setLive(false, "غير متصل");

/* ================= سلاسة: أداء الشريط =================
   ضبابية الشريط العلوي بتكلّف رسم على كل إطار أثناء التمرير — بعد أول شوية
   تمرير بنحوّلها لخلفية صلبة (نفس المنظر تقريبًا، بدون تكلفة).
   ملاحظة: حركة فتح/قفل النوافذ كلها CSS (transition-behavior: allow-discrete)
   من غير أي تعديل على سلوك dialog في الـJS. */
(function navSolidOnScroll() {
  const nav = document.querySelector(".topnav");
  if (!nav) return;
  let raf = 0, solid = false;
  const apply = () => {
    raf = 0;
    const want = window.scrollY > 8;
    if (want !== solid) { solid = want; nav.classList.toggle("solid", solid); }
  };
  addEventListener("scroll", () => { if (!raf) raf = requestAnimationFrame(apply); }, { passive: true });
  apply();
})();

