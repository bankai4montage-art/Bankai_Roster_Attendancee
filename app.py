import os
import csv
import io
import hmac
import re
import hashlib
import secrets
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional, List, Dict
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import (create_engine, Column, Integer, String, Boolean, Date, DateTime, Float,
                        ForeignKey, UniqueConstraint, func, case, or_, inspect, text,
                        bindparam, select)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from starlette.background import BackgroundTask

APP_NAME = "Rawahel — Roster & Attendance"
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bankai.db")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ChangeThisPassword123!")
# كلمة مرور المشرفين: مشاهدة كل حاجة + تعديل الحضور اليومي فقط. فاضية = الخاصية مقفولة.
SUPERVISOR_PASSWORD = os.getenv("SUPERVISOR_PASSWORD", "")
# المفتاح السري لتوقيع جلسات الدخول. لو مش متحدد بيتولد من كلمة المرور فتفضل الجلسات شغالة بعد إعادة التشغيل.
SECRET_KEY = (os.getenv("SECRET_KEY") or os.getenv("ADMIN_TOKEN")
              or hashlib.sha256(("bankai|" + ADMIN_PASSWORD).encode()).hexdigest())
TOKEN_TTL_SECONDS = 30 * 24 * 3600

BRANCHES = ("QCD 1", "QCD 2")
EMP_TYPES = ("FT", "PT")
WEEKDAYS = ("Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
STATUSES = ("Present", "Late", "Absent", "Excused")
NO_FINE_STATUSES = ("Present", "Excused")
# أسماء الوسائط المسموح بها في جمل تحديث الحضور المجمّعة
_PARAM_KEYS = ("row_id", "status", "fine", "reason", "note", "updated_at", "expected")
# لو العميل بعت القيمة دي في expected_updated_at يبقى هو شاف اليوم من غير أي تسجيل
# للموظف ده — فلو حد تاني سجّله في اللحظة دي بنقول تعارض بدل ما نكتب فوقه.
NEW_STAMP = "-"

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Employee(Base):
    __tablename__ = "employees"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    employee_code = Column(String(60), nullable=True, unique=True)
    branch = Column(String(20), nullable=False, index=True)
    shift = Column(String(80), nullable=False)
    employment_type = Column(String(10), nullable=False)
    day_off = Column(String(12), nullable=False, default="Friday")
    active = Column(Boolean, nullable=False, default=True)
    phone = Column(String(30), nullable=True)
    break_start = Column(String(5), nullable=True)      # "HH:MM"
    break_min = Column(Integer, nullable=True)          # مدة البريك بالدقايق
    off_locked = Column(Boolean, nullable=False, default=False)  # يوم الإجازة مثبّت (التوزيع التلقائي ما يغيّرهوش)
    created_at = Column(DateTime, default=utcnow)


class Attendance(Base):
    __tablename__ = "attendance"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    attendance_date = Column(Date, nullable=False, index=True)
    status = Column(String(20), nullable=False)
    fine = Column(Float, nullable=False, default=0)
    reason = Column(String(500), nullable=True, default="")
    note = Column(String(500), nullable=True, default="")
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    employee = relationship("Employee")
    __table_args__ = (UniqueConstraint("employee_id", "attendance_date", name="uq_employee_attendance_day"),)


class ShiftDef(Base):
    """شيفتات معرّفة لكل فرع (عشان نقدر نضيف شيفت فاضي ونوزّع عليه بعدين)."""
    __tablename__ = "shift_defs"
    id = Column(Integer, primary_key=True)
    branch = Column(String(20), nullable=False, index=True)
    name = Column(String(80), nullable=False)
    __table_args__ = (UniqueConstraint("branch", "name", name="uq_shift_branch_name"),)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(60), primary_key=True)
    value = Column(String(500), nullable=False, default="")


class WeekSchedule(Base):
    """جدول الموظف في أسبوع معيّن (الشيفت + يوم الإجازة + البريك). كل أسبوع له نسخته المستقلة."""
    __tablename__ = "week_schedule"
    id = Column(Integer, primary_key=True)
    week_start = Column(Date, nullable=False, index=True)       # يوم الأحد بتاع الأسبوع
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    shift = Column(String(80), nullable=False)
    day_off = Column(String(12), nullable=False, default="Friday")
    break_start = Column(String(5), nullable=True)
    break_min = Column(Integer, nullable=True)
    off_locked = Column(Boolean, nullable=False, default=False)
    __table_args__ = (UniqueConstraint("week_start", "employee_id", name="uq_week_employee"),)


Base.metadata.create_all(bind=engine)


def ensure_columns():
    """ترقية خفيفة لقاعدة البيانات القديمة: إضافة الأعمدة الجديدة لو ناقصة (بدون مسح أي بيانات)."""
    cols = {c["name"] for c in inspect(engine).get_columns("employees")}
    with engine.begin() as conn:
        if "phone" not in cols:
            conn.execute(text("ALTER TABLE employees ADD COLUMN phone VARCHAR(30)"))
        if "break_start" not in cols:
            conn.execute(text("ALTER TABLE employees ADD COLUMN break_start VARCHAR(5)"))
        if "break_min" not in cols:
            conn.execute(text("ALTER TABLE employees ADD COLUMN break_min INTEGER"))
        if "off_locked" not in cols:
            false = "0" if DATABASE_URL.startswith("sqlite") else "FALSE"
            conn.execute(text(f"ALTER TABLE employees ADD COLUMN off_locked BOOLEAN NOT NULL DEFAULT {false}"))


ensure_columns()


# ---------------------------------------------------------------- الأسابيع (Sunday → Saturday)
# الأسبوع بيبدأ الأحد. الأسبوع اللي فيه 1 يناير هو Week 1، فيكون 25-31 أكتوبر 2026 = Week 44.
BASE_WEEK = date(2026, 10, 25)          # الجدول الموجود قبل نظام الأسابيع بيتحوّل لهذا الأسبوع
SCHED_FIELDS = ("shift", "day_off", "break_start", "break_min", "off_locked")


def week_start_of(d: date) -> date:
    return d - timedelta(days=(d.weekday() + 1) % 7)


def week_number(ws: date):
    end = ws + timedelta(days=6)
    first = week_start_of(date(end.year, 1, 1))
    return end.year, (ws - first).days // 7 + 1


def week_info(ws: date) -> dict:
    year, no = week_number(ws)
    end = ws + timedelta(days=6)
    return {"start": ws.isoformat(), "end": end.isoformat(), "number": no, "year": year,
            "label": f"Week {no}"}


def today_local() -> date:
    return (utcnow() + timedelta(hours=3)).date()      # توقيت القاهرة تقريبًا


def week_param(value: Optional[date]) -> date:
    """أي تاريخ بيتحوّل ليوم الأحد بتاع أسبوعه. من غير قيمة = أسبوع النهارده."""
    return week_start_of(value or today_local())


def _copy_row(src, ws, employee_id=None):
    return WeekSchedule(week_start=ws, employee_id=employee_id or src.employee_id, shift=src.shift,
                        day_off=src.day_off, break_start=src.break_start, break_min=src.break_min,
                        off_locked=bool(src.off_locked))


def peek_week(db: Session, ws: date) -> dict:
    """جدول الأسبوع لو موجود (من غير ما ننشئه). فاضي لو الأسبوع ما اتفتحش قبل كده."""
    return {r.employee_id: r for r in db.query(WeekSchedule).filter(WeekSchedule.week_start == ws).all()}


def ensure_week(db: Session, ws: date) -> dict:
    """جدول الأسبوع {employee_id: WeekSchedule}. أول مرة الأسبوع بيتفتح بياخد نسخة من أقرب أسبوع
    قبله (أو بعده لو مفيش)، وبعد كده بيبقى مستقل تمامًا: أي تعديل فيه ما بيأثرش في أسبوع تاني."""
    rows = peek_week(db, ws)
    if rows:
        return rows
    prev = (db.query(func.max(WeekSchedule.week_start)).filter(WeekSchedule.week_start < ws).scalar())
    src_week = prev or db.query(func.min(WeekSchedule.week_start)).filter(WeekSchedule.week_start > ws).scalar()
    if src_week:
        src = db.query(WeekSchedule).filter(WeekSchedule.week_start == src_week).all()
        new = [_copy_row(r, ws) for r in src]
    else:
        new = [WeekSchedule(week_start=ws, employee_id=e.id, shift=e.shift, day_off=e.day_off,
                            break_start=e.break_start, break_min=e.break_min,
                            off_locked=bool(e.off_locked)) for e in db.query(Employee).all()]
    if not new:
        return {}
    try:
        db.add_all(new)
        db.commit()
    except IntegrityError:                  # طلب تاني فتح نفس الأسبوع في نفس اللحظة
        db.rollback()
    return peek_week(db, ws)


def migrate_weeks():
    """مرة واحدة: الجدول الحالي (شيفت/إجازة/بريك على الموظف) يبقى جدول Week 44."""
    db = SessionLocal()
    try:
        if db.query(WeekSchedule.id).first() is None and db.query(Employee.id).first() is not None:
            ensure_week(db, BASE_WEEK)
    finally:
        db.close()


migrate_weeks()


def ov(e, s=None):
    """الموظف + جدول أسبوعه (لو متاح) كـ dict جاهز للواجهة."""
    d = employee_dict(e)
    if s is not None:
        d.update(shift=s.shift, day_off=s.day_off, break_start=s.break_start or "",
                 break_min=s.break_min or 60, off_locked=bool(s.off_locked))
    return d


def week_pairs(db: Session, ws: date, branch: Optional[str] = None, active_only: bool = True):
    """[(Employee, WeekSchedule)] للموظفين اللي ليهم جدول في الأسبوع ده."""
    smap = ensure_week(db, ws)
    q = db.query(Employee)
    if active_only:
        q = q.filter(Employee.active.is_(True))
    if branch in BRANCHES:
        q = q.filter(Employee.branch == branch)
    return [(e, smap[e.id]) for e in q.order_by(Employee.branch, Employee.name).all() if e.id in smap]


def set_sched(db: Session, e, ws: date, **vals):
    """يكتب شيفت/إجازة/بريك لأسبوع واحد بس. لو الأسبوع هو أسبوع النهارده بنحدّث نسخة الموظف الأساسية كمان."""
    smap = ensure_week(db, ws)
    s = smap.get(e.id)
    if s is None:
        s = WeekSchedule(week_start=ws, employee_id=e.id, shift=e.shift, day_off=e.day_off,
                         break_start=e.break_start, break_min=e.break_min, off_locked=bool(e.off_locked))
        db.add(s)
        smap[e.id] = s
    for k, v in vals.items():
        if k in SCHED_FIELDS:
            setattr(s, k, v)
    if ws == week_param(None):
        for k in SCHED_FIELDS:
            setattr(e, k, getattr(s, k))
    return s


def add_to_weeks(db: Session, e, ws: date, **vals):
    """موظف جديد: يدخل جدول الأسبوع ده وكل الأسابيع اللي بعده واللي اتفتحت قبل كده."""
    base = dict(shift=e.shift, day_off=e.day_off, break_start=e.break_start, break_min=e.break_min,
                off_locked=bool(e.off_locked))
    base.update({k: v for k, v in vals.items() if k in SCHED_FIELDS})
    weeks = {ws} | {w for (w,) in db.query(WeekSchedule.week_start).filter(WeekSchedule.week_start > ws).distinct().all()}
    have = {w for (w,) in db.query(WeekSchedule.week_start).filter(WeekSchedule.employee_id == e.id).all()}
    for w in weeks - have:
        db.add(WeekSchedule(week_start=w, employee_id=e.id, **base))


if ADMIN_PASSWORD == "ChangeThisPassword123!":
    print("WARNING: ADMIN_PASSWORD is the default value. Set a strong ADMIN_PASSWORD environment variable.")

app = FastAPI(title=APP_NAME, version="2.1.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
security = HTTPBearer(auto_error=False)

# هيدرز أمنية لكل الردود: تطبيق داخلي مش محتاج يتحط في iframe، ومفيش داعي
# إن المتصفح يخمّن نوع الملف. HSTS بنسيبها للبروكسي بتاع Railway.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


# أيقونة التطبيق: SVG صغير مضمّن عشان نبطل 404 على /favicon.ico
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#2563eb"/><stop offset="1" stop-color="#60a5fa"/>'
    '</linearGradient></defs><rect width="64" height="64" rx="18" fill="url(#g)"/>'
    '<text x="32" y="46" font-family="Alexandria,Cairo,Segoe UI,Arial,sans-serif" font-size="38" '
    'font-weight="800" fill="#ffffff" text-anchor="middle">R</text></svg>'
)


@app.get("/favicon.svg", include_in_schema=False)
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------- auth
def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(role: str) -> str:
    exp = str(int(time.time()) + TOKEN_TTL_SECONDS)
    return f"{role}.{exp}.{_sign(role + '|' + exp)}"


def token_role(token: str) -> Optional[str]:
    """يرجع الدور (admin/supervisor) لو التوكن سليم، وإلا None. التوكنات القديمة (بدون دور) بتبقى غير صالحة."""
    try:
        role, exp, sig = token.split(".", 2)
        if role not in ("admin", "supervisor"):
            return None
        return role if (hmac.compare_digest(sig, _sign(role + "|" + exp)) and int(exp) > time.time()) else None
    except Exception:
        return None


def valid_token(token: str) -> bool:
    return token_role(token) is not None


def require_auth(credentials: HTTPAuthorizationCredentials = Depends(security)):
    role = token_role(credentials.credentials) if credentials else None
    if not role:
        raise HTTPException(status_code=401, detail="جلسة الدخول غير صالحة. سجّل الدخول مرة أخرى.")
    return role


