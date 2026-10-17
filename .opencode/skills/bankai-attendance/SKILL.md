---
name: bankai-attendance
description: Project conventions and domain rules for the Bankai Roster & Attendance app (FastAPI + SQLAlchemy + SQLite backend, vanilla HTML/CSS/JS frontend, deployed on Railway). Use when reading, changing, debugging, or reviewing any file in this repository - app.py, static/index.html, static/app.js, static/style.css, Dockerfile, or railway.json.
---

# Bankai Roster & Attendance

Internal workforce tool for tracking **daily attendance, rosters, shift schedules, and fines**
across two branches. Single-tenant, deployed for one company. All user-facing text is
**Egyptian Arabic** — keep it that way.

## Hard constraints (do not break these)

- **No build step, no bundler, no npm.** The frontend is plain HTML/CSS/JS served as static
  files. Never introduce React, Tailwind, TypeScript, or a bundler without being asked explicitly.
- **No third-party Python packages beyond `requirements.txt`**: `fastapi`, `uvicorn`,
  `sqlalchemy`, `pydantic`. Notably, the `.xlsx` reader is hand-rolled on `zipfile` +
  `xml.etree` (`read_xlsx`) to avoid `openpyxl`. Don't replace it with a dependency.
- **The backend is one file: `app.py`.** Synchronous SQLAlchemy 2.x-style models with the
  legacy `db.query()` API. Do not migrate to async or split into modules/routers unless the
  user explicitly asks for a refactor.
- **Schema changes go through `ensure_columns()`**, a hand-written idempotent
  `ALTER TABLE ... ADD COLUMN` guard that runs at import. There is no Alembic. Adding a
  column means: add the `Column(...)` to the model **and** an `if "<name>" not in cols:` block.
- **Asset cache-busting is automatic.** `static/index.html` contains `?v=__ASSET_V__`, and
  `home()` replaces it with a content stamp of `style.css` + `app.js` + `ui.js` (`ASSET_STAMP`).
  Never hardcode a version number — editing a static file is enough to bust the cache.
- **Security headers are global**, set by the `security_headers` middleware, and the favicon is
  an inline SVG served from `/favicon.svg` and `/favicon.ico`. The login lockout keys on the
  **right-most** `X-Forwarded-For` hop (the one the proxy appends); the left-most value is
  client-supplied and must never be trusted.

## Stack map

| Layer | File | Notes |
| --- | --- | --- |
| API + models + helpers | `app.py` (~1500 lines) | 35 routes (32 under `/api`), 21 classes, FastAPI 2.1.0 |
| Single-page UI | `static/index.html` (~37 KB) | served by `GET /` |
| All client logic | `static/app.js` (~93 KB) | fetch calls to `/api/*` |
| All styling | `static/style.css` (~114 KB) | no framework |
| Deploy | `Dockerfile`, `railway.json` | `python:3.12-slim`, healthcheck `/api/health` |

## Domain vocabulary (exact values — these are stored in the DB)

| Concept | Allowed values |
| --- | --- |
| Branches (`BRANCHES`) | `QCD 1`, `QCD 2` |
| Employment type (`EMP_TYPES`) | `FT`, `PT` |
| Weekday (`WEEKDAYS`) | `Saturday` … `Friday` (week starts Saturday; default day off is `Friday`) |
| Attendance status (`STATUSES`) | `Present`, `Late`, `Absent`, `Excused` |
| Statuses that force `fine = 0` (`NO_FINE_STATUSES`) | `Present`, `Excused` |

### Rules worth remembering

- `PUT /api/employees/{id}` body is always wrapped in a `data` object, matching `EmployeeIn`.
- `DELETE /api/employees/{id}` **does not delete** — it sets `active = False` so attendance
  history survives. Same intent for the roster: inactive employees never appear.
- Attendance is unique per `(employee_id, attendance_date)` (`uq_employee_attendance_day`).
  An empty `status` string in `_save_items` **deletes** the row instead of writing one.
- A single `POST /api/attendance/bulk` accepts a `date` plus per-row `{employee_id, status, fine, reason, note}`
  and returns `{saved, cleared, errors, records}` — per-row failures do not fail the whole request.
- Employees on their `day_off` must be rejected from attendance for that weekday
  ("هذا الموظف في إجازة في هذا اليوم").
- Fines are seeded from `Setting` rows `fine_late` / `fine_absent` (`GET/PUT /api/settings`),
  and `fine` is clamped to `[0, 1000000]` by Pydantic.
- `POST /api/schedule/auto` **only proposes** — it returns `{changes, warnings}` and never
  writes. The UI shows the suggestion, then the user saves via `/api/schedule/apply`.
- `off_locked = True` means an employee's day off is pinned and the auto-distributor skips them
  (unless `include_locked` is set).
- `parse_shift()` derives start/end minutes from the shift *name* (e.g. `8 AM - 4 PM`,
  `16:00-00:00`, and Egyptian `ص`/`م` markers). `break_slots()` places 30-minute-staggered
  breaks between 35% and 75% of the shift.
- Excel import (`/api/schedule/import`) is a **plan → review → apply** flow: `apply: false`
  returns a diff plan with per-row `status` (`create`/`update`/`same`/`review`/`skip`) and
  `decisions` (`skip`, `new`, or an employee id). Apply runs a post-write verification pass
  and **rolls back everything** if any row fails.

## Auth model

- Two shared passwords, no user accounts, no DB-stored users:
  `ADMIN_PASSWORD` (role `admin`) and optional `SUPERVISOR_PASSWORD` (role `supervisor`).
  Empty supervisor password = supervisor access disabled.
