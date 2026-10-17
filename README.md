# Bankai — Roster & Attendance

نظام داخلي لإدارة **الروستر والحضور والغرامات** لفرعين (`QCD 1` و `QCD 2`).
واجهة عربية RTL بالكامل، بدون أي build step.

## التشغيل

```bash
pip install -r requirements.txt
export ADMIN_PASSWORD="<كلمة مرور قوية>"
uvicorn app:app --host 0.0.0.0 --port 8000
```

ثم افتح <http://localhost:8000> — قاعدة البيانات `bankai.db` بتتولّد تلقائيًا.

## متغيرات البيئة

| المتغير | الافتراضي | الوظيفة |
| --- | --- | --- |
| `ADMIN_PASSWORD` | `ChangeThisPassword123!` | حساب الأدمن (كل الصلاحيات). **لازم تتغيّر في الإنتاج.** |
| `SUPERVISOR_PASSWORD` | `""` | حساب المشرف (مشاهدة + تعديل الحضور اليومي). فاضية = مقفول. |
| `SECRET_KEY` | مشتق من `ADMIN_PASSWORD` | توقيع جلسات الدخول. تغييره يبطّل كل الجلسات القائمة. |
| `DATABASE_URL` | `sqlite:///./bankai.db` | الصورة على Railway بتحدده لـ `sqlite:////data/bankai.db`. |
| `PORT` | `8000` | Railway بيحدده تلقائيًا. |

> ⚠️ تغيير `ADMIN_PASSWORD` مع عدم تحديد `SECRET_KEY` بيغيّر المفتاح المشتق، فكل المستخدمين لازم يسجّلوا الدخول من جديد.

## البنية

| الملف | الوصف |
| --- | --- |
| `app.py` | الـ API + الموديلات + منطق الأعمال (FastAPI + SQLAlchemy + SQLite). |
| `static/index.html` | الواجهة (SPA واحدة، HTML دلالي). |
| `static/app.js` | كل منطق الواجهة (fetch على `/api/*`). |
| `static/ui.js` | إضافات عرضية بحتة (أفاتار الأحرف الأولى + إخفاء الأيقونات الزخرفية عن قارئ الشاشة). |
| `static/style.css` | كل التنسيقات (CSS خام، بدون framework). |
| `Dockerfile` / `railway.json` | النشر على Railway (`/api/health` هو الـ healthcheck). |

**مبدأ مهم:** الـ `.xlsx` بيتقرا **يدوي** بـ `zipfile` + `xml.etree` جوّه `app.py` — بدون `openpyxl` أو أي مكتبة زيادة.

## ملاحظات تشغيلية

- **نسخة ملفات الواجهة تلقائية.** `index.html` فيه `?v=__ASSET_V__` والسيرفر بيستبدلها ببصمة محتوى
  لـ `style.css` + `app.js` + `ui.js` عند كل طلب — يعني مفيش حاجة تنسى تزوّد رقم النسخة بعد النشر.
- **تغييرات قاعدة البيانات** بتم من `ensure_columns()` (إضافة أعمدة idempotent)، مفيش Alembic.
- **إجراءات لا رجعة فيها:** `POST /api/admin/wipe-attendance` بيمسح كل سجل الحضور. خُد نسخة
  احتياطية من الإعدادات قبلها.
- **قفل تسجيل الدخول:** 8 محاولات فاشلة من نفس الـ IP ⇒ قفل 5 دقايق (في الذاكرة، بيروح مع restart).