def require_admin(role: str = Depends(require_auth)):
    if role != "admin":
        raise HTTPException(status_code=403, detail="العملية دي متاحة للأدمن فقط.")
    return role


_login_state: Dict[str, List[float]] = {}  # ip -> [failed_count, locked_until]
MAX_FAILS = 8
LOCK_SECONDS = 300
MAX_TRACKED_IPS = 512


def client_ip(request: Request) -> str:
    """عنوان العميل اللي بنقفل عليه المحاولات الفاشلة.

    بناخد **آخر** عنوان في X-Forwarded-For مش أوله: البروكسي (Railway) هو اللي بيضيف
    عنوان العميل الحقيقي في آخر السلسلة، وأي قيمة العميل بعتها بنفسه بتفضل في الأول.
    لو خدنا أول عنصر، أي حد يقدر يبعت هيدر مزيّف ويتخطى قفل تسجيل الدخول تمامًا.
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        hops = [p.strip() for p in fwd.split(",") if p.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


def _prune_login_state(now: float) -> None:
    """تنظيف دوري عشان الديكشنري مايكبرش بلا حدود لو حد ضرب بـ IPs كتير."""
    if len(_login_state) < MAX_TRACKED_IPS:
        return
    for ip, (fails, locked_until) in list(_login_state.items()):
        if not fails and locked_until <= now:
            _login_state.pop(ip, None)


# ---------------------------------------------------------------- schemas
class LoginIn(BaseModel):
    password: str


class EmployeeIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    employee_code: Optional[str] = Field(default=None, max_length=60)
    branch: str
    shift: str = Field(min_length=1, max_length=80)
    employment_type: str
    day_off: str = "Friday"
    phone: Optional[str] = Field(default=None, max_length=30)
    active: bool = True


class AttendanceItem(BaseModel):
    employee_id: int
    status: str = ""  # فاضي = مسح التسجيل
    fine: float = Field(default=0, ge=0, le=1000000)
    reason: str = Field(default="", max_length=500)
    note: str = Field(default="", max_length=500)
    # الـstamp اللي العميل شافه وقت تحميل الصفحة. لو السجل اتعدّل بعده بنرفض الحفظ
    # بدل ما نكتب فوق شغل حد تاني (قفل متفائل).
    expected_updated_at: str = Field(default="", max_length=40)


class AttendanceIn(BaseModel):
    employee_id: int
    attendance_date: date
    status: str
    fine: float = Field(default=0, ge=0, le=1000000)
    reason: str = Field(default="", max_length=500)
    note: str = Field(default="", max_length=500)
    expected_updated_at: str = Field(default="", max_length=40)


class AttendanceBulkIn(BaseModel):
    attendance_date: date
    items: List[AttendanceItem]


class MoveBranchIn(BaseModel):
    branch: str


class BulkEmployeesIn(BaseModel):
    ids: List[int]
    action: str  # activate | deactivate | update
    branch: Optional[str] = None
    shift: Optional[str] = Field(default=None, max_length=80)
    employment_type: Optional[str] = None
    day_off: Optional[str] = None


class ImportIn(BaseModel):
    rows: List[dict]
    update_existing: bool = True
    dry_run: bool = False


class SettingsIn(BaseModel):
    fine_late: float = Field(default=0, ge=0, le=100000)
    fine_absent: float = Field(default=0, ge=0, le=100000)


# ---------------------------------------------------------------- helpers
def employee_dict(e):
    return {
        "id": e.id, "name": e.name, "employee_code": e.employee_code or "",
        "branch": e.branch, "shift": e.shift, "employment_type": e.employment_type,
        "day_off": e.day_off, "active": e.active, "phone": e.phone or "",
        "break_start": e.break_start or "", "break_min": e.break_min or 60,
        "off_locked": bool(e.off_locked)
    }


def attendance_dict(a):
    return {
        "id": a.id, "employee_id": a.employee_id,
        "attendance_date": a.attendance_date.isoformat(), "status": a.status,
        "fine": a.fine, "reason": a.reason or "", "note": a.note or "",
        "updated_at": a.updated_at.isoformat() if a.updated_at else ""
    }


def csv_safe(v):
    """يمنع حقن المعادلات في Excel (نص يبدأ بـ = + - @)."""
    if isinstance(v, str) and v and v[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def csv_response(header, rows, filename):
    buffer = io.StringIO()
    buffer.write("﻿")
    writer = csv.writer(buffer)
    writer.writerow(header)
    for r in rows:
        writer.writerow([csv_safe(x) for x in r])
    return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def norm_branch(v):
    s = str(v or "").translate(_AR_DIGITS).strip().lower().replace(" ", "")
    return {"qcd1": "QCD 1", "1": "QCD 1", "qcd2": "QCD 2", "2": "QCD 2"}.get(s)


def norm_type(v):
    s = str(v or "").strip().lower().replace(" ", "").replace("-", "")
    if s in ("ft", "fulltime", "full", "كامل", "فولتايم", "فول", "دوامكامل"):
        return "FT"
    if s in ("pt", "parttime", "part", "جزئي", "بارتتايم", "بارت", "دوامجزئي"):
        return "PT"
    return None


_EN_DAYS = {d.lower(): d for d in WEEKDAYS}
_EN_DAYS.update({d.lower()[:3]: d for d in WEEKDAYS})
_AR_DAYS = {"سبت": "Saturday", "احد": "Sunday", "اثنين": "Monday", "ثلاثاء": "Tuesday",
            "اربعاء": "Wednesday", "خميس": "Thursday", "جمعه": "Friday"}


def norm_day(v):
    s = str(v or "").strip().lower()
    if s in _EN_DAYS:
        return _EN_DAYS[s]
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه").replace(" ", "")
    if s.startswith("ال"):
        s = s[2:]
    return _AR_DAYS.get(s)


def check_employee_values(branch, employment_type, day_off):
    if branch not in BRANCHES:
        raise HTTPException(400, "الفرع يجب أن يكون QCD 1 أو QCD 2")
    if employment_type not in EMP_TYPES:
        raise HTTPException(400, "نوع التعاقد يجب أن يكون FT أو PT")
    if day_off not in WEEKDAYS:
        raise HTTPException(400, "يوم الإجازة غير صحيح")


def get_settings(db: Session) -> dict:
    stored = {s.key: s.value for s in db.query(Setting).all()}
    out = {}
    for key in ("fine_late", "fine_absent"):
        try:
            out[key] = float(stored.get(key, 0))
        except ValueError:
            out[key] = 0.0
    return out


# ---------------------------------------------------------------- live updates
class EventBus:
    """باص أحداث في الذاكرة للتحديث المباشر بين كل التابات والمستخدمين.

    أي عملية كتابة بتنشر حدث هنا، والتابات المتصلة بتستنى على `/api/events`
    (long-poll) فتوصلها التغييرات في نفس اللحظة من غير أي refresh من المستخدم.
    الأحداث في الذاكرة بس: بعد إعادة تشغيل السيرفر بتبدأ من جديد، والتاب اللي
    كان متصل بياخد `reset` ويعمل تحديث كامل مرة واحدة.
    """

    def __init__(self, keep: int = 150):
        self._cv = threading.Condition()
        self._rev = 0
        self._events: List[dict] = []
        self._keep = keep

    def publish(self, kind: str, **data) -> dict:
        with self._cv:
            self._rev += 1
            event = {"rev": self._rev, "kind": kind, "at": utcnow().isoformat(), **data}
            self._events.append(event)
            if len(self._events) > self._keep:
                self._events = self._events[-self._keep:]
            self._cv.notify_all()
            return event

    def _payload(self, since: int) -> dict:
        """أحداث بعد `since` + تنبيه لو العميل فوّت أحداث (السيرفر اتعمله restart
        أو الطابور اتمسح من كتر الأحداث)."""
        missed = since > self._rev or (since > 0 and bool(self._events) and since < self._events[0]["rev"] - 1)
        return {"rev": self._rev, "events": [e for e in self._events if e["rev"] > since],
                "reset": bool(missed)}

    def snapshot(self, since: int) -> dict:
        with self._cv:
            return self._payload(since)

    def wait(self, since: int, timeout: float) -> dict:
        """بيستنى لحد ما يحصل أي تغيير أو الوقت يخلص (نبضة كل فترة عشان الاتصال ما يقفلش)."""
        with self._cv:
            if self._rev <= since:
                self._cv.wait(timeout)
            return self._payload(since)


EVENTS = EventBus()


def who_role(role: str) -> str:
    return "أدمن" if role == "admin" else "مشرف"


def client_tag(request: Request) -> str:
    """معرّف التاب اللي بعت الطلب — التاب بتتجاهل الأحداث اللي هي مصدرها."""
    return (request.headers.get("x-client-id") or "")[:40]


def publish_event(kind: str, **data) -> None:
    """بينشر تغيير لكل التابات المتصلة. بيتنده بعد نجاح الكتابة (بعد الـcommit)."""
    EVENTS.publish(kind, **data)


# ---------------------------------------------------------------- pages / auth routes
def _asset_stamp() -> str:
    """بصمة قصيرة لملفات الواجهة.

    بتتغيّر تلقائيًا مع أي تعديل، فالمتصفح بيحمّل النسخة الجديدة بعد كل نشر
    ومفيش حاجة تنسى تزوّد رقم `?v=` يدويًا.
    """
    h = hashlib.sha256()
    for rel in ("static/style.css", "static/app.js", "static/ui.js"):
        try:
            st = os.stat(rel)
            h.update(f"{rel}:{st.st_size}:{st.st_mtime_ns}".encode())
        except OSError:
            h.update(rel.encode())
    return h.hexdigest()[:10]


ASSET_STAMP = _asset_stamp()


@app.get("/", response_class=HTMLResponse)
def home():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read().replace("__ASSET_V__", ASSET_STAMP)


@app.get("/api/health")
def health():
    return {"status": "ok", "app": APP_NAME}


@app.post("/api/login")
def login(data: LoginIn, request: Request):
    ip = client_ip(request)
    now = time.time()
    _prune_login_state(now)
    fails, locked_until = _login_state.get(ip, [0, 0.0])
    if locked_until > now:
        minutes = int((locked_until - now) // 60) + 1
        raise HTTPException(429, f"محاولات كثيرة خاطئة. حاول مرة أخرى بعد {minutes} دقيقة.")
    if secrets.compare_digest(data.password.encode(), ADMIN_PASSWORD.encode()):
        role = "admin"
    elif SUPERVISOR_PASSWORD and secrets.compare_digest(data.password.encode(), SUPERVISOR_PASSWORD.encode()):
        role = "supervisor"
    else:
        fails += 1
        if fails >= MAX_FAILS:
            _login_state[ip] = [0, now + LOCK_SECONDS]
        else:
            _login_state[ip] = [fails, 0.0]
        raise HTTPException(401, "كلمة المرور غير صحيحة")
    _login_state.pop(ip, None)
    return {"token": make_token(role), "role": role, "message": "تم تسجيل الدخول"}


@app.get("/api/me")
def me(role: str = Depends(require_auth)):
    return {"app": APP_NAME, "authenticated": True, "role": role}


@app.get("/api/events")
def live_events(since: int = Query(0, ge=0), wait: float = Query(20, ge=0, le=30),
                _: bool = Depends(require_auth)):
    """التحديث المباشر: long-poll بترجع فورًا لو في تغيير، أو بعد `wait` ثانية كحد أقصى.

    الواجهة بتناديها في حلقة مستمرة بـ`since` = آخر rev وصلها، فأي تسجيل من أي
    مستخدم تاني بيظهر عندها في نفس اللحظة بدون refresh.
    """
    return EVENTS.wait(since, wait) if wait else EVENTS.snapshot(since)


# ---------------------------------------------------------------- settings
@app.get("/api/settings")
def read_settings(_: bool = Depends(require_auth), db: Session = Depends(get_db)):
    return get_settings(db)


@app.put("/api/settings")
def update_settings(data: SettingsIn, request: Request, role: str = Depends(require_admin),
                    db: Session = Depends(get_db)):
    for key, value in (("fine_late", data.fine_late), ("fine_absent", data.fine_absent)):
        row = db.query(Setting).filter(Setting.key == key).first()
        if row:
            row.value = str(value)
        else:
            db.add(Setting(key=key, value=str(value)))
    db.commit()
    publish_event("settings", by=who_role(role), client=client_tag(request))
    return get_settings(db)


# ---------------------------------------------------------------- employees
@app.get("/api/employees")
def list_employees(branch: Optional[str] = None, active_only: bool = False, week: Optional[date] = None,
                   _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    """موظفين الأسبوع المختار (الشيفت والإجازة والبريك بتاعة الأسبوع ده)."""
    ws = week_param(week)
    smap = ensure_week(db, ws)
    q = db.query(Employee)
    if branch in BRANCHES:
        q = q.filter(Employee.branch == branch)
    if active_only:
        q = q.filter(Employee.active.is_(True))
    return [ov(e, smap[e.id]) for e in q.order_by(Employee.branch, Employee.name).all() if e.id in smap]


@app.post("/api/employees")
def create_employee(data: EmployeeIn, request: Request, week: Optional[date] = None,
                    role: str = Depends(require_admin), db: Session = Depends(get_db)):
    check_employee_values(data.branch, data.employment_type, data.day_off)
    code = (data.employee_code or "").strip() or None
    if code and db.query(Employee).filter(Employee.employee_code == code).first():
        raise HTTPException(409, "كود الموظف مستخدم بالفعل")
    e = Employee(name=data.name.strip(), employee_code=code, branch=data.branch, shift=data.shift.strip(),
                 employment_type=data.employment_type, day_off=data.day_off, active=data.active,
                 phone=(data.phone or "").strip() or None)
    db.add(e)
    db.flush()
    ws = week_param(week)
    ensure_week(db, ws)
    add_to_weeks(db, e, ws)
    db.commit()
    db.refresh(e)
    publish_event("employees", action="create", names=[e.name], by=who_role(role),
                  client=client_tag(request))
    return ov(e, peek_week(db, ws).get(e.id))


@app.put("/api/employees/{employee_id}")
def update_employee(employee_id: int, data: EmployeeIn, request: Request, week: Optional[date] = None,
                    role: str = Depends(require_admin), db: Session = Depends(get_db)):
    e = db.query(Employee).filter(Employee.id == employee_id).first()
    if not e:
        raise HTTPException(404, "الموظف غير موجود")
    check_employee_values(data.branch, data.employment_type, data.day_off)
    code = (data.employee_code or "").strip() or None
    if code and db.query(Employee).filter(Employee.employee_code == code, Employee.id != employee_id).first():
        raise HTTPException(409, "كود الموظف مستخدم بالفعل")
    e.name = data.name.strip()
    e.employee_code = code
    e.branch = data.branch
    e.employment_type = data.employment_type
    e.active = data.active
    e.phone = (data.phone or "").strip() or None
    # الشيفت ويوم الإجازة بيتغيّروا في الأسبوع المختار بس
    ws = week_param(week)
    s = set_sched(db, e, ws, shift=data.shift.strip(), day_off=data.day_off)
    db.commit()
    db.refresh(e)
    publish_event("employees", action="update", names=[e.name], by=who_role(role),
                  client=client_tag(request))
    return ov(e, s)


@app.post("/api/employees/{employee_id}/move")
def move_employee_branch(employee_id: int, data: MoveBranchIn, request: Request,
                         role: str = Depends(require_admin), db: Session = Depends(get_db)):
    """نقل موظف لفرع آخر مع الحفاظ على كل بياناته: الشيفت، يوم الإجازة، البريك،
    نوع التعاقد، والبريك المثبّت. الفرع هو الحاجة الوحيدة اللي بتتغيّر، وسجل الحضور
    والغرامات بيفضل محفوظ زي ما هو."""
    e = db.query(Employee).filter(Employee.id == employee_id).first()
    if not e:
        raise HTTPException(404, "الموظف غير موجود")
    if data.branch not in BRANCHES:
        raise HTTPException(400, "الفرع غير صحيح")
    old_branch = e.branch
    if data.branch == old_branch:
        raise HTTPException(400, "الموظف في الفرع ده بالفعل")
    e.branch = data.branch
    # نشتغل بالشيفت القديم في الفرع الجديد: لو مش معمول فيه نعريف شيفت بنضيفه
    # عشان صفحة الجدول والفلاتر تشوفه من غير خطوة إضافية.
    for (sh_name,) in db.query(WeekSchedule.shift).filter(WeekSchedule.employee_id == e.id).distinct().all():
        if sh_name and not db.query(ShiftDef).filter(ShiftDef.branch == data.branch, ShiftDef.name == sh_name).first():
            db.add(ShiftDef(branch=data.branch, name=sh_name))
    db.commit()
    db.refresh(e)
    publish_event("employees", action="move", names=[e.name], branch=e.branch,
                  by=who_role(role), client=client_tag(request))
    return {"employee": employee_dict(e), "from": old_branch,
            "message": f"تم نقل {e.name} من {old_branch} إلى {e.branch} بنفس الشيفت والإجازة"}


@app.delete("/api/employees/{employee_id}")
def delete_employee(employee_id: int, request: Request, from_week: Optional[date] = None,
                    role: str = Depends(require_admin), db: Session = Depends(get_db)):
    e = db.query(Employee).filter(Employee.id == employee_id).first()
    if not e:
        raise HTTPException(404, "الموظف غير موجود")
    if from_week is not None:
        # من صفحة الجدول: الموظف بيطلع من جدول الأسبوع ده وما بعده بس. الأسابيع اللي قبله وسجل الحضور زي ما هما.
        ws = week_param(from_week)
        db.query(WeekSchedule).filter(WeekSchedule.employee_id == e.id, WeekSchedule.week_start >= ws).delete(
            synchronize_session=False)
        db.commit()
        publish_event("employees", action="deactivate", names=[e.name], by=who_role(role),
                      client=client_tag(request))
        return {"message": "تمت إزالة الموظف من جدول الأسبوع ده وما بعده مع الاحتفاظ بسجل حضوره"}
    # نحافظ على سجل الحضور القديم: إيقاف بدل الحذف النهائي.
    e.active = False
    db.commit()
    publish_event("employees", action="deactivate", names=[e.name], by=who_role(role),
                  client=client_tag(request))
    return {"message": "تم إيقاف الموظف مع الاحتفاظ بسجل حضوره السابق"}


@app.post("/api/employees/bulk")
def bulk_employees(data: BulkEmployeesIn, request: Request, week: Optional[date] = None,
                   role: str = Depends(require_admin), db: Session = Depends(get_db)):
    if not data.ids:
        raise HTTPException(400, "لم يتم تحديد أي موظف")
    if len(data.ids) > 2000:
        raise HTTPException(400, "العدد المحدد كبير جدًا")
    if data.action not in ("activate", "deactivate", "update"):
        raise HTTPException(400, "إجراء غير معروف")
    emps = db.query(Employee).filter(Employee.id.in_(data.ids)).all()
    if data.action == "update":
        if data.branch is not None and data.branch not in BRANCHES:
            raise HTTPException(400, "الفرع غير صحيح")
        if data.employment_type is not None and data.employment_type not in EMP_TYPES:
            raise HTTPException(400, "نوع التعاقد غير صحيح")
        if data.day_off is not None and data.day_off not in WEEKDAYS:
            raise HTTPException(400, "يوم الإجازة غير صحيح")
        shift = (data.shift or "").strip() or None
        if all(v is None for v in (data.branch, shift, data.employment_type, data.day_off)):
            raise HTTPException(400, "اختر قيمة واحدة على الأقل للتعديل")
    for e in emps:
        if data.action == "activate":
            e.active = True
        elif data.action == "deactivate":
            e.active = False
        else:
            if data.branch is not None:
                e.branch = data.branch
            if data.employment_type is not None:
                e.employment_type = data.employment_type
            sched = {}
            if shift is not None:
                sched["shift"] = shift
            if data.day_off is not None:
                sched["day_off"] = data.day_off
            if sched:
                set_sched(db, e, week_param(week), **sched)
    db.commit()
    publish_event("employees", action=data.action, names=[e.name for e in emps[:3]],
                  count=len(emps), by=who_role(role), client=client_tag(request))
    return {"updated": len(emps)}


@app.post("/api/employees/import")
def import_employees(data: ImportIn, request: Request, week: Optional[date] = None,
                     role: str = Depends(require_admin), db: Session = Depends(get_db)):
    if not data.rows:
        raise HTTPException(400, "الملف فاضي")
    if len(data.rows) > 5000:
        raise HTTPException(400, "الحد الأقصى 5000 صف في المرة الواحدة")
    by_code, by_name = {}, {}
    for e in db.query(Employee).all():
        if e.employee_code:
            by_code[e.employee_code] = e
        by_name.setdefault((e.name.strip().lower(), e.branch), e)
    created = updated = skipped = 0
    errors = []
    ws = week_param(week)
    ensure_week(db, ws)

    for idx, r in enumerate(data.rows, start=1):
        def err(msg):
            errors.append({"row": idx, "name": str(r.get("name") or ""), "detail": msg})

        name = str(r.get("name") or "").strip()
        code = str(r.get("employee_code") or "").strip() or None
        shift = str(r.get("shift") or "").strip()
        phone = str(r.get("phone") or "").strip() or None
        branch = norm_branch(r.get("branch"))
        raw_type, raw_day = str(r.get("employment_type") or "").strip(), str(r.get("day_off") or "").strip()
        etype = norm_type(raw_type) if raw_type else None   # فاضي = الافتراضي للجديد، وبدون تغيير للموجود
        day = norm_day(raw_day) if raw_day else None
        if raw_type and not etype:
            err("نوع التعاقد غير صحيح (FT أو PT)"); continue
        if raw_day and not day:
            err("يوم الإجازة غير صحيح"); continue
        raw_active = str(r.get("active") or "").strip().lower()
        active = raw_active not in ("no", "0", "false", "n", "لا", "موقوف", "غير نشط")

        if not name or len(name) > 120:
            err("الاسم مطلوب (حتى 120 حرف)"); continue
        if not branch:
            err("الفرع غير صحيح (QCD 1 أو QCD 2)"); continue
        if not shift or len(shift) > 80:
            err("الشيفت مطلوب (حتى 80 حرف)"); continue
        if code and len(code) > 60:
            err("الكود أطول من 60 حرف"); continue
        if phone and len(phone) > 30:
            err("رقم الهاتف أطول من 30 حرف"); continue

        match = None
        if code:
            match = by_code.get(code)
            if not match:
                cand = by_name.get((name.lower(), branch))
                if cand is not None and not cand.employee_code:
                    match = cand
        else:
            match = by_name.get((name.lower(), branch))

        if match is not None:
            if not data.update_existing:
                skipped += 1
                continue
            if code and by_code.get(code) not in (None, match):
                err("الكود مستخدم لموظف آخر"); continue
            match.name, match.branch = name, branch
            if etype:
                match.employment_type = etype
            sched = {"shift": shift}
            if day:
                sched["day_off"] = day
            set_sched(db, match, ws, **sched)
            match.active = active
            if phone:
                match.phone = phone
            if code and not match.employee_code:
                match.employee_code = code
                by_code[code] = match
            updated += 1
        else:
            if code and code in by_code:
                err("الكود مكرر داخل الملف"); continue
            e = Employee(name=name, employee_code=code, branch=branch, shift=shift,
                         employment_type=etype or "FT", day_off=day or "Friday", active=active, phone=phone)
            db.add(e)
            db.flush()
            add_to_weeks(db, e, ws)
            if code:
                by_code[code] = e
            by_name.setdefault((name.lower(), branch), e)
            created += 1

    if data.dry_run:
        db.rollback()
    else:
        db.commit()
        if created or updated:
            publish_event("employees", action="import", count=created + updated,
                          names=[], by=who_role(role), client=client_tag(request))
    return {"created": created, "updated": updated, "skipped": skipped,
            "errors": errors, "dry_run": data.dry_run}


@app.get("/api/employees/export.csv")
def export_employees(week: Optional[date] = None, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    ws = week_param(week)
    smap = ensure_week(db, ws)
    rows = [(e, smap[e.id]) for e in db.query(Employee).all() if e.id in smap]
    rows.sort(key=lambda p: (p[0].branch, p[1].shift, p[0].name))
    return csv_response(
        ["name", "employee_code", "branch", "shift", "employment_type", "day_off", "phone", "active"],
        [[e.name, e.employee_code or "", e.branch, s.shift, e.employment_type, s.day_off,
          e.phone or "", "yes" if e.active else "no"] for e, s in rows],
        f"bankai_employees_week{week_number(ws)[1]}.csv")


# ---------------------------------------------------------------- roster & attendance
@app.get("/api/roster")
def roster(attendance_date: date = Query(...), branch: Optional[str] = None,
           _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    weekday = attendance_date.strftime("%A")
    ws = week_start_of(attendance_date)
    pairs = week_pairs(db, ws, branch)
    working = sorted([(e, s) for e, s in pairs if s.day_off != weekday], key=lambda p: (p[0].branch, p[1].shift, p[0].name))
    off = [(e, s) for e, s in pairs if s.day_off == weekday]
    ids = [e.id for e, _ in working]
    existing = {}
    if ids:
        rows = db.query(Attendance).filter(Attendance.attendance_date == attendance_date,
                                           Attendance.employee_id.in_(ids)).all()
        existing = {a.employee_id: attendance_dict(a) for a in rows}
    out = []
    for e, s in working:
        row = ov(e, s)
        row["attendance"] = existing.get(e.id)
        out.append(row)
    return {"date": attendance_date.isoformat(), "weekday": weekday, "week": week_info(ws), "employees": out,
            "off_employees": [ov(e, s) for e, s in off]}


def _parse_stamp(value: str) -> Optional[datetime]:
    """يحوّل الـstamp اللي جاي من الواجهة (نص ISO) لتاريخ. أي قيمة غير مفهومة بتتجاهَل."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return stamp.replace(tzinfo=None) if stamp.tzinfo else stamp