- Tokens are stateless: `"{role}.{exp}.{hmac_sha256}"` signed with `SECRET_KEY`, 30-day TTL
  (`TOKEN_TTL_SECONDS`). `SECRET_KEY` defaults to a hash of `ADMIN_PASSWORD`, so **rotating the
  password invalidates every session**.
- Role helpers: `require_auth` (any role), `require_admin` (admin only). Read endpoints use
  `require_auth`; writes to employees/settings/schedule and the danger zone use `require_admin`.
- Login lockout is in-memory per client IP: 8 failures → 300 s lock (`_login_state`).
  It does not survive a restart; that is accepted.
- `ADMIN_PASSWORD` still has the placeholder default `ChangeThisPassword123!`. Never "fix" this
  by hardcoding a new secret — it must come from the environment.

## API conventions to follow

- Error responses are `HTTPException(status, "<Arabic message>")`. Match the existing tone and
  phrasing; do not switch to English messages.
- Request/response bodies use Pydantic models with `Field(...)` bounds; responses are built by
  `employee_dict()` / `attendance_dict()` / `_counts()` so the JSON shape stays stable.
- CSV exports go through `csv_response()`, which writes a UTF-8 BOM and calls `csv_safe()` to
  neutralise Excel formula injection. Never emit CSV with `csv.writer` directly.
- Date range endpoints (`/api/reports/*`) enforce through `_check_range()`: `end >= start` and
  a maximum span of 366 days.
- Input normalisation helpers exist so imports tolerate messy human input:
  `norm_branch`, `norm_type`, `norm_day`, `parse_shift`, `parse_break_time`, `_name_key`,
  `_token_key`. Reuse them instead of writing new parsers.

## Frontend conventions

- One SPA: `index.html` holds the markup, `app.js` all behaviour, `style.css` all styles.
  There are no components and no state library — keep functions module-scoped.
- Authenticate with `Authorization: Bearer <token>`; store the token client-side and expect
  `401` to mean "session invalid, log in again".
- UI copy is Arabic and right-to-left friendly; numbers/dates in exports are ISO (`YYYY-MM-DD`)
  and fines formatted with `f"{x:.2f}"`.
- `.xlsx` files are read in the browser and posted as base64 (`file_b64`), never multipart.

## CSS layout traps (learned the hard way)

`style.css` grows by **appending numbered layers** (`V15 «REBORN»`, `V16 «POLISH»`, …). The last
matching rule wins, so override at the end instead of rewriting an earlier layer. Three traps have
already caused real, user-visible breakage:

- **Never put `transform`, `filter`, `backdrop-filter`, `perspective`, or `will-change` on
  `.page`.** `.save-bar` and `#schSaveBar` are `position: fixed` *inside* `.page`; any of those
  properties makes `.page` their containing block, so `bottom: 16px` resolves against the bottom of
  the page instead of the viewport and the bar lands off-screen — the user sees "there is no save
  button at all" even though the element is in the DOM. `@keyframes pageIn` is **opacity-only on
  purpose**. Regression check:

  ```js
  [...document.querySelectorAll('.page')].every(p => getComputedStyle(p).transform === 'none')
  ```

- **`@keyframes` names are global.** Redefining an existing name (`cardIn`) in a new layer silently
  retargets every animation already using it. Give new keyframes a unique name.

- **The sticky `.topbar` and `.main` must agree on horizontal padding.** `.topbar` uses
  `margin-inline: -30px; padding-inline: 30px` to bleed across `main`, while
  `@media(max-width:1180px)` drops `.main` to `padding-inline: 22px`; in the 981–1180 px window the
  pair disagreed by 8 px, leaked past the viewport edge, and produced a horizontal scrollbar. Keep
  the two in sync.

Before shipping any CSS change, confirm on every route that
`document.documentElement.scrollWidth === document.documentElement.clientWidth`, and that
`elementFromPoint()` at the centre of `#saveBtn` returns the button (not `null`, not an overlay).

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./bankai.db` | Docker image sets `sqlite:////data/bankai.db` |
| `ADMIN_PASSWORD` | `ChangeThisPassword123!` | admin login |
| `SUPERVISOR_PASSWORD` | `""` | supervisor login (disabled when empty) |
| `SECRET_KEY` | derived from `ADMIN_PASSWORD` | token signing |
| `PORT` | `8000` | Railway injects this |

## Run and verify locally

```powershell
# install (a virtualenv is recommended)
python -m pip install -r requirements.txt

# run — creates bankai.db in the working directory
python -m uvicorn app:app --host 127.0.0.1 --port 8000

# smoke checks
curl http://127.0.0.1:8000/api/health          # {"status":"ok", ...}
curl -X POST http://127.0.0.1:8000/api/login -H "Content-Type: application/json" -d '{\"password\":\"<ADMIN_PASSWORD>\"}'
```

There is **no test suite**. Before claiming a change works, exercise the real endpoint
(curl or the `webapp-testing` skill) and confirm the response body — do not infer success from
a clean import.

## Working rules for this repo

1. Read the surrounding code before editing; the file blends Arabic comments, domain jargon, and
   normalisation helpers, and re-implementing any of it will drift.
2. Keep user-visible strings Arabic and error status codes meaningful (`400` validation,
   `401` session, `403` role, `404` missing, `409` duplicate code, `429` lockout).
3. Preserve the `unique` constraint on `employee_code` and the `(employee_id, attendance_date)`
   uniqueness when touching attendance logic.
4. Never run `/api/admin/wipe-attendance` or `deactivate_missing` against real data while
   testing — they destroy attendance history irreversibly.
5. `git push` and any destructive migration are the user's call. Ask first.