def _save_items(db: Session, day: date, items: List[AttendanceItem], _retry: bool = False):
    """حفظ/تعديل/مسح تسجيلات يوم واحد.

    كل صف بيتكتب بأعمدته الأربعة كاملة (status/fine/reason/note) في جملة UPDATE
    واحدة، ومعاها شرط على updated_at اللي السيرفر قراه — فلو حد تاني عدّل نفس الصف
    في اللحظة دي، التعديل القديم بيترفض بتعارض واضح بدل ما يخلط قيم من مصدرين.
    الجمل بتتنفّذ مجمّعة (executemany) عشان الحفظ الجماعي يبقى جملة واحدة مش جملة لكل موظف.
    """
    weekday = day.strftime("%A")
    wanted: Dict[int, AttendanceItem] = {}
    for it in items:                       # آخر تسجيل لنفس الموظف في الطلب هو اللي يمشي
        wanted[it.employee_id] = it
    ids = list(wanted.keys())
    emps = {e.id: e for e in db.query(Employee).filter(Employee.id.in_(ids)).all()}
    smap = ensure_week(db, week_start_of(day))
    # بنقرا المفاتيح والـstamps بس (مش كائنات ORM) عشان مفيش أي كائن قديم يتكتب بعد الكتابة
    rows = db.execute(select(Attendance.id, Attendance.employee_id, Attendance.updated_at).where(
        Attendance.attendance_date == day, Attendance.employee_id.in_(ids))).all()
    row_ids = {r.employee_id: r.id for r in rows}
    stamps = {r.employee_id: r.updated_at for r in rows}
    saved = cleared = 0
    errors, touched, names = [], [], []
    inserts: Dict[int, dict] = {}
    updates: Dict[int, dict] = {}
    updates_no_stamp: Dict[int, dict] = {}
    clear_ids: List[int] = []
    for eid, it in wanted.items():
        e = emps.get(eid)
        if not e:
            errors.append({"employee_id": eid, "detail": "الموظف غير موجود"}); continue
        if not e.active:
            errors.append({"employee_id": e.id, "name": e.name, "detail": "الموظف غير نشط"}); continue
        sched = smap.get(eid)
        if sched is None:
            errors.append({"employee_id": e.id, "name": e.name, "detail": "الموظف مش مضاف في جدول الأسبوع ده"}); continue
        if sched.day_off == weekday:
            errors.append({"employee_id": e.id, "name": e.name, "detail": "هذا الموظف في إجازة في هذا اليوم"}); continue
        status = (it.status or "").strip()
        row_id = row_ids.get(eid)
        touched.append(eid)
        if status not in STATUSES and status != "":
            errors.append({"employee_id": e.id, "name": e.name, "detail": "حالة الحضور غير صحيحة"}); continue
        if status == "":
            if row_id is not None:
                clear_ids.append(eid)
                cleared += 1
                names.append(e.name)
            continue
        fine = 0.0 if status in NO_FINE_STATUSES else float(it.fine or 0)
        reason = "" if status == "Present" else (it.reason or "").strip()
        note = (it.note or "").strip()
        if row_id is not None:
            current = stamps.get(eid)
            raw = (it.expected_updated_at or "").strip()
            expected = _parse_stamp(raw)
            if raw == NEW_STAMP or (expected is not None and current is not None and expected != current):
                errors.append({"employee_id": e.id, "name": e.name, "conflict": True,
                               "detail": "الموظف ده اتعدّل من مستخدم تاني بعد ما فتحت الصفحة. "
                                         "اضغط تحديث وشوف التسجيل الحالي وبعدين احفظ تاني."})
                touched.pop()
                continue
            values = {"row_id": row_id, "employee_id": eid, "status": status, "fine": fine,
                      "reason": reason, "note": note, "updated_at": utcnow(), "name": e.name}
            if current is None:            # سجل قديم من غير stamp: بنكتبه من غير شرط
                updates_no_stamp[eid] = values
            else:
                # الشرط على الـstamp اللي السيرفر قراه دلوقتي: لو حد عدّل الصف في
                # اللحظة دي، الجملة مش هتلاقي صف والسيرفر يقول تعارض بدل ما يخلط القيم
                values["expected"] = expected if expected is not None else current
                updates[eid] = values
        else:
            inserts[eid] = {"employee_id": eid, "attendance_date": day, "status": status,
                            "fine": fine, "reason": reason, "note": note, "updated_at": utcnow()}
        saved += 1
        names.append(e.name)

    def _apply():
        tbl = Attendance.__table__
        if clear_ids:
            db.execute(tbl.delete().where(tbl.c.attendance_date == day,
                                          tbl.c.employee_id.in_(clear_ids)))
        if inserts:
            db.execute(Attendance.__table__.insert(), list(inserts.values()))
        for batch, guarded in ((updates, True), (updates_no_stamp, False)):
            if not batch:
                continue
            stmt = tbl.update().where(tbl.c.id == bindparam("row_id"))
            if guarded:
                stmt = stmt.where(tbl.c.updated_at == bindparam("expected"))
            stmt = stmt.values(status=bindparam("status"), fine=bindparam("fine"),
                               reason=bindparam("reason"), note=bindparam("note"),
                               updated_at=bindparam("updated_at"))
            batch = list(batch.values())
            args = [{k: v for k, v in values.items() if k in _PARAM_KEYS} for values in batch]
            # synchronize_session=None: إحنا مش محتاجين SQLAlchemy تزامن أي كائن في الجلسة
            result = db.execute(stmt, args, execution_options={"synchronize_session": None})
            if guarded and result.rowcount is not None and result.rowcount < len(batch):
                # حد تاني عدّل صف في نفس اللحظة: بنكررهم واحد واحد عشان نعرف مين بالظبط
                for values, params in zip(batch, args):
                    one = db.execute(stmt, [params], execution_options={"synchronize_session": None})
                    if one.rowcount:
                        continue
                    errors.append({"employee_id": values["employee_id"], "name": values["name"],
                                   "conflict": True,
                                   "detail": "اتعدّل من مستخدم تاني في نفس اللحظة. اضغط تحديث "
                                             "وشوف التسجيل الحالي وبعدين احفظ تاني."})
        db.commit()

    try:
        _apply()
    except IntegrityError:
        db.rollback()
        if _retry:
            raise
        # حالة نادرة: نفس السجل اتحفظ من مكان تاني في نفس اللحظة. بنعيد المحاولة مرة
        # واحدة — المرة دي الصف بقى موجود فبيمشي في مسار التحديث بدل الإضافة.
        return _save_items(db, day, list(wanted.values()), _retry=True)

    # بنقرا التسجيلات المتأثرة مرة واحدة بعد الحفظ عشان نبني الرد
    records = {eid: None for eid in touched}
    if touched:
        for r in db.execute(
                select(Attendance.id, Attendance.employee_id, Attendance.attendance_date,
                       Attendance.status, Attendance.fine, Attendance.reason,
                       Attendance.note, Attendance.updated_at).where(
                    Attendance.attendance_date == day, Attendance.employee_id.in_(touched))).all():
            records[r.employee_id] = attendance_dict(r)
    return {"saved": saved, "cleared": cleared, "errors": errors, "records": records,
            "names": names[:4], "ids": touched}


@app.post("/api/attendance")
def save_attendance(data: AttendanceIn, request: Request, role: str = Depends(require_auth),
                    db: Session = Depends(get_db)):
    if data.status not in STATUSES:
        raise HTTPException(400, "حالة الحضور غير صحيحة")
    item = AttendanceItem(employee_id=data.employee_id, status=data.status, fine=data.fine,
                          reason=data.reason, note=data.note,
                          expected_updated_at=data.expected_updated_at)
    res = _save_items(db, data.attendance_date, [item])
    if res["errors"]:
        first = res["errors"][0]
        code = 409 if first.get("conflict") else (404 if first["detail"] == "الموظف غير موجود" else 400)
        raise HTTPException(code, first["detail"])
    publish_event("attendance", date=data.attendance_date.isoformat(), ids=[data.employee_id],
                  names=res["names"], count=res["saved"], by=who_role(role),
                  client=client_tag(request))
    return res["records"][data.employee_id]


@app.post("/api/attendance/bulk")
def save_attendance_bulk(data: AttendanceBulkIn, request: Request, role: str = Depends(require_auth),
                         db: Session = Depends(get_db)):
    if not data.items:
        raise HTTPException(400, "لا توجد تغييرات للحفظ")
    if len(data.items) > 2000:
        raise HTTPException(400, "عدد التسجيلات كبير جدًا في طلب واحد")
    res = _save_items(db, data.attendance_date, data.items)
    if res["saved"] or res["cleared"]:
        publish_event("attendance", date=data.attendance_date.isoformat(), ids=res["ids"],
                      names=res["names"], count=res["saved"] + res["cleared"],
                      by=who_role(role), client=client_tag(request))
    return {"saved": res["saved"], "cleared": res["cleared"], "errors": res["errors"],
            "records": res["records"]}


# ---------------------------------------------------------------- dashboard
@app.get("/api/dashboard")
def dashboard(attendance_date: date = Query(...), _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    weekday = attendance_date.strftime("%A")
    ws = week_start_of(attendance_date)
    pairs = week_pairs(db, ws)
    smap = {e.id: s for e, s in pairs}
    active = [e for e, _ in pairs]
    att = {a.employee_id: a for a in db.query(Attendance).filter(Attendance.attendance_date == attendance_date).all()}

    def blank():
        return {"scheduled": 0, "present": 0, "late": 0, "absent": 0, "excused": 0, "unmarked": 0, "fines": 0.0}

    totals = blank()
    branches = {b: dict(blank(), shifts={}) for b in BRANCHES}
    off_today = 0
    for e, s in pairs:
        if s.day_off == weekday:
            off_today += 1
            continue
        b = branches.setdefault(e.branch, dict(blank(), shifts={}))
        sh = b["shifts"].setdefault(s.shift, blank())
        a = att.get(e.id)
        key = a.status.lower() if a else "unmarked"
        if key not in totals:
            key = "unmarked"
        for bucket in (totals, b, sh):
            bucket["scheduled"] += 1
            bucket[key] += 1
            if a:
                bucket["fines"] += a.fine or 0
    totals["fines"] = round(totals["fines"], 2)
    for b in branches.values():
        b["fines"] = round(b["fines"], 2)
        b["shifts"] = [dict(shift=k, **dict(v, fines=round(v["fines"], 2))) for k, v in sorted(b["shifts"].items())]

    # الاتجاه والمتابعة بيتحسبوا على الأسبوع ده بس (من الأحد للسبت)
    we = ws + timedelta(days=6)
    trend_rows = (db.query(Attendance.attendance_date, Attendance.status, func.count(Attendance.id))
                  .select_from(Attendance).join(Employee, Attendance.employee_id == Employee.id)
                  .filter(Employee.active.is_(True), Attendance.attendance_date >= ws,
                          Attendance.attendance_date <= we)
                  .group_by(Attendance.attendance_date, Attendance.status).all())
    trend_map = {}
    for d, st, c in trend_rows:
        trend_map.setdefault(d, {})[st] = int(c)
    trend = []
    for i in range(7):
        d = ws + timedelta(days=i)
        m = trend_map.get(d, {})
        trend.append({"date": d.isoformat(), "present": m.get("Present", 0), "late": m.get("Late", 0),
                      "absent": m.get("Absent", 0), "excused": m.get("Excused", 0)})

    w_rows = (db.query(Employee.id, Employee.name, Employee.branch,
                       func.sum(case((Attendance.status == "Absent", 1), else_=0)).label("absent"),
                       func.sum(case((Attendance.status == "Late", 1), else_=0)).label("late"),
                       func.coalesce(func.sum(Attendance.fine), 0).label("fines"))
              .select_from(Attendance).join(Employee, Attendance.employee_id == Employee.id)
              .filter(Employee.active.is_(True), Attendance.attendance_date >= ws,
                      Attendance.attendance_date <= we)
              .group_by(Employee.id, Employee.name, Employee.branch).all())
    watch = []
    for r in w_rows:
        absent, late = int(r.absent or 0), int(r.late or 0)
        if absent or late:
            sc = smap.get(r.id)
            watch.append({"id": r.id, "name": r.name, "branch": r.branch, "shift": sc.shift if sc else "",
                          "absent": absent, "late": late, "fines": round(float(r.fines or 0), 2)})
    watch.sort(key=lambda x: (x["absent"] * 2 + x["late"], x["fines"]), reverse=True)

    wf = db.query(func.coalesce(func.sum(Attendance.fine), 0)).filter(
        Attendance.attendance_date >= ws, Attendance.attendance_date <= we).scalar()
    return {"date": attendance_date.isoformat(), "weekday": weekday, "week": week_info(ws),
            "week_fines": round(float(wf or 0), 2), "active_total": len(active),
            "off_today": off_today, "totals": totals, "branches": branches,
            "trend": trend, "watchlist": watch[:8]}


# ---------------------------------------------------------------- reports
class WeekLookup:
    """بيجيب شيفت/إجازة الموظف في أسبوع تاريخ معيّن (من غير ما ينشئ أسابيع جديدة)."""
    def __init__(self, db):
        self.db, self.cache = db, {}

    def get(self, e, d):
        ws = week_start_of(d)
        if ws not in self.cache:
            self.cache[ws] = peek_week(self.db, ws)
        s = self.cache[ws].get(e.id)
        return s if s is not None else e          # الموظف نفسه عنده shift/day_off احتياطي


def _count_cols():
    return [
        func.count(Attendance.id).label("total"),
        func.sum(case((Attendance.status == "Present", 1), else_=0)).label("present"),
        func.sum(case((Attendance.status == "Late", 1), else_=0)).label("late"),
        func.sum(case((Attendance.status == "Absent", 1), else_=0)).label("absent"),
        func.sum(case((Attendance.status == "Excused", 1), else_=0)).label("excused"),
        func.coalesce(func.sum(Attendance.fine), 0).label("fines"),
    ]


def _counts(r):
    return {"total": int(r.total or 0), "present": int(r.present or 0), "late": int(r.late or 0),
            "absent": int(r.absent or 0), "excused": int(r.excused or 0),
            "fine_total": round(float(r.fines or 0), 2)}


def _range_query(db, cols, start, end, branch=None, q=None):
    query = (db.query(*cols).select_from(Attendance).join(Employee, Attendance.employee_id == Employee.id)
             .filter(Attendance.attendance_date >= start, Attendance.attendance_date <= end))
    if branch in BRANCHES:
        query = query.filter(Employee.branch == branch)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Employee.name.ilike(like), Employee.employee_code.ilike(like)))
    return query


def _check_range(start, end):
    if end < start:
        raise HTTPException(400, "تاريخ النهاية يجب ألا يسبق تاريخ البداية")
    if (end - start).days > 366:
        raise HTTPException(400, "الفترة المسموحة حتى سنة واحدة")


@app.get("/api/reports/summary")
def report_summary(start: date, end: date, branch: Optional[str] = None,
                   _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    _check_range(start, end)
    by_branch = {}
    for r in _range_query(db, [Employee.branch.label("branch")] + _count_cols(), start, end, branch).group_by(Employee.branch).all():
        by_branch[r.branch] = _counts(r)
    total = {"total": 0, "present": 0, "late": 0, "absent": 0, "excused": 0, "fine_total": 0.0}
    for b in by_branch.values():
        for k in total:
            total[k] += b[k]
    total["fine_total"] = round(total["fine_total"], 2)
    by_day = []
    day_rows = (_range_query(db, [Attendance.attendance_date.label("d")] + _count_cols(), start, end, branch)
                .group_by(Attendance.attendance_date).order_by(Attendance.attendance_date).all())
    for r in day_rows:
        by_day.append(dict(_counts(r), date=r.d.isoformat()))
    return {"start": start.isoformat(), "end": end.isoformat(), **total, "by_branch": by_branch, "by_day": by_day}


@app.get("/api/reports/employees")
def report_employees(start: date, end: date, branch: Optional[str] = None, q: Optional[str] = None,
                     _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    _check_range(start, end)
    cols = [Employee.id.label("id"), Employee.name.label("name"), Employee.employee_code.label("code"),
            Employee.branch.label("branch"), Employee.shift.label("shift"),
            Employee.employment_type.label("etype"), Employee.active.label("active")] + _count_cols()
    rows = (_range_query(db, cols, start, end, branch, q)
            .group_by(Employee.id, Employee.name, Employee.employee_code, Employee.branch,
                      Employee.shift, Employee.employment_type, Employee.active)
            .order_by(Employee.branch, Employee.name).all())
    out = []
    smap = peek_week(db, week_start_of(end))
    for r in rows:
        c = _counts(r)
        rate = round((c["present"] + c["late"]) * 100 / c["total"], 1) if c["total"] else 0
        sc = smap.get(r.id)
        out.append(dict(c, id=r.id, name=r.name, employee_code=r.code or "", branch=r.branch,
                        shift=sc.shift if sc else r.shift, employment_type=r.etype, active=bool(r.active), rate=rate))
    return {"start": start.isoformat(), "end": end.isoformat(), "employees": out}


@app.get("/api/reports/employee/{employee_id}")
def employee_history(employee_id: int, start: Optional[date] = None, end: Optional[date] = None,
                     _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    e = db.query(Employee).filter(Employee.id == employee_id).first()
    if not e:
        raise HTTPException(404, "الموظف غير موجود")
    end = end or utcnow().date()
    start = start or (end - timedelta(days=29))
    _check_range(start, end)
    rows = (db.query(Attendance).filter(Attendance.employee_id == employee_id,
                                        Attendance.attendance_date >= start, Attendance.attendance_date <= end)
            .order_by(Attendance.attendance_date.desc()).all())
    stats = {"total": len(rows), "present": 0, "late": 0, "absent": 0, "excused": 0, "fine_total": 0.0}
    records = []
    for a in rows:
        key = a.status.lower()
        if key in stats:
            stats[key] += 1
        stats["fine_total"] += a.fine or 0
        records.append(dict(attendance_dict(a), weekday=a.attendance_date.strftime("%A")))
    stats["fine_total"] = round(stats["fine_total"], 2)
    # قايمة الأسابيع اللي للموظف تسجيلات فيها (كل التاريخ، مش الفترة المفلترة)
    # عشان دروب مينيو السجل يعرض Week N لكل أسبوع اتسجل فيه حاجة.
    day_rows = {r[0] for r in db.query(Attendance.attendance_date)
                .filter(Attendance.employee_id == employee_id).distinct().all()}
    weeks = [{"start": ws.isoformat(), "end": (ws + timedelta(days=6)).isoformat(),
              "number": week_number(ws)[1], "year": week_number(ws)[0]}
             for ws in sorted({week_start_of(d) for d in day_rows}, reverse=True)]
    return {"employee": ov(e, peek_week(db, week_start_of(end)).get(e.id)), "start": start.isoformat(),
            "end": end.isoformat(), "stats": stats, "records": records, "weeks": weeks}


@app.get("/api/reports/export.csv")
def export_csv(start: date, end: date, branch: Optional[str] = None,
               _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    _check_range(start, end)
    q = db.query(Attendance, Employee).join(Employee, Attendance.employee_id == Employee.id).filter(
        Attendance.attendance_date >= start, Attendance.attendance_date <= end)
    if branch in BRANCHES:
        q = q.filter(Employee.branch == branch)
    rows = q.order_by(Attendance.attendance_date, Employee.branch, Employee.name).all()
    look = WeekLookup(db)
    out = []
    for a, e in rows:
        sc = look.get(e, a.attendance_date)
        out.append([week_number(week_start_of(a.attendance_date))[1], a.attendance_date.isoformat(), e.branch,
                    e.employee_code or "", e.name, e.employment_type, sc.shift, sc.day_off, a.status,
                    f"{float(a.fine or 0):.2f}", a.reason or "", a.note or ""])
    return csv_response(
        ["Week", "Date", "Branch", "Employee Code", "Name", "Type", "Shift", "Day Off", "Status", "Fine (EGP)",
         "Reason", "Note"], out, f"bankai_attendance_{start}_{end}.csv")


@app.get("/api/reports/employees.csv")
def export_employees_report(start: date, end: date, branch: Optional[str] = None, q: Optional[str] = None,
                            _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    data = report_employees(start, end, branch, q, True, db)
    return csv_response(
        ["Branch", "Employee Code", "Name", "Type", "Shift", "Records", "Present", "Late", "Absent", "Excused",
         "Attendance Rate %"],
        [[r["branch"], r["employee_code"], r["name"], r["employment_type"], r["shift"], r["total"], r["present"],
          r["late"], r["absent"], r["excused"], r["rate"]] for r in data["employees"]],
        f"bankai_employees_summary_{start}_{end}.csv")


# ---------------------------------------------------------------- fines (الغرامات)
FINES_ITEMS_LIMIT = 2000  # سقف صفوف التفاصيل في الرد — الإجماليات بتتحسب على كل الصفوف دايمًا


def _fines_rows(db, start=None, end=None, branch=None, status=None, q=None):
    """كل تسجيلات الغرامات (الغرامة أكبر من صفر) في الفرعين مع بيانات الموظف."""
    query = (db.query(Attendance, Employee).join(Employee, Attendance.employee_id == Employee.id)
             .filter(Attendance.fine > 0))
    if start:
        query = query.filter(Attendance.attendance_date >= start)
    if end:
        query = query.filter(Attendance.attendance_date <= end)
    if branch in BRANCHES:
        query = query.filter(Employee.branch == branch)
    if status in STATUSES:
        query = query.filter(Attendance.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Employee.name.ilike(like), Employee.employee_code.ilike(like)))
    return query.order_by(Attendance.attendance_date.desc(), Employee.name).all()


def _fines_payload(rows, branch_rows=None, look=None):
    """تجميع الغرامات: إجماليات الفرعين + تفصيل شهري + إجمالي كل موظف + السجل الكامل بالتفاصيل.

    branch_rows: صفوف إجماليات الفرعين لوحدها. لما الفلتر يكون على فرع واحد بنمرّر
    هنا كل الصفوف بتاعة الفترة (من غير فلتر الفرع) عشان عدّادات مفتاح الفروع تفضل
    شايفة الفرعين، بينما الباقي (الشهور/الموظفين/السجل/الإجماليات) بيفضل مخصوص بالفلتر."""
    by_branch = {b: {"branch": b, "count": 0, "total": 0.0, "late": 0, "absent": 0,
                     "late_total": 0.0, "absent_total": 0.0} for b in BRANCHES}
    for a, e in (branch_rows if branch_rows is not None else rows):
        fine = round(float(a.fine or 0), 2)
        kind = "late" if a.status == "Late" else "absent"
        b = by_branch.setdefault(e.branch, {"branch": e.branch, "count": 0, "total": 0.0, "late": 0, "absent": 0,
                                            "late_total": 0.0, "absent_total": 0.0})
        b["count"] += 1
        b["total"] = round(b["total"] + fine, 2)
        b[kind] += 1
        b[kind + "_total"] = round(b[kind + "_total"] + fine, 2)
    by_month, by_employee, by_week, items = {}, {}, {}, []
    total = late_total = absent_total = 0.0
    for a, e in rows:
        fine = round(float(a.fine or 0), 2)
        late = a.status == "Late"
        shift_name = look.get(e, a.attendance_date).shift if look else e.shift
        wstart = week_start_of(a.attendance_date)
        wk = by_week.setdefault(wstart, dict(week_info(wstart), count=0, total=0.0, late=0, absent=0,
                                             late_total=0.0, absent_total=0.0))
        wk["count"] += 1
        wk["total"] = round(wk["total"] + fine, 2)
        wk["late" if late else "absent"] += 1
        wk["late_total" if late else "absent_total"] = round(wk["late_total" if late else "absent_total"] + fine, 2)
        kind = "late" if late else "absent"
        total += fine
        late_total += fine if late else 0.0
        absent_total += 0.0 if late else fine
        mkey = a.attendance_date.strftime("%Y-%m")
        m = by_month.setdefault(mkey, {"month": mkey, "count": 0, "total": 0.0, "late": 0, "absent": 0,
                                       "late_total": 0.0, "absent_total": 0.0})
        m["count"] += 1
        m["total"] = round(m["total"] + fine, 2)
        m[kind] += 1
        m[kind + "_total"] = round(m[kind + "_total"] + fine, 2)
        emp = by_employee.setdefault(e.id, {"id": e.id, "name": e.name, "employee_code": e.employee_code or "",
                                            "branch": e.branch, "shift": shift_name,
                                            "employment_type": e.employment_type, "count": 0, "total": 0.0,
                                            "late": 0, "absent": 0})
        emp["count"] += 1
        emp["total"] = round(emp["total"] + fine, 2)
        emp[kind] += 1
        items.append({
            "id": a.id, "employee_id": e.id, "name": e.name, "employee_code": e.employee_code or "",
            "branch": e.branch, "shift": shift_name, "employment_type": e.employment_type,
            "week": wk["number"], "week_start": wstart.isoformat(),
            "date": a.attendance_date.isoformat(), "weekday": a.attendance_date.strftime("%A"),
            "status": a.status, "fine": fine, "reason": a.reason or "", "note": a.note or "",
            "updated_at": a.updated_at.isoformat() if a.updated_at else "",
        })
    return {
        "count": len(items), "total": round(total, 2),
        "late_total": round(late_total, 2), "absent_total": round(absent_total, 2),
        "avg": round(total / len(items), 2) if items else 0.0,
        "max": round(max((i["fine"] for i in items), default=0.0), 2),
        "by_branch": [by_branch[b] for b in BRANCHES],
        "by_week": sorted(by_week.values(), key=lambda x: x["start"], reverse=True),
        "by_month": sorted(by_month.values(), key=lambda x: x["month"], reverse=True),
        "by_employee": sorted(by_employee.values(), key=lambda x: x["total"], reverse=True),
        "items": items[:FINES_ITEMS_LIMIT],
        "truncated": len(items) > FINES_ITEMS_LIMIT,
    }


@app.get("/api/fines")
def list_fines(start: Optional[date] = None, end: Optional[date] = None, branch: Optional[str] = None,
               status: Optional[str] = None, q: Optional[str] = None,
               _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    """كل غرامات الفرعين (تأخير وغياب) بكل تفاصيلها: الموظف والفرع والشيفت والنوع واليوم والغرامة والسبب والملاحظة."""
    if start and end and end < start:
        raise HTTPException(400, "تاريخ النهاية يجب ألا يسبق تاريخ البداية")
    # استعلام واحد بس، وفلتر الفرع بيتطبّق في الذاكرة: نفس النتيجة نفس ترتيب الصفوف،
    # وعدّادات الفروع تفضل شايفة الفرعين وإنت واقف على فرع واحد
    all_rows = _fines_rows(db, start, end, None, status, q)
    rows = [r for r in all_rows if r[1].branch == branch] if branch in BRANCHES else all_rows
    payload = _fines_payload(rows, all_rows, WeekLookup(db))
    return {"start": start.isoformat() if start else "", "end": end.isoformat() if end else "",
            "branch": branch if branch in BRANCHES else "", "status": status if status in STATUSES else "", **payload}


@app.get("/api/fines/export.csv")
def export_fines(start: Optional[date] = None, end: Optional[date] = None, branch: Optional[str] = None,
                 status: Optional[str] = None, q: Optional[str] = None,
                 _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    rows = _fines_rows(db, start, end, branch, status, q)
    look = WeekLookup(db)
    return csv_response(
        ["Week", "Date", "Branch", "Employee Code", "Name", "Type", "Shift", "Status", "Fine (EGP)", "Reason", "Note"],
        [[week_number(week_start_of(a.attendance_date))[1], a.attendance_date.isoformat(), e.branch,
          e.employee_code or "", e.name, e.employment_type, look.get(e, a.attendance_date).shift, a.status,
          f"{float(a.fine or 0):.2f}", a.reason or "", a.note or ""] for a, e in rows],
        f"bankai_fines_{start or 'all'}_{end or 'all'}.csv")


# ---------------------------------------------------------------- schedule (جدول الشيفتات)
_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|ص|م)?", re.I)
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
WD_AR = {"Saturday": "السبت", "Sunday": "الأحد", "Monday": "الاثنين", "Tuesday": "الثلاثاء",
         "Wednesday": "الأربعاء", "Thursday": "الخميس", "Friday": "الجمعة"}


def parse_shift(name):
    """يقرأ وقت بداية ونهاية الشيفت من اسمه (مثلًا '12 AM - 8 AM' أو '16:00-00:00'). يرجّع (start_min, end_min) أو None."""
    text_ = str(name or "").translate(_AR_DIGITS)
    times = []
    for h, m, ap in _TIME_RE.findall(text_):
        h, m = int(h), int(m or 0)
        ap = ap.lower().replace(".", "")
        if m > 59:
            continue
        if ap in ("am", "ص"):
            if not 1 <= h <= 12:
                continue
            h = 0 if h == 12 else h
        elif ap in ("pm", "م"):
            if not 1 <= h <= 12:
                continue
            h = 12 if h == 12 else h + 12
        else:
            if h > 24:
                continue
            h %= 24
        times.append(h * 60 + m)
    if len(times) < 2:
        return None
    start, end = times[0], times[1]
    if end <= start:
        end += 1440
    return start, end


def hhmm(minutes):
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def shift_sort_key(name):
    p = parse_shift(name)
    return (0, p[0], name) if p else (1, 0, name)


def break_slots(start, end, dur):
    """أوقات بريك ثابتة (كل نص ساعة) في منتصف الشيفت تقريبًا (من 35% لحد 75% من مدته)، ومدة البريك dur دقيقة."""
    length = end - start
    t = ((start + int(length * 0.35) + 29) // 30) * 30
    limit = start + int(length * 0.75)
    slots = []
    while t + dur <= limit:
        slots.append(t % 1440)
        t += 30
    return slots or [(start + length // 2) % 1440]


class ShiftIn(BaseModel):
    branch: str
    name: str = Field(min_length=1, max_length=80)


class ShiftRenameIn(BaseModel):
    branch: str
    old: str
    new: str = Field(min_length=1, max_length=80)
    week: Optional[date] = None


class CopyWeekIn(BaseModel):
    from_week: date
    to_week: date


class ScheduleAddItem(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    employment_type: str = "FT"
    day_off: Optional[str] = None
    break_start: Optional[str] = None


class ScheduleAddIn(BaseModel):
    branch: str
    shift: str = Field(min_length=1, max_length=80)
    items: List[ScheduleAddItem]
    week: Optional[date] = None


class ScheduleApplyItem(BaseModel):
    id: int
    day_off: Optional[str] = None
    break_start: Optional[str] = None      # "" = مسح البريك، None = بدون تغيير
    break_min: Optional[int] = Field(default=None, ge=5, le=120)
    off_locked: Optional[bool] = None
    shift: Optional[str] = Field(default=None, max_length=80)


class ScheduleApplyIn(BaseModel):
    items: List[ScheduleApplyItem]
    week: Optional[date] = None


class ScheduleAutoIn(BaseModel):
    branch: str
    shift: Optional[str] = None
    include_locked: bool = False
    allowed_days: List[str] = Field(default_factory=lambda: list(WEEKDAYS))
    distribute_off: bool = True
    distribute_breaks: bool = True
    overwrite_breaks: bool = False
    break_min: int = Field(default=60, ge=5, le=120)
    week: Optional[date] = None


def _check_branch(branch):
    if branch not in BRANCHES:
        raise HTTPException(400, "الفرع يجب أن يكون QCD 1 أو QCD 2")


@app.get("/api/schedule")
def get_schedule(branch: str, week: Optional[date] = None, _: bool = Depends(require_auth),
                 db: Session = Depends(get_db)):
    _check_branch(branch)
    ws = week_param(week)
    pairs = week_pairs(db, ws, branch)
    names = {d.name for d in db.query(ShiftDef).filter(ShiftDef.branch == branch).all()} | {s.shift for _, s in pairs}
    return {"branch": branch, "week": week_info(ws), "days": list(WEEKDAYS),
            "shifts": sorted(names, key=shift_sort_key), "employees": [ov(e, s) for e, s in pairs]}


@app.post("/api/schedule/shifts")
def create_shift(data: ShiftIn, request: Request, role: str = Depends(require_admin),
                 db: Session = Depends(get_db)):
    _check_branch(data.branch)
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "اسم الشيفت مطلوب")
    if not db.query(ShiftDef).filter(ShiftDef.branch == data.branch, ShiftDef.name == name).first():
        db.add(ShiftDef(branch=data.branch, name=name))
        db.commit()
        publish_event("schedule", branch=data.branch, action="shift-create", by=who_role(role),
                      client=client_tag(request))
    return {"name": name}


@app.delete("/api/schedule/shifts")
def delete_shift(branch: str, name: str, request: Request, week: Optional[date] = None,
                 role: str = Depends(require_admin), db: Session = Depends(get_db)):
    _check_branch(branch)
    if any(s.shift == name for _, s in week_pairs(db, week_param(week), branch)):
        raise HTTPException(400, "الشيفت فيه موظفين في الأسبوع ده. انقلهم لشيفت آخر أو أوقفهم أولًا.")
    db.query(ShiftDef).filter(ShiftDef.branch == branch, ShiftDef.name == name).delete()
    db.commit()
    publish_event("schedule", branch=branch, action="shift-delete", by=who_role(role),
                  client=client_tag(request))
    return {"message": "تم حذف الشيفت"}


@app.post("/api/schedule/shifts/rename")
def rename_shift(data: ShiftRenameIn, request: Request, role: str = Depends(require_admin),
                 db: Session = Depends(get_db)):
    _check_branch(data.branch)
    new = data.new.strip()
    if not new:
        raise HTTPException(400, "اسم الشيفت مطلوب")
    if new == data.old:
        return {"name": new, "moved": 0}
    ws = week_param(data.week)
    moved = 0
    for e, sc in week_pairs(db, ws, data.branch, active_only=False):
        if sc.shift == data.old:
            set_sched(db, e, ws, shift=new)
            moved += 1
    db.flush()
    still_used = (db.query(WeekSchedule.id).join(Employee, Employee.id == WeekSchedule.employee_id)
                  .filter(Employee.branch == data.branch, WeekSchedule.shift == data.old).first())
    if not still_used:
        db.query(ShiftDef).filter(ShiftDef.branch == data.branch, ShiftDef.name == data.old).delete()
    if not db.query(ShiftDef).filter(ShiftDef.branch == data.branch, ShiftDef.name == new).first():
        db.add(ShiftDef(branch=data.branch, name=new))
    db.commit()
    publish_event("schedule", branch=data.branch, action="shift-rename", by=who_role(role),
                  client=client_tag(request))
    return {"name": new, "moved": moved}


@app.post("/api/schedule/add")
def schedule_add(data: ScheduleAddIn, request: Request, role: str = Depends(require_admin),
                 db: Session = Depends(get_db)):
    _check_branch(data.branch)
    shift = data.shift.strip()
    if not shift:
        raise HTTPException(400, "الشيفت مطلوب")
    if not data.items:
        raise HTTPException(400, "اكتب اسم واحد على الأقل")
    if len(data.items) > 500:
        raise HTTPException(400, "الحد الأقصى 500 اسم في المرة")
    ws = week_param(data.week)
    smap = ensure_week(db, ws)
    existing = {}
    for e in db.query(Employee).filter(Employee.branch == data.branch).all():
        key = e.name.strip().lower()
        if key not in existing or (e.active and not existing[key].active):
            existing[key] = e
    if not db.query(ShiftDef).filter(ShiftDef.branch == data.branch, ShiftDef.name == shift).first():
        db.add(ShiftDef(branch=data.branch, name=shift))
    created, out, notes = 0, [], []
    for it in data.items:
        name = " ".join(it.name.split())
        if not name:
            continue
        etype = it.employment_type if it.employment_type in EMP_TYPES else "FT"
        day = it.day_off if it.day_off in WEEKDAYS else None
        bs = it.break_start.strip() if it.break_start and HHMM_RE.match(it.break_start.strip()) else None
        e = existing.get(name.lower())
        if e is not None:
            cur = smap.get(e.id)
            if e.active and cur is not None and cur.shift == shift:
                notes.append({"name": e.name, "type": "same"})
            else:
                notes.append({"name": e.name, "type": "moved" if e.active else "reactivated",
                              "from": cur.shift if (e.active and cur is not None) else None})
                e.active = True
                vals = {"shift": shift}
                if day:
                    vals.update(day_off=day, off_locked=True)
                if bs:
                    vals["break_start"] = bs
                set_sched(db, e, ws, **vals)
        else:
            e = Employee(name=name, branch=data.branch, shift=shift, employment_type=etype,
                         day_off=day or "Friday", off_locked=bool(day), break_start=bs, break_min=60, active=True)
            db.add(e)
            db.flush()
            add_to_weeks(db, e, ws)
            existing[name.lower()] = e
            created += 1
            notes.append({"name": name, "type": "created"})
        out.append(e)
    db.commit()
    for e in out:
        db.refresh(e)
    smap = peek_week(db, ws)
    publish_event("schedule", branch=data.branch, action="add", count=created,
                  names=[e.name for e in out[:3]], by=who_role(role), client=client_tag(request))
    return {"created": created, "notes": notes, "employees": [ov(e, smap.get(e.id)) for e in out]}


@app.post("/api/schedule/apply")
def schedule_apply(data: ScheduleApplyIn, request: Request, role: str = Depends(require_admin),
                   db: Session = Depends(get_db)):
    if not data.items:
        raise HTTPException(400, "لا توجد تغييرات للحفظ")
    if len(data.items) > 2000:
        raise HTTPException(400, "عدد التعديلات كبير جدًا في طلب واحد")
    emps = {e.id: e for e in db.query(Employee).filter(Employee.id.in_([i.id for i in data.items])).all()}
    ws = week_param(data.week)
    updated, errors = 0, []
    for it in data.items:
        e = emps.get(it.id)
        if not e:
            errors.append({"id": it.id, "detail": "الموظف غير موجود"}); continue
        if it.day_off is not None and it.day_off not in WEEKDAYS:
            errors.append({"id": it.id, "name": e.name, "detail": "يوم الإجازة غير صحيح"}); continue
        bs = None if it.break_start is None else it.break_start.strip()
        if bs and not HHMM_RE.match(bs):
            errors.append({"id": it.id, "name": e.name, "detail": "وقت البريك غير صحيح"}); continue
        sh = None if it.shift is None else it.shift.strip()
        if sh is not None and not sh:
            errors.append({"id": it.id, "name": e.name, "detail": "الشيفت مطلوب"}); continue
        vals = {}
        if it.day_off is not None:
            vals["day_off"] = it.day_off
        if bs is not None:
            vals["break_start"] = bs or None
        if it.break_min is not None:
            vals["break_min"] = it.break_min
        if it.off_locked is not None:
            vals["off_locked"] = it.off_locked
        if sh is not None:
            vals["shift"] = sh
        if vals:
            set_sched(db, e, ws, **vals)
        if sh is not None:
            if not db.query(ShiftDef).filter(ShiftDef.branch == e.branch, ShiftDef.name == sh).first():
                db.add(ShiftDef(branch=e.branch, name=sh))
        updated += 1
    db.commit()
    if updated:
        publish_event("schedule", action="apply", count=updated,
                      names=[emps[i.id].name for i in data.items if i.id in emps][:3],
                      by=who_role(role), client=client_tag(request))
    return {"updated": updated, "errors": errors}


@app.post("/api/schedule/auto")
def schedule_auto(data: ScheduleAutoIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """يقترح توزيع إجازات (متوازن على أيام الأسبوع داخل كل شيفت) وبريكات متدرّجة. مابيحفظش حاجة — الواجهة بتعرض الاقتراح وانت تحفظ."""
    _check_branch(data.branch)
    allowed = [d for d in WEEKDAYS if d in data.allowed_days]
    if data.distribute_off and not allowed:
        raise HTTPException(400, "اختر يوم واحد على الأقل متاح للإجازة")
    ws = week_param(data.week)
    groups = {}
    for e, sc in sorted(week_pairs(db, ws, data.branch), key=lambda p: p[0].name):
        if data.shift and sc.shift != data.shift:
            continue
        v = SimpleNamespace(id=e.id, name=e.name, shift=sc.shift, day_off=sc.day_off, off_locked=bool(sc.off_locked),
                            break_start=sc.break_start, break_min=sc.break_min)
        groups.setdefault(sc.shift, []).append(v)
    changes, warnings = {}, []
    off_changed = break_changed = 0
    for shift in sorted(groups, key=shift_sort_key):
        group = groups[shift]
        new_off, new_brk = {}, {}
        if data.distribute_off:
            fixed = [e for e in group if e.off_locked and not data.include_locked]
            counts = {d: 0 for d in WEEKDAYS}
            for e in fixed:
                counts[e.day_off] = counts.get(e.day_off, 0) + 1
            fixed_ids = {e.id for e in fixed}
            for e in group:
                if e.id in fixed_ids:
                    continue
                best = min(allowed, key=lambda d: (counts[d], 0 if d == e.day_off else 1, WEEKDAYS.index(d)))
                new_off[e.id] = best
                counts[best] += 1
        if data.distribute_breaks:
            parsed = parse_shift(shift)
            if not parsed:
                warnings.append(f"ما قدرتش أقرأ وقت الشيفت «{shift}» — اكتبه بالشكل 8 AM - 4 PM عشان يتوزّع البريك.")
            else:
                slots = break_slots(parsed[0], parsed[1], data.break_min)
                counts = {s: 0 for s in slots}
                todo = []
                for e in group:
                    if e.break_start and not data.overwrite_breaks:
                        try:
                            h, m = e.break_start.split(":")
                            t = int(h) * 60 + int(m)
                            if t in counts:
                                counts[t] += 1
                        except ValueError:
                            pass
                    else:
                        todo.append(e)
                for e in todo:
                    best = min(slots, key=lambda s: (counts[s], slots.index(s)))
                    new_brk[e.id] = hhmm(best)
                    counts[best] += 1
        for e in group:
            ch = {}
            if e.id in new_off and new_off[e.id] != e.day_off:
                ch["day_off"] = new_off[e.id]; off_changed += 1
            if e.id in new_brk and (new_brk[e.id] != (e.break_start or "") or (e.break_min or 60) != data.break_min):
                ch["break_start"] = new_brk[e.id]; ch["break_min"] = data.break_min
                if new_brk[e.id] != (e.break_start or ""):
                    break_changed += 1
            if ch:
                changes[e.id] = dict(ch, id=e.id, name=e.name, shift=shift)
    return {"changes": list(changes.values()), "warnings": warnings, "off_changed": off_changed,
            "break_changed": break_changed, "employees": sum(len(g) for g in groups.values())}


@app.get("/api/schedule/export.csv")
def schedule_export(branch: Optional[str] = None, week: Optional[date] = None,
                    _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    ws = week_param(week)
    emps = sorted((SimpleNamespace(**ov(e, s)) for e, s in week_pairs(db, ws, branch)),
                  key=lambda e: (e.branch, shift_sort_key(e.shift), e.name))
    return csv_response(
        ["الفرع", "الشيفت", "الاسم", "الكود", "النوع", "يوم الإجازة", "Beginning البريك", "مدة البريك (دقيقة)"],
        [[e.branch, e.shift, e.name, e.employee_code or "", e.employment_type, WD_AR.get(e.day_off, e.day_off),
          e.break_start or "", (e.break_min or 60) if e.break_start else ""] for e in emps],
        f"bankai_schedule_week{week_number(ws)[1]}_{(branch or 'all').replace(' ', '')}.csv")


@app.get("/api/schedule/export.xlsx")
def schedule_export_xlsx(branch: Optional[str] = None, week: Optional[date] = None,
                         _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    wk = week_param(week)
    emps = [SimpleNamespace(**ov(e, s)) for e, s in week_pairs(db, wk, branch)]

    wb = Workbook()
    ws = wb.active
    ws.title = f"Week {week_number(wk)[1]}"

    # Styles
    header_font = Font(name="Cairo", bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill(start_color="2A0F57", end_color="2A0F57", fill_type="solid")
    branch_font = Font(name="Cairo", bold=True, size=12, color="2A0F57")
    normal_font = Font(name="Cairo", size=10)
    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin")
    )
    title_font = Font(name="Cairo", bold=True, size=14, color="2A0F57")
    section_font = Font(name="Cairo", bold=True, size=13, color="2A0F57")

    # Separate FT and PT
    ft_emps = [e for e in emps if e.employment_type == "FT"]
    pt_emps = [e for e in emps if e.employment_type == "PT"]

    def write_section(ws, emps_list, start_row, is_ft=True):
        # Title
        title = "Full Time Employees" if is_ft else "Part Time Employees"
        ws.merge_cells(start_row=start_row, start_column=1, end_row=start_row, end_column=8)
        cell = ws.cell(row=start_row, column=1, value=title)
        cell.font = title_font
        cell.alignment = Alignment(horizontal="right", vertical="center")
        ws.row_dimensions[start_row].height = 30

        # Headers
        headers = ["الفرع", "الشيفت", "الاسم", "الكود", "النوع", "يوم الإجازة", "Beginning البريك", "مدة البريك (دقيقة)"]
        header_row = start_row + 1
        for i, h in enumerate(headers, 1):
            cell = ws.cell(row=header_row, column=i, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="right", vertical="center", wrap_text=True)
            cell.border = thin_border
        ws.row_dimensions[header_row].height = 30

        # Data rows
        for r, e in enumerate(emps_list, header_row + 1):
            values = [
                e.branch,
                e.shift,
                e.name,
                e.employee_code or "",
                e.employment_type,
                WD_AR.get(e.day_off, e.day_off),
                e.break_start or "",
                e.break_min if e.break_start else ""
            ]
            for c, v in enumerate(values, 1):
                cell = ws.cell(row=r, column=c, value=v)
                cell.font = normal_font
                cell.alignment = Alignment(horizontal="right", vertical="center", wrap_text=True)
                cell.border = thin_border
            ws.row_dimensions[r].height = 25

        return start_row + 1 + len(emps_list) + 2

    # Write FT section
    ft_start = 1
    if ft_emps:
        ft_end = write_section(wb.active, ft_emps, ft_start, is_ft=True)
    else:
        ft_end = ft_start

    # Write PT section after FT
    pt_start = ft_end
    if pt_emps:
        write_section(wb.active, pt_emps, pt_start, is_ft=False)
    else:
        # Write empty PT section header
        cell = ws.cell(row=pt_start, column=1, value="Part Time Employees (no employees)")
        cell.font = Font(name="Cairo", italic=True, size=10, color="808080")
        ws.merge_cells(start_row=pt_start, start_column=1, end_row=pt_start, end_column=8)

    # Set column widths
    ws.column_dimensions["A"].width = 14  # branch
    ws.column_dimensions["B"].width = 20   # shift
    ws.column_dimensions["C"].width = 30   # name
    ws.column_dimensions["D"].width = 18   # code
    ws.column_dimensions["E"].width = 14   # type
    ws.column_dimensions["F"].width = 18   # day off
    ws.column_dimensions["G"].width = 22   # break start
    ws.column_dimensions["H"].width = 22   # break minutes

    # Apply Arabic number formatting for numeric columns
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        for cell in row:
            if cell.column >= 8 and isinstance(cell.value, (int, float)):
                cell.number_format = '0.00'

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    filename = f"bankai_schedule_week{week_number(wk)[1]}_{(branch or 'all').replace(' ', '')}.xlsx"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.post("/api/schedule/copy-week")
def copy_week(data: CopyWeekIn, request: Request, role: str = Depends(require_admin),
              db: Session = Depends(get_db)):
    """ينسخ جدول أسبوع (شيفت/إجازة/بريك) فوق أسبوع تاني. الحضور والغرامات ما بتتأثرش."""
    src_w, dst_w = week_start_of(data.from_week), week_start_of(data.to_week)
    if src_w == dst_w:
        raise HTTPException(400, "اختار أسبوعين مختلفين")
    src = ensure_week(db, src_w)
    dst = ensure_week(db, dst_w)
    n = 0
    for eid, r in src.items():
        d = dst.get(eid)
        if d is None:
            db.add(_copy_row(r, dst_w))
        else:
            for k in SCHED_FIELDS:
                setattr(d, k, getattr(r, k))
        n += 1
    db.commit()
    publish_event("schedule", action="copy-week", count=n, by=who_role(role), client=client_tag(request))
    return {"copied": n, "from": week_info(src_w), "to": week_info(dst_w)}


# ---------------------------------------------------------------- schedule import (Excel)
import base64
import difflib
import unicodedata


def _clean(v):
    if v is None:
        return ""
    if hasattr(v, "hour") and hasattr(v, "minute") and not hasattr(v, "year"):
        return f"{v.hour:02d}:{v.minute:02d}"
    s = unicodedata.normalize("NFKC", str(v)).replace("‏", "").replace("‎", "")
    return " ".join(s.replace("\xa0", " ").split())


def _name_key(name):
    s = _clean(name).casefold()
    return re.sub(r"[^\w\s]", "", s).strip()


def _token_key(name):
    return " ".join(sorted(_name_key(name).split()))


_HDR = lambda s: re.sub(r"[\s_\-\.]", "", _clean(s).casefold())
_NAME_HDR = {"fulltime": "FT", "ft": "FT", "فولتايم": "FT", "دوامكامل": "FT",
             "parttime": "PT", "pt": "PT", "بارتتايم": "PT", "دوامجزئي": "PT",
             "name": None, "employee": None, "employeename": None, "الاسم": None, "اسم": None, "الموظف": None, "المندوب": None}


def _col_kind(h):
    if "shift" in h or "شيفت" in h:
        return "shift"
    if "break" in h or "بريك" in h or "استراح" in h:
        return "break"
    if "dayoff" in h or "off" in h or "اجاز" in h or "إجاز" in h or "راحه" in h or "راحة" in h:
        return "day"
    if h in ("type", "نوع", "النوع", "employmenttype"):
        return "type"
    return None


def parse_break_time(raw, start=None, end=None):
    """'12:PM' / '1:AM' / '6 PM' / '18:30' -> 'HH:MM' أو None."""
    s = _clean(raw).upper().translate(_AR_DIGITS).replace(".", "")
    if not s:
        return None
    m = re.match(r"^(\d{1,2})(?::?(\d{2}))?\s*:?\s*(AM|PM|ص|م)?$", s)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if mi > 59:
        return None
    if ap in ("AM", "ص"):
        if not 1 <= h <= 12:
            return None
        h = 0 if h == 12 else h
    elif ap in ("PM", "م"):
        if not 1 <= h <= 12:
            return None
        h = 12 if h == 12 else h + 12
    elif h > 23:
        return None
    return f"{h:02d}:{mi:02d}"


import zipfile
import xml.etree.ElementTree as ET
from collections import namedtuple

_XNS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_XCell = namedtuple("XCell", "value")


class _XSheet:
    def __init__(self, title, cells):
        self.title, self.cells = title, cells
        self.max_row = max((r for r, _ in cells), default=0)
        self.max_column = max((c for _, c in cells), default=0)

    def cell(self, r, c):
        return _XCell(self.cells.get((r, c)))


def _col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _col_letter(n):
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def read_xlsx(data):
    """قارئ xlsx بالمكتبة القياسية فقط (من غير openpyxl) — يرجّع شيتات بقيمها."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        if sum(i.file_size for i in z.infolist()) > 60_000_000:
            raise ValueError("too big")
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_XNS + "si"):
                txt = []
                for ch in si:
                    if ch.tag == _XNS + "t":
                        txt.append(ch.text or "")
                    elif ch.tag == _XNS + "r":
                        txt.extend(t.text or "" for t in ch if t.tag == _XNS + "t")
                shared.append("".join(txt))
        sheets = []
        for sh in wb.iter(_XNS + "sheet"):
            target = rels[sh.get(_RNS + "id")].lstrip("/")
            path = target if target.startswith("xl/") else "xl/" + target
            cells = {}
            for c in ET.fromstring(z.read(path)).iter(_XNS + "c"):
                m = re.match(r"([A-Z]+)(\d+)$", c.get("r") or "")
                if not m:
                    continue
                t, v = c.get("t"), c.find(_XNS + "v")
                if t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(_XNS + "t"))
                elif v is None or v.text is None:
                    continue
                elif t == "s":
                    val = shared[int(v.text)]
                elif t in ("str", "e"):
                    val = v.text
                elif t == "b":
                    val = "TRUE" if v.text == "1" else "FALSE"
                else:
                    f = float(v.text)
                    val = int(f) if f == int(f) else f
                cells[(int(m.group(2)), _col_index(m.group(1)))] = val
            sheets.append(_XSheet(sh.get("name") or "Sheet", cells))
        return sheets
    except Exception:
        raise HTTPException(400, "الملف مش Excel (.xlsx) صالح. احفظه من Excel بصيغة .xlsx وحاول تاني.")


def parse_schedule_workbook(data):
    """يقرأ الشيت (عمودين/بلوكين: FULL TIME + PART TIME أو أي ترتيب) ويرجّع صفوف خام + ملاحظات."""
    sheets_ = read_xlsx(data)
    rows, notes, used = [], [], []
    for ws in sheets_:
        hdr_row, hdr = None, {}
        for r in range(1, min(ws.max_row, 15) + 1):
            found = {c: _HDR(ws.cell(r, c).value) for c in range(1, ws.max_column + 1)}
            marks = {c: h for c, h in found.items() if h in _NAME_HDR}
            if marks:
                hdr_row, hdr = r, found
                break
        if hdr_row is None:
            continue
        marks = [c for c, h in hdr.items() if h in _NAME_HDR]
        blocks = []
        for i, c in enumerate(marks):
            stop = marks[i + 1] if i + 1 < len(marks) else ws.max_column + 1
            cols = {}
            for cc in range(c + 1, stop):
                k = _col_kind(hdr.get(cc, ""))
                if k and k not in cols:
                    cols[k] = cc
            # المحتوى أصدق من العنوان: لو عنوان BREAK فوق أيام أو العكس، نصحّح حسب القيم الفعلية
            content = {}
            for cc in range(c + 1, stop):
                vals = [_clean(ws.cell(r, cc).value) for r in range(hdr_row + 1, ws.max_row + 1)]
                vals = [v for v in vals if v]
                if len(vals) < 2:
                    continue
                frac = lambda f: sum(1 for v in vals if f(v)) / len(vals)
                if frac(lambda v: norm_day(v) is not None) >= 0.6:
                    kind = "day"
                elif frac(lambda v: parse_shift(v) is not None and re.search(r"\d\s*[-–—]\s*\d|\s[-–—]\s", v)) >= 0.6:
                    kind = "shift"
                elif frac(lambda v: parse_break_time(v) is not None) >= 0.6:
                    kind = "break"
                else:
                    continue
                content.setdefault(kind, cc)
            if content and any(cols.get(k) != v for k, v in content.items()):
                label = {"shift": "SHIFT", "break": "BREAK", "day": "DAY OFF"}
                notes.append(f"عناوين الأعمدة في الشيت مش مطابقة للمحتوى ({ws.title}) — اتقرا حسب القيم الفعلية: "
                             + "، ".join(f"عمود {_col_letter(v)} = {label[k]}" for k, v in sorted(content.items(), key=lambda x: x[1])))
                cols = {**{k: v for k, v in cols.items() if k == "type"}, **content}
            blocks.append({"name": c, "type": _NAME_HDR[hdr[c]], "cols": cols})
        used.append(ws.title)
        for r in range(hdr_row + 1, ws.max_row + 1):
            for b in blocks:
                g = lambda k: _clean(ws.cell(r, b["cols"][k]).value) if k in b["cols"] else ""
                name = _clean(ws.cell(r, b["name"]).value)
                shift, brk, day, typ = g("shift"), g("break"), g("day"), g("type")
                if not (name or shift or brk or day):
                    continue
                etype = b["type"] or norm_type(typ) or "FT"
                rows.append({"key": f"{ws.title}!{r}:{b['name']}", "sheet": ws.title, "row": r, "type": etype,
                             "name": name, "shift": shift, "break": brk, "day": day,
                             "has_break_col": "break" in b["cols"], "has_day_col": "day" in b["cols"]})
    if not used:
        raise HTTPException(400, "مش لاقي عناوين مفهومة في الشيت. لازم يكون فيه أعمدة FULL TIME / SHIFT / BREAK / DAY OFF (أو PART TIME).")
    return rows, used, notes


def _similar(name, pool):
    """موظفين بأسماء قريبة (ترتيب مختلف/حرف ناقص) — بنقترحهم بس ومابنربطش تلقائي."""
    nk, tk = _name_key(name), _token_key(name)
    out = []
    ntoks = set(nk.split())
    for e in pool:
        ek, etk = _name_key(e.name), _token_key(e.name)
        score = max(difflib.SequenceMatcher(None, nk, ek).ratio(), difflib.SequenceMatcher(None, tk, etk).ratio())
        etoks = set(ek.split())
        small, big = (ntoks, etoks) if len(ntoks) <= len(etoks) else (etoks, ntoks)
        if len(small) >= 2 and small <= big:
            score = max(score, 0.9)
        if score >= 0.86:
            out.append((score, e))
    out.sort(key=lambda x: -x[0])
    return [(round(s, 2), e) for s, e in out[:3]]


def _cand(e, score=None):
    return {"id": e.id, "name": e.name, "branch": e.branch, "shift": e.shift, "active": bool(e.active),
            **({"score": score} if score is not None else {})}


def plan_schedule_import(db, branch, raw_rows, decisions=None, deactivate_missing=False, ws=None):
    decisions = decisions or {}
    smap_w = ensure_week(db, ws or week_param(None))
    # الموظفين بجدول الأسبوع المختار (الشيفت/الإجازة/البريك بتاعة الأسبوع ده)
    emps = [SimpleNamespace(**ov(e, smap_w.get(e.id))) for e in db.query(Employee).all()]
    in_branch = [e for e in emps if e.branch == branch]
    other = [e for e in emps if e.branch != branch]
    shifts_known = {}
    for e in in_branch:
        p = parse_shift(e.shift)
        if p and e.active:
            shifts_known.setdefault(p, e.shift)
    for d in db.query(ShiftDef).filter(ShiftDef.branch == branch).all():
        p = parse_shift(d.name)
        if p:
            shifts_known.setdefault(p, d.name)

    def find(pool, name, only_active=None):
        nk, tk = _name_key(name), _token_key(name)
        sel = [e for e in pool if only_active is None or bool(e.active) == only_active]
        ex = [e for e in sel if _name_key(e.name) == nk]
        return ex or [e for e in sel if _token_key(e.name) == tk]

    plan, seen, claimed = [], {}, {}
    for r in raw_rows:
        item = {"key": r["key"], "sheet": r["sheet"], "row": r["row"], "type": r["type"], "name": r["name"],
                "raw_shift": r["shift"], "raw_break": r["break"], "raw_day": r["day"],
                "shift": None, "shift_new": False, "break": None, "day_off": None,
                "status": "skip", "reason": "", "warnings": [], "changes": [], "match": None, "candidates": [],
                "action": None, "decision": None}
        plan.append(item)
        if not r["name"]:
            item["reason"] = "صف بدون اسم (فيه بيانات تانية) — اتخطّى"
            continue
        name = r["name"]
        nk = _token_key(name)
        if nk in seen:
            item["reason"] = f"الاسم متكرر في الشيت (سطر {seen[nk]}) — اتخطّى"
            continue
        seen[nk] = r["row"]
        if not r["shift"]:
            item["reason"] = "مفيش شيفت — اتخطّى"
            continue
        p = parse_shift(r["shift"])
        if p:
            label = shifts_known.get(p) or f"{hhmm(p[0])}-{hhmm(p[1] % 1440)}"
            item["shift"], item["shift_new"] = label, p not in shifts_known
        else:
            item["shift"], item["shift_new"] = r["shift"], True
            item["warnings"].append("مش قادر أقرا وقت الشيفت")
        if r["day"]:
            d = norm_day(r["day"])
            if d:
                item["day_off"] = d
            else:
                item["warnings"].append(f"يوم الإجازة «{r['day']}» غير مفهوم")
        elif r["has_day_col"]:
            item["warnings"].append("بدون يوم إجازة")
        if r["break"]:
            b = parse_break_time(r["break"])
            if not b:
                item["warnings"].append(f"البريك «{r['break']}» غير مفهوم — اتتجاهل")
            elif p:
                rel = (int(b[:2]) * 60 + int(b[3:]) - p[0]) % 1440
                if rel >= p[1] - p[0]:
                    item["warnings"].append(f"البريك {b} خارج وقت الشيفت — اتتجاهل")
                    b = None
            item["break"] = b
        # --- match
        same = find(in_branch, name, True) or find(in_branch, name, False)
        act = [e for e in same if e.active]
        target, reason = None, ""
        dec = decisions.get(r["key"])
        if len(act) > 1:
            reason = "ambiguous"; item["candidates"] = [_cand(e) for e in act]
        elif same:
            target = (act or same)[0]
        else:
            om = find(other, name)
            if om:
                reason = "other_branch"; item["candidates"] = [_cand(e) for e in om[:3]]
            else:
                sim = _similar(name, in_branch)
                if sim:
                    reason = "similar"; item["candidates"] = [_cand(e, s) for s, e in sim]
        if reason:
            default = "new" if reason == "similar" else "skip"
            item["status"], item["reason"], item["decision"] = "review", reason, dec or default
            if dec in ("skip", "new"):
                pass
            elif dec and dec.isdigit() and int(dec) in {c["id"] for c in item["candidates"]}:
                target = next(e for e in emps if e.id == int(dec))
            else:
                item["decision"] = default
            if item["decision"] == "skip":
                continue
            if target is None and item["decision"] != "new":
                continue
        if target is not None:
            if target.id in claimed:
                item["status"], item["reason"] = "skip", f"نفس الموظف اتربط بسطر {claimed[target.id]} — اتخطّى"
                continue
            claimed[target.id] = r["row"]
            item["match"] = _cand(target)
            ch = []
            def chg(f, old, new):
                if (old or "") != (new or ""):
                    ch.append({"field": f, "from": old or "", "to": new or ""})
            chg("branch", target.branch, branch)
            chg("shift", target.shift, item["shift"])
            chg("employment_type", target.employment_type, r["type"])
            if item["day_off"]:
                chg("day_off", target.day_off, item["day_off"])
                if not target.off_locked:
                    ch.append({"field": "off_locked", "from": "", "to": "مثبّت"})
            if item["break"]:
                chg("break_start", target.break_start, item["break"])
            if not target.active:
                ch.append({"field": "active", "from": "موقوف", "to": "نشط"})
            item["changes"] = ch
            item["action"] = "update"
            if item["status"] != "review":
                item["status"] = "update" if ch else "same"
            if not ch:
                item["action"] = "same"
        else:
            item["action"] = "create"
            if item["status"] != "review":
                item["status"] = "create"
    matched = {i["match"]["id"] for i in plan if i["match"] and i["action"] in ("update", "same")}
    missing = [_cand(e) for e in in_branch if e.active and e.id in smap_w and e.id not in matched]
    summary = {k: sum(1 for i in plan if i["status"] == k) for k in ("create", "update", "same", "review", "skip")}
    summary["warnings"] = sum(1 for i in plan if i["warnings"] and i["status"] != "skip")
    summary["missing"] = len(missing)
    shifts = {}
    for i in plan:
        if i["action"] in ("create", "update", "same") and i["shift"]:
            s = shifts.setdefault(i["shift"], {"name": i["shift"], "count": 0, "new": i["shift_new"]})
            s["count"] += 1
    return {"items": plan, "summary": summary, "missing": missing,
            "shifts": sorted(shifts.values(), key=lambda s: shift_sort_key(s["name"]))}


class ScheduleImportIn(BaseModel):
    week: Optional[date] = None
    branch: str
    filename: str = ""
    file_b64: str = Field(min_length=10, max_length=9_000_000)
    decisions: Dict[str, str] = Field(default_factory=dict)
    deactivate_missing: bool = False
    apply: bool = False


@app.post("/api/schedule/import")
def schedule_import(data: ScheduleImportIn, request: Request, role: str = Depends(require_admin),
                    db: Session = Depends(get_db)):
    _check_branch(data.branch)
    try:
        raw = base64.b64decode(data.file_b64, validate=False)
    except Exception:
        raise HTTPException(400, "الملف غير صالح")
    if len(raw) > 6_000_000:
        raise HTTPException(400, "الملف كبير جدًا")
    rows, sheets, notes = parse_schedule_workbook(raw)
    if len(rows) > 3000:
        raise HTTPException(400, "عدد الصفوف كبير جدًا")
    ws = week_param(data.week)
    plan = plan_schedule_import(db, data.branch, rows, data.decisions, data.deactivate_missing, ws)
    hint = None
    m = re.search(r"qcd\s*[-_]?\s*([12])", f"{data.filename} {' '.join(sheets)}", re.I)
    if m:
        hint = f"QCD {m.group(1)}"
    plan.update({"branch": data.branch, "week": week_info(ws), "sheets": sheets, "notes": notes, "branch_hint": hint,
                 "branch_mismatch": bool(hint and hint != data.branch)})
    if not data.apply:
        return plan
    # ---------- apply (كله في transaction واحدة)
    by_id = {e.id: e for e in db.query(Employee).all()}
    known_defs = {d.name for d in db.query(ShiftDef).filter(ShiftDef.branch == data.branch).all()}
    created = updated = 0
    expected = []
    for it in plan["items"]:
        if it["action"] not in ("create", "update", "same"):
            continue
        if it["shift"] not in known_defs:
            db.add(ShiftDef(branch=data.branch, name=it["shift"]))
            known_defs.add(it["shift"])
        if it["action"] == "create":
            e = Employee(name=" ".join(it["name"].split()), branch=data.branch, shift=it["shift"],
                         employment_type=it["type"], day_off=it["day_off"] or "Friday",
                         off_locked=bool(it["day_off"]), break_start=it["break"], break_min=60, active=True)
            db.add(e)
            db.flush()
            add_to_weeks(db, e, ws)
            created += 1
        else:
            e = by_id[it["match"]["id"]]
            if it["action"] == "update":
                e.branch, e.employment_type, e.active = data.branch, it["type"], True
                updated += 1
            vals = {"shift": it["shift"]}
            if it["action"] == "update":
                if it["day_off"]:
                    vals.update(day_off=it["day_off"], off_locked=True)
                if it["break"]:
                    vals["break_start"] = it["break"]
                    cur = peek_week(db, ws).get(e.id)
                    vals["break_min"] = (cur.break_min if cur and cur.break_min else None) or e.break_min or 60
            set_sched(db, e, ws, **vals)
        expected.append((it, e))
    deactivated = 0
    if data.deactivate_missing:
        # «مش في الشيت» = يطلع من جدول الأسبوع ده وما بعده (الأسابيع اللي قبله وسجلات الحضور ما بتتلمسش)
        for c in plan["missing"]:
            n = (db.query(WeekSchedule).filter(WeekSchedule.employee_id == c["id"], WeekSchedule.week_start >= ws)
                 .delete(synchronize_session=False))
            deactivated += 1 if n else 0
    db.flush()
    smap_after = peek_week(db, ws)
    bad = []
    for it, e in expected:
        sc = smap_after.get(e.id)
        ok = (sc is not None and sc.shift == it["shift"] and e.employment_type == it["type"]
              and e.branch == data.branch and bool(e.active)
              and (not it["day_off"] or sc.day_off == it["day_off"]) and (not it["break"] or sc.break_start == it["break"]))
        if not ok:
            bad.append(it["name"])
    if bad:
        db.rollback()
        raise HTTPException(500, "فشل التحقق بعد الاستيراد، ما اتحفظش أي حاجة: " + "، ".join(bad[:5]))
    db.commit()
    plan["applied"] = {"created": created, "updated": updated, "same": sum(1 for it, _ in expected if it["action"] == "same"),
                       "deactivated": deactivated, "verified": len(expected)}
    publish_event("employees", action="import-schedule", branch=data.branch,
                  count=created + updated, names=[], by=who_role(role), client=client_tag(request))
    return plan


# ---------------------------------------------------------------- backup
@app.get("/api/backup")
def backup(_: bool = Depends(require_admin)):
    if not DATABASE_URL.startswith("sqlite"):
        raise HTTPException(400, "النسخ الاحتياطي المباشر متاح لقاعدة SQLite فقط")
    path = DATABASE_URL.replace("sqlite:///", "", 1)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    src = sqlite3.connect(path)
    dst = sqlite3.connect(tmp.name)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    name = f"bankai_backup_{utcnow().strftime('%Y%m%d_%H%M')}.db"
    return FileResponse(tmp.name, filename=name, media_type="application/octet-stream",
                        background=BackgroundTask(os.remove, tmp.name))


# ---------------------------------------------------------------- danger zone (admin only)
@app.post("/api/admin/wipe-attendance")
def wipe_attendance(request: Request, role: str = Depends(require_admin), db: Session = Depends(get_db)):
    """مسح سجل الحضور بالكامل (الموظفين والإعدادات والجدول بيفضلوا زي ما هما)."""
    n = db.query(Attendance).delete(synchronize_session=False)
    db.commit()
    publish_event("attendance", date="", ids=[], names=[], count=n, wipe=True,
                  by=who_role(role), client=client_tag(request))
    return {"deleted": n, "message": f"تم مسح {n} تسجيلة حضور. السجل بقى فاضي."}
