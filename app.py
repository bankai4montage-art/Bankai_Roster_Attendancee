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
                        ForeignKey, UniqueConstraint, Index, func, case, or_, inspect, text,
                        bindparam, select)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from starlette.background import BackgroundTask

APP_NAME = "Rawahel — Roster & Attendance"
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bankai.db")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ChangeThisPassword123!")
# كلمة مرور المشرفين: مشاهدة كل حاجة + تعديل الحضور اليومي فقط. فاضية = الخاصية مقفولة.
SUPERVISOR_PASSWORD = os.getenv("SUPERVISOR_PASSWORD", "")
# مشرف لكل فرع (اختياري): "QCD 1=باسورد;QCD 2=باسورد".
# لما يتحدد، المشرف المسجّل بكلمة مرور فرع يقدر يوافق/يرفض طلبات حضور DAS
# التابعة **لفرعه فقط** — والمشرف العام (بلا فرع) يفضل مشاهدة من غير قرارات.
# التعريف الفعلي بعد norm_branch — هو baseline SUPERVISOR_BRANCH_PASSWORDS.
BRANCH_SUPERVISOR_PASSWORDS_RAW = os.getenv("SUPERVISOR_BRANCH_PASSWORDS", "")
BRANCH_SUPERVISOR_PASSWORDS = {}  # بيتعبّى بعد تعريف norm_branch
# المفتاح السري لتوقيع جلسات الدخول. لو مش متحدد بيتولد من كلمة المرور فتفضل الجلسات شغالة بعد إعادة التشغيل.
SECRET_KEY = (os.getenv("SECRET_KEY") or os.getenv("ADMIN_TOKEN")
              or hashlib.sha256(("bankai|" + ADMIN_PASSWORD).encode()).hexdigest())
TOKEN_TTL_SECONDS = 30 * 24 * 3600

BRANCHES = ("QCD 1", "QCD 2")
EMP_TYPES = ("FT", "PT")
WEEKDAYS = ("Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
STATUSES = ("Present", "Late", "Absent", "Excused")
NO_FINE_STATUSES = ("Present", "Excused")
# الغرامة الإدارية: نوع تالت مستقل عن الحضور. قيمتها في الواجهة "Fine"
# (تظهر زي Late/Absent في التصدير) بس مصدرها جدول fines مش جدول attendance.
FINE_STATUS = "Fine"
# الأنواع اللي ينفع نفلتر بيها صفحة الغرامات
FINE_KINDS = ("Late", "Absent", FINE_STATUS)
# أسماء الوسائط المسموح بها في جمل تحديث الحضور المجمّعة
_PARAM_KEYS = ("row_id", "status", "fine", "reason", "note", "recorded_by", "updated_at", "expected")
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
    recorded_by = Column(String(60), nullable=True, default="")  # مين كتب تسجيل الحضور (الأدمن/المشرف/DAS)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    employee = relationship("Employee")
    __table_args__ = (UniqueConstraint("employee_id", "attendance_date", name="uq_employee_attendance_day"),)


class Fine(Base):
    """غرامة إدارية مستقلة: بتتسجل على مندوب في أي تاريخ من غير أي يوم حضور.

    منفصلة عن جدول الحضور عن قصد: الغرامة الإدارية مش حضور، فمتحطش كحالة حضور
    عشان إحصائيات الحضور ونسبة الحضور ونسبة الدقة تفضل سليمة، ولما تتسجل
    غرامتين في نفس اليوم مفيش تعارض مع قيد (الموظف + يوم الحضور)."""
    __tablename__ = "fines"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    fine_date = Column(Date, nullable=False, index=True)
    amount = Column(Float, nullable=False, default=0)
    reason = Column(String(500), nullable=True, default="")
    note = Column(String(500), nullable=True, default="")
    created_by = Column(String(60), nullable=True, default="")   # مين سجّل الغرامة (الأدمن/المشرف)
    created_at = Column(DateTime, default=utcnow)
    employee = relationship("Employee")


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
    att_cols = {c["name"] for c in inspect(engine).get_columns("attendance")}
    fine_cols = {c["name"] for c in inspect(engine).get_columns("fines")}
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
        # مين سجّل/قرّر — للأدمن والمشرفين وDAS
        if "recorded_by" not in att_cols:
            conn.execute(text("ALTER TABLE attendance ADD COLUMN recorded_by VARCHAR(60)"))
        if "created_by" not in fine_cols:
            conn.execute(text("ALTER TABLE fines ADD COLUMN created_by VARCHAR(60)"))


ensure_columns()


def migrate_manual_fines():
    """ترحيل لمرة واحدة: جدول manual_fines من النسخة السابقة بيتدمج في fines.

    كان فيه شكل سابق من الغرامات اليدوية بيتخزّن في جدول اسمه manual_fines —
    بعد دمج الغرامة الإدارية في نظام الغرامات الأساسي، صفوفه القديمة بتتقلّ
    لـ fines بنفس قيمها (من غير ما نتلمس جدول الحضور نهائيًا)، ولو ملاحظتها
    فاضية واسم المسجّل موجود بنكتبه في الملاحظة عشان ما يضيعوش."""
    try:
        if not inspect(engine).has_table("manual_fines"):
            return
        with engine.begin() as conn:
            if (conn.execute(text("SELECT COUNT(*) FROM fines")).scalar() or 0) > 0:
                return
            conn.execute(text(
                "INSERT INTO fines (employee_id, fine_date, amount, reason, note, created_by, created_at) "
                "SELECT employee_id, fine_date, amount, reason, "
                "CASE WHEN note IS NULL OR note = '' AND created_by IS NOT NULL AND created_by <> '' "
                "     THEN 'سجّلها: ' || created_by ELSE note END, "
                "created_by, COALESCE(created_at, CURRENT_TIMESTAMP) "
                "FROM manual_fines"))
    except Exception:
        pass      # الترحيل مش شرط ناجح — لو فشل التطبيق يفضل شغّال بالجدول الجديد زي ما هو


migrate_manual_fines()


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
    """يرجع الدور لو التوكن سليم، وإلا None.

    الأدوار:_admin / supervisor (مشرف عام) / supervisor:فرع (كلمة مرور فرع) /
    supervisor:الاسم:فرع (حساب مشرف بالاسم). التوكنات القديمة بتبقى غير صالحة."""
    try:
        role, exp, sig = token.split(".", 2)
        if not (role == "admin" or role == "supervisor" or role.startswith("supervisor:")):
            return None
        if role.startswith("supervisor:"):
            parts = role.split(":")
            if len(parts) == 2:
                if parts[1] not in BRANCHES:
                    return None
            elif len(parts) == 3:
                if parts[2] not in BRANCHES or len(parts[1]) > 60 or not parts[1]:
                    return None
            else:
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


class BranchFinesIn(BaseModel):
    """غرامات ثابتة لفرع واحد: تأخير وغياب × فول تايم وبارت تايم."""
    late_ft: float = Field(default=0, ge=0, le=100000)
    late_pt: float = Field(default=0, ge=0, le=100000)
    absent_ft: float = Field(default=0, ge=0, le=100000)
    absent_pt: float = Field(default=0, ge=0, le=100000)


class SettingsIn(BaseModel):
    fine_late: float = Field(default=0, ge=0, le=100000)
    fine_absent: float = Field(default=0, ge=0, le=100000)
    # قاموس {فرع: غرامات} — الفرع لازم يكون من BRANCHES، والقيم لكل نوع تعاقد (FT/PT)
    branch_fines: Optional[Dict[str, BranchFinesIn]] = None


class FineIn(BaseModel):
    """غرامة إدارية جديدة: مندوب + تاريخ + قيمة. من غير أي حالة حضور."""
    employee_id: int
    fine_date: date
    amount: float = Field(default=0, ge=0, le=1000000)
    reason: str = Field(default="", max_length=500)
    note: str = Field(default="", max_length=500)


class FineEditIn(BaseModel):
    """تعديل غرامة إدارية: كل الحقول اختيارية — اللي مش مبعوت بيفضل زي ما هو."""
    fine_date: Optional[date] = None
    amount: Optional[float] = Field(default=None, ge=0, le=1000000)
    reason: Optional[str] = Field(default=None, max_length=500)
    note: Optional[str] = Field(default=None, max_length=500)


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
        "recorded_by": a.recorded_by or "",
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


def _parse_branch_supervisor_passwords():
    """تفسير SUPERVISOR_BRANCH_PASSWORDS بصيغة "QCD 1=باس;QCD 2=باس" (بنفصل بالفاصلة أو ;)."""
    out = {}
    for part in re.split(r"[;,]+", BRANCH_SUPERVISOR_PASSWORDS_RAW or ""):
        part = part.strip()
        if not part or "=" not in part:
            continue
        b, pw = part.split("=", 1)
        branch = norm_branch(b)
        pw = pw.strip()
        if branch and pw:
            out[branch] = pw
    return out


BRANCH_SUPERVISOR_PASSWORDS = _parse_branch_supervisor_passwords()


def _parse_supervisor_accounts():
    """حسابات المشرفين بالاسم: "الاسم:الباسورد:الفرع" مفصولة بـ ; أو ,.

    مثال: SUPERVISOR_ACCOUNTS=HAMED:pass1:QCD 2;Karim:pass2:QCD 2;Elsuid:pass3:QCD 1
    كل حساب بيدي توكن مرتبط بالاسم وبالفرع — المشرف يشوف ويقرّر على فرعه فقط،
    وكل حاجة يعملها (حضور/موافقة/غرامة) بتتسجل باسمه. الأسماء اللي فيها ":" أو "|" مش مسموحة."""
    out = {}
    for part in re.split(r"[;,]+", os.getenv("SUPERVISOR_ACCOUNTS", "")):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        if len(bits) != 3:
            continue
        name, pw, br = (b.strip() for b in bits)
        branch = norm_branch(br)
        if not (name and pw and branch) or ":" in name or "|" in name:
            continue
        out[pw] = {"name": name, "branch": branch}
    return out


BRANCH_SUPERVISOR_ACCOUNTS = _parse_supervisor_accounts()


def role_branch(role: str) -> Optional[str]:
    """فرع المشرف لو الدور مقيّد بفرع، وإلا None (الأدمن والمشرف العام لأي حاجة)."""
    if not role or not role.startswith("supervisor:"):
        return None
    parts = role.split(":")
    if len(parts) == 2:
        return parts[1] if parts[1] in BRANCHES else None
    if len(parts) == 3:
        return parts[2] if parts[2] in BRANCHES else None
    return None


def role_name(role: str) -> str:
    """اسم المشرف لو الدور جاي من حساب مشرف بالاسم، وإلا نص فاضي."""
    if role and role.startswith("supervisor:"):
        parts = role.split(":")
        if len(parts) == 3:
            return parts[1]
    return ""


def supervisor_scope(role: str) -> Optional[str]:
    """فرع المشرف الحالي لفلترة البيانات (None = مش مقيّد بفرع)."""
    return role_branch(role)


def _has_branch_supervisors() -> bool:
    """هل في حسابات مشرفين مرتبطة بفروع (حسابات بالاسم أو كلمات مرور الفرع)."""
    return bool(BRANCH_SUPERVISOR_ACCOUNTS or BRANCH_SUPERVISOR_PASSWORDS)


@app.get("/api/admin/supervisors")
def supervisors_debug(request: Request, role: str = Depends(require_admin)):
    """تشخيص حسابات المشرفين — للأدمن بس. بيرجّع الأسماء والفروع **بدون أي كلمات مرور**
    عشان تتأكد إن متغيرات البيئة وصلت واتفسّرت صح بعد النشر."""
    accounts = [{"name": acc["name"], "branch": acc["branch"]} for acc in BRANCH_SUPERVISOR_ACCOUNTS.values()]
    return {
        "accounts": accounts,
        "count": len(accounts),
        "branch_passwords": sorted(BRANCH_SUPERVISOR_PASSWORDS.keys()),
        "general_supervisor": bool(SUPERVISOR_PASSWORD),
        "das_integration": bool(DAS_SECRET),
        "message": "لو القايمة فاضية والباسوردات مش شغالة: متغير SUPERVISOR_ACCOUNTS مش متسجل على السيرفر أو النشر ماخدش قيمته." if not accounts else "",
    }



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
            "اربعاء": "Wednesday", "اربع": "Wednesday", "خميس": "Thursday", "جمعه": "Friday"}


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
    # الغرامات الثابتة لكل فرع: مفاتيح بصيغة "bf|الفرع|FT|late" في جدول settings.
    # القيمة الموجودة تعني إنها متسجّلة صراحةً؛ اللي مش موجودة بترجع برفض JSON
    # (مفيش مفتاح) والواجهة بتاخد مكانها الافتراضي العام fine_late / fine_absent.
    branch_fines: Dict[str, Dict[str, float]] = {}
    for key, value in stored.items():
        parts = key.split("|")
        if len(parts) != 4 or parts[0] != "bf":
            continue
        _, branch, emp, kind = parts
        if branch not in BRANCHES or emp not in EMP_TYPES or kind not in ("late", "absent"):
            continue
        try:
            branch_fines.setdefault(branch, {})[f"{kind}_{emp.lower()}"] = float(value)
        except ValueError:
            continue
    out["branch_fines"] = branch_fines
    return out


def _branch_fine_key(branch: str, emp: str, kind: str) -> str:
    return f"bf|{branch}|{emp}|{kind}"


def _apply_fixed_fines_last_week(db: Session) -> dict:
    """تطبيق الغرامات الثابتة على آخر أسبوع فيه تسجيلات حضور.

    كل تسجيل تأخير/غياب في الأسبوع ده بياخد الغرامة الثابتة لفرع ونوع تعاقد
    الموظف (أو الافتراضي العام لو الفرع مالوش قيمة محفوظة) — حتى لو فيه أرقام
    قديمة محطوطة، بتتبدل بالقيمة الجديدة. الغرامات الإدارية المستقلة (Fine)
    والغروب/الحاضر/بعذر مش بيمسّهم، والأسبوع الأقدم بيفضل زي ما هو.
    """
    s = get_settings(db)
    bf = s.get("branch_fines") or {}

    def fixed(branch: str, emp: str, kind: str) -> float:
        v = (bf.get(branch) or {}).get(f"{kind}_{emp.lower()}")
        if v is not None:
            return v
        return s.get("fine_late" if kind == "late" else "fine_absent") or 0.0

    last = db.query(func.max(Attendance.attendance_date)).scalar()
    if not last:
        return {"week_start": None, "week_end": None, "rows": 0, "changed": 0}
    ws = week_start_of(last)
    we = ws + timedelta(days=6)
    rows = (db.query(Attendance, Employee)
              .join(Employee, Attendance.employee_id == Employee.id)
              .filter(Attendance.status.in_(("Late", "Absent")),
                      Attendance.attendance_date >= ws,
                      Attendance.attendance_date <= we)
              .all())
    changed = 0
    for att, emp in rows:
        want = fixed(emp.branch, emp.employment_type, "late" if att.status == "Late" else "absent")
        if float(att.fine or 0) != float(want):
            att.fine = float(want)
            changed += 1
    if changed:
        db.commit()
    return {"week_start": ws.isoformat(), "week_end": we.isoformat(), "rows": len(rows), "changed": changed}


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
    """اسم/وصف حقيقي لصاحب العملية — بيظهر في القرارات والتسجيلات والغرامات."""
    if role == "admin":
        return "أدمن"
    name, branch = role_name(role), role_branch(role)
    if name and branch:
        return f"مشرف {name} — {branch}"
    if branch:
        return f"مشرف {branch}"
    return "مشرف"




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
    else:
        role = None
        # حسابات المشرفين بالاسم — توكن مرتبط بالاسم وبالفرع معًا
        for pw, acc in BRANCH_SUPERVISOR_ACCOUNTS.items():
            if secrets.compare_digest(data.password.encode(), pw.encode()):
                role = f"supervisor:{acc['name']}:{acc['branch']}"
                break
        if role is None:
            # مشرف مرتبط بفرع بدون اسم (الشكل القديم)
            for branch, pw in BRANCH_SUPERVISOR_PASSWORDS.items():
                if secrets.compare_digest(data.password.encode(), pw.encode()):
                    role = f"supervisor:{branch}"
                    break
        if role is None and SUPERVISOR_PASSWORD and secrets.compare_digest(data.password.encode(), SUPERVISOR_PASSWORD.encode()):
            role = "supervisor"
    if role is None:
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
    return {"app": APP_NAME, "authenticated": True, "role": role, "name": role_name(role),
            "branch": role_branch(role) or ""}


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
    if data.branch_fines is not None:
        for branch, fines in data.branch_fines.items():
            if branch not in BRANCHES:
                raise HTTPException(400, "الفرع يجب أن يكون QCD 1 أو QCD 2")
            for emp in EMP_TYPES:
                for kind in ("late", "absent"):
                    value = getattr(fines, f"{kind}_{emp.lower()}", 0)
                    bfkey = _branch_fine_key(branch, emp, kind)
                    row = db.query(Setting).filter(Setting.key == bfkey).first()
                    if row:
                        row.value = str(value)
                    else:
                        db.add(Setting(key=bfkey, value=str(value)))
    db.commit()
    publish_event("settings", by=who_role(role), client=client_tag(request))
    # الغرامات الثابتة بتتطبق فورًا على آخر أسبوع فيه تسجيلات: أي أرقام تانية
    # محطوطة في التأخير/الغياب بتتبدل بقيمة الغرامة الجديدة لكل فرع ونوع.
    applied = _apply_fixed_fines_last_week(db)
    if applied["changed"]:
        publish_event("attendance", date="", ids=[], names=[], count=applied["changed"],
                      by=who_role(role), client=client_tag(request))
    out = get_settings(db)
    out["applied"] = applied
    return out


# ---------------------------------------------------------------- employees
@app.get("/api/employees")
def list_employees(branch: Optional[str] = None, active_only: bool = False, week: Optional[date] = None,
                   role: str = Depends(require_auth), db: Session = Depends(get_db)):
    """موظفين الأسبوع المختار (الشيفت والإجازة والبريك بتاعة الأسبوع ده) — مشرف الفرع يشوف فرعه بس."""
    ws = week_param(week)
    smap = ensure_week(db, ws)
    q = db.query(Employee)
    branch = supervisor_scope(role) or branch
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
def export_employees(week: Optional[date] = None, role: str = Depends(require_auth), db: Session = Depends(get_db)):
    ws = week_param(week)
    smap = ensure_week(db, ws)
    scope = supervisor_scope(role)
    rows = [(e, smap[e.id]) for e in db.query(Employee).all() if e.id in smap]
    if scope:
        rows = [(e, s) for e, s in rows if e.branch == scope]
    rows.sort(key=lambda p: (p[0].branch, p[1].shift, p[0].name))
    return csv_response(
        ["name", "employee_code", "branch", "shift", "employment_type", "day_off", "phone", "active"],
        [[e.name, e.employee_code or "", e.branch, s.shift, e.employment_type, s.day_off,
          e.phone or "", "yes" if e.active else "no"] for e, s in rows],
        f"bankai_employees_week{week_number(ws)[1]}.csv")


# ---------------------------------------------------------------- roster & attendance
@app.get("/api/roster")
def roster(attendance_date: date = Query(...), branch: Optional[str] = None,
           role: str = Depends(require_auth), db: Session = Depends(get_db)):
    weekday = attendance_date.strftime("%A")
    ws = week_start_of(attendance_date)
    branch = supervisor_scope(role) or branch
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


def _save_items(db: Session, day: date, items: List[AttendanceItem], _retry: bool = False,
                actor: str = "", scope_branch: Optional[str] = None):
    """حفظ/تعديل/مسح تسجيلات يوم واحد.

    كل صف بيتكتب بأعمدته الأربعة كاملة (status/fine/reason/note) في جملة UPDATE
    واحدة، ومعاها شرط على updated_at اللي السيرفر قراه — فلو حد تاني عدّل نفس الصف
    في اللحظة دي، التعديل القديم بيترفض بتعارض واضح بدل ما يخلط قيم من مصدرين.
    الجمل بتتنفّذ مجمّعة (executemany) عشان الحفظ الجماعي يبقى جملة واحدة مش جملة لكل موظف.
    actor = مين كتب التسجيل (بيتسجّل في recorded_by) وscope_branch = فرع المشرف
    المرتبط بفرع — الموظفين بره النطاق بيرجعوا خطأ بدل ما يتسجّل عليهم.
    """
    weekday = day.strftime("%A")
    wanted: Dict[int, AttendanceItem] = {}
    for it in items:                       # آخر تسجيل لنفس الموظف في الطلب هو اللي يمشي
        wanted[it.employee_id] = it
    errors: List[dict] = []
    if scope_branch:
        # مشرف فرع: نفس اليوم بس لموظفي فرعه — الباقي خطأ صريح بدل ما يتسجّل غياب غلط
        emps_pre = {e.id: e for e in db.query(Employee).filter(Employee.id.in_(list(wanted.keys()))).all()}
        for eid, it in list(wanted.items()):
            e = emps_pre.get(eid)
            if e is not None and e.branch != scope_branch:
                wanted.pop(eid, None)
                errors.append({"employee_id": eid, "name": e.name,
                               "detail": f"الموظف بتاع {e.branch} مش في نطاقك — فرعك {scope_branch}"})
    ids = list(wanted.keys())
    emps = {e.id: e for e in db.query(Employee).filter(Employee.id.in_(ids)).all()}
    smap = ensure_week(db, week_start_of(day))
    # بنقرا المفاتيح والـstamps بس (مش كائنات ORM) عشان مفيش أي كائن قديم يتكتب بعد الكتابة
    rows = db.execute(select(Attendance.id, Attendance.employee_id, Attendance.updated_at).where(
        Attendance.attendance_date == day, Attendance.employee_id.in_(ids))).all()
    row_ids = {r.employee_id: r.id for r in rows}
    stamps = {r.employee_id: r.updated_at for r in rows}
    saved = cleared = 0
    touched, names = [], []
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
                      "reason": reason, "note": note, "recorded_by": actor,
                      "updated_at": utcnow(), "name": e.name}
            if current is None:            # سجل قديم من غير stamp: بنكتبه من غير شرط
                updates_no_stamp[eid] = values
            else:
                # الشرط على الـstamp اللي السيرفر قراه دلوقتي: لو حد عدّل الصف في
                # اللحظة دي، الجملة مش هتلاقي صف والسيرفر يقول تعارض بدل ما يخلط القيم
                values["expected"] = expected if expected is not None else current
                updates[eid] = values
        else:
            inserts[eid] = {"employee_id": eid, "attendance_date": day, "status": status,
                            "fine": fine, "reason": reason, "note": note, "recorded_by": actor,
                            "updated_at": utcnow()}
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
                               recorded_by=bindparam("recorded_by"),
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
        return _save_items(db, day, list(wanted.values()), _retry=True, actor=actor, scope_branch=scope_branch)

    # بنقرا التسجيلات المتأثرة مرة واحدة بعد الحفظ عشان نبني الرد
    records = {eid: None for eid in touched}
    if touched:
        for r in db.execute(
                select(Attendance.id, Attendance.employee_id, Attendance.attendance_date,
                       Attendance.status, Attendance.fine, Attendance.reason,
                       Attendance.note, Attendance.recorded_by, Attendance.updated_at).where(
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
    res = _save_items(db, data.attendance_date, [item], actor=who_role(role),
                      scope_branch=supervisor_scope(role))
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
    res = _save_items(db, data.attendance_date, data.items, actor=who_role(role),
                      scope_branch=supervisor_scope(role))
    if res["saved"] or res["cleared"]:
        publish_event("attendance", date=data.attendance_date.isoformat(), ids=res["ids"],
                      names=res["names"], count=res["saved"] + res["cleared"],
                      by=who_role(role), client=client_tag(request))
    return {"saved": res["saved"], "cleared": res["cleared"], "errors": res["errors"],
            "records": res["records"]}


# ---------------------------------------------------------------- dashboard
@app.get("/api/dashboard")
def dashboard(attendance_date: date = Query(...), role: str = Depends(require_auth), db: Session = Depends(get_db)):
    weekday = attendance_date.strftime("%A")
    ws = week_start_of(attendance_date)
    scope = supervisor_scope(role)
    pairs = week_pairs(db, ws, scope)
    smap = {e.id: s for e, s in pairs}
    active = [e for e, _ in pairs]
    emp_rows = {a.employee_id: a for a in db.query(Attendance).filter(Attendance.attendance_date == attendance_date).all()}
    att = {eid: a for eid, a in emp_rows.items() if eid in smap}

    def blank():
        return {"scheduled": 0, "present": 0, "late": 0, "absent": 0, "excused": 0, "unmarked": 0, "fines": 0.0}

    totals = blank()
    branches = {b: dict(blank(), shifts={}) for b in ([scope] if scope else BRANCHES)}
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

    # الاتجاه والمتابعة بيتحسبوا على الأسبوع ده بس (من الأحد للسبت) — وبنطاق الفرع لو مشرف فرع
    we = ws + timedelta(days=6)
    scoped = [Employee.branch == scope] if scope else []
    trend_rows = (db.query(Attendance.attendance_date, Attendance.status, func.count(Attendance.id))
                  .select_from(Attendance).join(Employee, Attendance.employee_id == Employee.id)
                  .filter(Employee.active.is_(True), *scoped, Attendance.attendance_date >= ws,
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
              .filter(Employee.active.is_(True), *scoped, Attendance.attendance_date >= ws,
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

    wf = db.query(func.coalesce(func.sum(Attendance.fine), 0)).select_from(Attendance).join(
        Employee, Attendance.employee_id == Employee.id).filter(
        *scoped, Attendance.attendance_date >= ws, Attendance.attendance_date <= we).scalar()
    # الغرامات الإدارية المستقلة في نفس الأسبوع (مش مرتبطة بيوم حضور)
    mf_q = db.query(func.coalesce(func.sum(Fine.amount), 0)).filter(
        Fine.fine_date >= ws, Fine.fine_date <= we)
    if scope:
        mf_q = mf_q.join(Employee, Fine.employee_id == Employee.id).filter(Employee.branch == scope)
    mf = mf_q.scalar()
    return {"date": attendance_date.isoformat(), "weekday": weekday, "week": week_info(ws),
            "week_fines": round(float(wf or 0) + float(mf or 0), 2),
            "week_attendance_fines": round(float(wf or 0), 2),
            "week_standalone_fines": round(float(mf or 0), 2),
            "active_total": len(active),
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


def _empty_counts():
    """صف موظف من غير أي تسجيلات حضور — بس لو عنده غرامات إدارية."""
    return {"total": 0, "present": 0, "late": 0, "absent": 0, "excused": 0, "fine_total": 0.0}


def _standalone_fines_query(db, start, end, branch=None, q=None):
    """استعلام غرامات إدارية مستقلة في فترة — نفس فلاتر تقرير الموظفين."""
    query = (db.query(Employee.id.label("id"), Employee.name.label("name"),
                      Employee.employee_code.label("code"), Employee.branch.label("branch"),
                      Employee.shift.label("shift"), Employee.employment_type.label("etype"),
                      Employee.active.label("active"),
                      func.count(Fine.id).label("scount"),
                      func.coalesce(func.sum(Fine.amount), 0).label("sfines"))
             .select_from(Fine).join(Employee, Fine.employee_id == Employee.id)
             .filter(Fine.fine_date >= start, Fine.fine_date <= end))
    if branch in BRANCHES:
        query = query.filter(Employee.branch == branch)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Employee.name.ilike(like), Employee.employee_code.ilike(like)))
    return query


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
                   role: str = Depends(require_auth), db: Session = Depends(get_db)):
    _check_range(start, end)
    branch = supervisor_scope(role) or branch
    by_branch = {}
    for r in _range_query(db, [Employee.branch.label("branch")] + _count_cols(), start, end, branch).group_by(Employee.branch).all():
        by_branch[r.branch] = _counts(r)
    # الغرامات الإدارية المستقلة في نفس الفترة: بتتحسب منفصلة عن الحضور
    # وبتتزود على إجمالي الغرامات من غير ما تأثر على عدّاد التسجيلات.
    srows = (db.query(Employee.branch.label("branch"),
                      func.coalesce(func.sum(Fine.amount), 0).label("sfines"))
             .select_from(Fine).join(Employee, Fine.employee_id == Employee.id)
             .filter(Fine.fine_date >= start, Fine.fine_date <= end)
             .group_by(Employee.branch).all())
    standalone_total = 0.0
    for r in srows:
        if branch in BRANCHES and r.branch != branch:
            continue
        sf = round(float(r.sfines or 0), 2)
        standalone_total = round(standalone_total + sf, 2)
        b = by_branch.setdefault(r.branch, _empty_counts())
        b["fine_total"] = round(b["fine_total"] + sf, 2)
    total = _empty_counts()
    for b in by_branch.values():
        for k in total:
            total[k] += b[k]
    total["fine_total"] = round(total["fine_total"], 2)
    by_day = []
    day_rows = (_range_query(db, [Attendance.attendance_date.label("d")] + _count_cols(), start, end, branch)
                .group_by(Attendance.attendance_date).order_by(Attendance.attendance_date).all())
    for r in day_rows:
        by_day.append(dict(_counts(r), date=r.d.isoformat()))
    return {"start": start.isoformat(), "end": end.isoformat(), **total,
            "standalone_total": standalone_total, "by_branch": by_branch, "by_day": by_day}


@app.get("/api/reports/employees")
def report_employees(start: date, end: date, branch: Optional[str] = None, q: Optional[str] = None,
                     role: str = Depends(require_auth), db: Session = Depends(get_db)):
    _check_range(start, end)
    branch = supervisor_scope(role) or branch
    cols = [Employee.id.label("id"), Employee.name.label("name"), Employee.employee_code.label("code"),
            Employee.branch.label("branch"), Employee.shift.label("shift"),
            Employee.employment_type.label("etype"), Employee.active.label("active")] + _count_cols()
    rows = (_range_query(db, cols, start, end, branch, q)
            .group_by(Employee.id, Employee.name, Employee.employee_code, Employee.branch,
                      Employee.shift, Employee.employment_type, Employee.active)
            .order_by(Employee.branch, Employee.name).all())
    # الغرامات الإدارية المستقلة في نفس الفترة: بتتحسب منفصلة وبتتزود على
    # fine_total لكل موظف، من غير ما تأثر على عدد التسجيلات أو نسبة الحضور.
    srows = (_standalone_fines_query(db, start, end, branch, q)
             .group_by(Employee.id, Employee.name, Employee.employee_code, Employee.branch,
                       Employee.shift, Employee.employment_type, Employee.active).all())
    smap = peek_week(db, week_start_of(end))
    merged = {}
    for r in rows:
        c = _counts(r)
        rate = round((c["present"] + c["late"]) * 100 / c["total"], 1) if c["total"] else 0
        sc = smap.get(r.id)
        merged[r.id] = dict(c, id=r.id, name=r.name, employee_code=r.code or "", branch=r.branch,
                            shift=sc.shift if sc else r.shift, employment_type=r.etype,
                            active=bool(r.active), rate=rate, standalone=0, standalone_total=0.0)
    for r in srows:
        rec = merged.get(r.id)
        if rec is None:
            # موظف عنده غرامات إدارية بس — من غير أي تسجيل حضور في الفترة
            sc = smap.get(r.id)
            rec = dict(_empty_counts(), id=r.id, name=r.name, employee_code=r.code or "",
                       branch=r.branch, shift=sc.shift if sc else r.shift, employment_type=r.etype,
                       active=bool(r.active), rate=0, standalone=0, standalone_total=0.0)
            merged[r.id] = rec
        rec["standalone"] = int(r.scount or 0)
        rec["standalone_total"] = round(float(r.sfines or 0), 2)
        rec["fine_total"] = round(rec["fine_total"] + rec["standalone_total"], 2)
    out = sorted(merged.values(), key=lambda x: (x["branch"], x["name"]))
    return {"start": start.isoformat(), "end": end.isoformat(), "employees": out}


@app.get("/api/reports/employee/{employee_id}")
def employee_history(employee_id: int, start: Optional[date] = None, end: Optional[date] = None,
                     role: str = Depends(require_auth), db: Session = Depends(get_db)):
    e = db.query(Employee).filter(Employee.id == employee_id).first()
    if not e:
        raise HTTPException(404, "الموظف غير موجود")
    scope = supervisor_scope(role)
    if scope and e.branch != scope:
        raise HTTPException(403, "بيانات الموظفين دي لفرع مش فرعك")
    end = end or utcnow().date()
    start = start or (end - timedelta(days=29))
    _check_range(start, end)
    rows = (db.query(Attendance).filter(Attendance.employee_id == employee_id,
                                        Attendance.attendance_date >= start, Attendance.attendance_date <= end)
            .order_by(Attendance.attendance_date.desc()).all())
    # الغرامات الإدارية المستقلة في نفس الفترة (مش مرتبطة بيوم حضور)
    fines = (db.query(Fine).filter(Fine.employee_id == employee_id,
                                    Fine.fine_date >= start, Fine.fine_date <= end)
             .order_by(Fine.fine_date.desc()).all())
    stats = {"total": len(rows), "present": 0, "late": 0, "absent": 0, "excused": 0,
             "standalone": len(fines), "fine_total": 0.0, "standalone_total": 0.0}
    records = []
    for a in rows:
        key = a.status.lower()
        if key in stats:
            stats[key] += 1
        stats["fine_total"] += a.fine or 0
        records.append(dict(attendance_dict(a), weekday=a.attendance_date.strftime("%A"), source="attendance"))
    for f in fines:
        stats["standalone_total"] += f.amount or 0
        stats["fine_total"] += f.amount or 0
        records.append({
            "id": f.id, "employee_id": f.employee_id,
            "attendance_date": f.fine_date.isoformat(), "status": FINE_STATUS,
            "fine": round(float(f.amount or 0), 2), "reason": f.reason or "", "note": f.note or "",
            "created_by": f.created_by or "",
            "updated_at": f.created_at.isoformat() if f.created_at else "",
            "weekday": f.fine_date.strftime("%A"), "source": "standalone",
        })
    records.sort(key=lambda r: r["attendance_date"], reverse=True)
    stats["fine_total"] = round(stats["fine_total"], 2)
    stats["standalone_total"] = round(stats["standalone_total"], 2)
    # قايمة الأسابيع اللي للموظف تسجيلات فيها (كل التاريخ، مش الفترة المفلترة)
    # عشان دروب مينيو السجل يعرض Week N لكل أسبوع اتسجل فيه حاجة.
    day_rows = {r[0] for r in db.query(Attendance.attendance_date)
                .filter(Attendance.employee_id == employee_id).distinct().all()}
    fine_days = {r[0] for r in db.query(Fine.fine_date)
                 .filter(Fine.employee_id == employee_id).distinct().all()}
    weeks = [{"start": ws.isoformat(), "end": (ws + timedelta(days=6)).isoformat(),
              "number": week_number(ws)[1], "year": week_number(ws)[0]}
             for ws in sorted({week_start_of(d) for d in (day_rows | fine_days)}, reverse=True)]
    return {"employee": ov(e, peek_week(db, week_start_of(end)).get(e.id)), "start": start.isoformat(),
            "end": end.isoformat(), "stats": stats, "records": records, "weeks": weeks}


@app.get("/api/reports/export.csv")
def export_csv(start: date, end: date, branch: Optional[str] = None,
               role: str = Depends(require_auth), db: Session = Depends(get_db)):
    _check_range(start, end)
    branch = supervisor_scope(role) or branch
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
                            role: str = Depends(require_auth), db: Session = Depends(get_db)):
    data = report_employees(start, end, role_branch(role) or branch, q, role, db)
    return csv_response(
        ["Branch", "Employee Code", "Name", "Type", "Shift", "Records", "Present", "Late", "Absent", "Excused",
         "Attendance Rate %"],
        [[r["branch"], r["employee_code"], r["name"], r["employment_type"], r["shift"], r["total"], r["present"],
          r["late"], r["absent"], r["excused"], r["rate"]] for r in data["employees"]],
        f"bankai_employees_summary_{start}_{end}.csv")


# ---------------------------------------------------------------- fines (الغرامات)
FINES_ITEMS_LIMIT = 2000  # سقف صفوف التفاصيل في الرد — الإجماليات بتتحسب على كل الصفوف دايمًا


class FineRow:
    """صف غرامة موحّد: بيغلّف سجل الحضور (تأخير/غياب) وسجل الغرامة الإدارية.

    كل الكود اللي بيحسب الغرامات بيقرأ الحقول دي بنفس الأسماء، فالغرامة
    الإدارية بتدخل في نفس التجميعات من غير تكرار في الكود.
    المصدر بيتحدد من الحقل `source`: "attendance" أو "standalone"."""

    __slots__ = ("id", "employee_id", "attendance_date", "status", "fine", "reason", "note",
                 "updated_at", "source", "by")

    def __init__(self, id, employee_id, day, status, fine, reason, note, updated_at, source, by=""):
        self.id = id
        self.employee_id = employee_id
        self.attendance_date = day
        self.status = status
        self.fine = fine
        self.reason = reason or ""
        self.note = note or ""
        self.updated_at = updated_at
        self.source = source
        self.by = by or ""

    @classmethod
    def from_attendance(cls, a):
        return cls(a.id, a.employee_id, a.attendance_date, a.status, float(a.fine or 0),
                   a.reason, a.note, a.updated_at, "attendance", a.recorded_by or "")

    @classmethod
    def from_fine(cls, f):
        return cls(f.id, f.employee_id, f.fine_date, FINE_STATUS, float(f.amount or 0),
                   f.reason, f.note, f.created_at, "standalone", f.created_by or "")


def _fine_kind(status: str) -> str:
    """نوع الغرامة: late / absent / standalone (غرامة إدارية مستقلة عن الحضور)."""
    if status == "Late":
        return "late"
    if status == "Absent":
        return "absent"
    return "standalone"


def _attendance_fines(db, start=None, end=None, branch=None, status=None, q=None):
    """غرامات التأخير والغياب المولّدة من تسجيلات الحضور."""
    if status == FINE_STATUS:
        return []  # فلتر «غرامة إدارية بس» مش بيشمل غرامات الحضور
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


def _manual_fines(db, start=None, end=None, branch=None, status=None, q=None):
    """الغرامات الإدارية المستقلة: مش مرتبطة بيوم حضور ولا بحالة حضور."""
    if status and status != FINE_STATUS:
        return []  # فلتر التأخير/الغياب مش بيشمل غرامات إدارية
    query = (db.query(Fine, Employee).join(Employee, Fine.employee_id == Employee.id)
             .filter(Fine.amount > 0))
    if start:
        query = query.filter(Fine.fine_date >= start)
    if end:
        query = query.filter(Fine.fine_date <= end)
    if branch in BRANCHES:
        query = query.filter(Employee.branch == branch)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Employee.name.ilike(like), Employee.employee_code.ilike(like)))
    return query.order_by(Fine.fine_date.desc(), Employee.name).all()


def _fines_rows(db, start=None, end=None, branch=None, status=None, q=None):
    """كل تسجيلات الغرامات (الغرامة أكبر من صفر) في الفرعين مع بيانات الموظف:
    تأخير وغياب من سجل الحضور + غرامات إدارية مستقلة، مرتبة من الأحدث للأقدم."""
    att = _attendance_fines(db, start, end, branch, status, q)
    man = _manual_fines(db, start, end, branch, status, q)
    rows = [(FineRow.from_attendance(a), e) for a, e in att]
    rows += [(FineRow.from_fine(f), e) for f, e in man]
    rows.sort(key=lambda r: (-r[0].attendance_date.toordinal(), r[1].name))
    return rows


def _fines_payload(rows, branch_rows=None, look=None):
    """تجميع الغرامات: إجماليات الفرعين + تفصيل شهري + إجمالي كل موظف + السجل الكامل بالتفاصيل.

    branch_rows: صفوف إجماليات الفرعين لوحدها. لما الفلتر يكون على فرع واحد بنمرّر
    هنا كل الصفوف بتاعة الفترة (من غير فلتر الفرع) عشان عدّادات مفتاح الفروع تفضل
    شايفة الفرعين، بينما الباقي (الشهور/الموظفين/السجل/الإجماليات) بيفضل مخصوص بالفلتر."""
    blank_branch = {"branch": "", "count": 0, "total": 0.0, "late": 0, "absent": 0, "standalone": 0,
                    "late_total": 0.0, "absent_total": 0.0, "standalone_total": 0.0}
    by_branch = {b: dict(blank_branch, branch=b) for b in BRANCHES}
    for a, e in (branch_rows if branch_rows is not None else rows):
        fine = round(float(a.fine or 0), 2)
        kind = _fine_kind(a.status)
        b = by_branch.setdefault(e.branch, dict(blank_branch, branch=e.branch))
        b["count"] += 1
        b["total"] = round(b["total"] + fine, 2)
        b[kind] += 1
        b[kind + "_total"] = round(b[kind + "_total"] + fine, 2)
    by_month, by_employee, by_week, items = {}, {}, {}, []
    total = late_total = absent_total = standalone_total = 0.0
    for a, e in rows:
        fine = round(float(a.fine or 0), 2)
        kind = _fine_kind(a.status)
        shift_name = look.get(e, a.attendance_date).shift if look else e.shift
        wstart = week_start_of(a.attendance_date)
        wk = by_week.setdefault(wstart, dict(week_info(wstart), count=0, total=0.0, late=0, absent=0,
                                             standalone=0, late_total=0.0, absent_total=0.0,
                                             standalone_total=0.0))
        wk["count"] += 1
        wk["total"] = round(wk["total"] + fine, 2)
        wk[kind] += 1
        wk[kind + "_total"] = round(wk[kind + "_total"] + fine, 2)
        total += fine
        if kind == "late":
            late_total += fine
        elif kind == "absent":
            absent_total += fine
        else:
            standalone_total += fine
        mkey = a.attendance_date.strftime("%Y-%m")
        m = by_month.setdefault(mkey, {"month": mkey, "count": 0, "total": 0.0, "late": 0, "absent": 0,
                                       "standalone": 0, "late_total": 0.0, "absent_total": 0.0,
                                       "standalone_total": 0.0})
        m["count"] += 1
        m["total"] = round(m["total"] + fine, 2)
        m[kind] += 1
        m[kind + "_total"] = round(m[kind + "_total"] + fine, 2)
        emp = by_employee.setdefault(e.id, {"id": e.id, "name": e.name, "employee_code": e.employee_code or "",
                                            "branch": e.branch, "shift": shift_name,
                                            "employment_type": e.employment_type, "count": 0, "total": 0.0,
                                            "late": 0, "absent": 0, "standalone": 0, "standalone_total": 0.0})
        emp["count"] += 1
        emp["total"] = round(emp["total"] + fine, 2)
        emp[kind] += 1
        if kind == "standalone":
            emp["standalone_total"] = round(emp["standalone_total"] + fine, 2)
        items.append({
            "id": a.id, "employee_id": e.id, "name": e.name, "employee_code": e.employee_code or "",
            "branch": e.branch, "shift": shift_name, "employment_type": e.employment_type,
            "week": wk["number"], "week_start": wstart.isoformat(),
            "date": a.attendance_date.isoformat(), "weekday": a.attendance_date.strftime("%A"),
            "status": a.status, "fine": fine, "reason": a.reason or "", "note": a.note or "",
            "by": a.by,
            "updated_at": a.updated_at.isoformat() if a.updated_at else "",
            "source": a.source,
        })
    return {
        "count": len(items), "total": round(total, 2),
        "late_total": round(late_total, 2), "absent_total": round(absent_total, 2),
        "standalone_total": round(standalone_total, 2),
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
               role: str = Depends(require_auth), db: Session = Depends(get_db)):
    """كل غرامات الفرعين (تأخير وغياب وغرامات إدارية) بكل تفاصيلها: الموظف والفرع والشيفت
    والنوع واليوم والغرامة والسبب والملاحظة ومين سجّلها."""
    branch = supervisor_scope(role) or branch
    if start and end and end < start:
        raise HTTPException(400, "تاريخ النهاية يجب ألا يسبق تاريخ البداية")
    # استعلام واحد بس، وفلتر الفرع بيتطبّق في الذاكرة: نفس النتيجة نفس ترتيب الصفوف،
    # وعدّادات الفروع تفضل شايفة الفرعين وإنت واقف على فرع واحد
    all_rows = _fines_rows(db, start, end, None, status, q)
    rows = [r for r in all_rows if r[1].branch == branch] if branch in BRANCHES else all_rows
    payload = _fines_payload(rows, all_rows, WeekLookup(db))
    return {"start": start.isoformat() if start else "", "end": end.isoformat() if end else "",
            "branch": branch if branch in BRANCHES else "", "status": status if status in FINE_KINDS else "", **payload}


@app.get("/api/fines/export.csv")
def export_fines(start: Optional[date] = None, end: Optional[date] = None, branch: Optional[str] = None,
                 status: Optional[str] = None, q: Optional[str] = None,
                 role: str = Depends(require_auth), db: Session = Depends(get_db)):
    branch = supervisor_scope(role) or branch
    rows = _fines_rows(db, start, end, branch, status, q)
    look = WeekLookup(db)
    return csv_response(
        ["Week", "Date", "Branch", "Employee Code", "Name", "Type", "Shift", "Status", "Fine (EGP)", "Reason", "Note"],
        [[week_number(week_start_of(a.attendance_date))[1], a.attendance_date.isoformat(), e.branch,
          e.employee_code or "", e.name, e.employment_type, look.get(e, a.attendance_date).shift, a.status,
          f"{float(a.fine or 0):.2f}", a.reason or "", a.note or ""] for a, e in rows],
        f"bankai_fines_{start or 'all'}_{end or 'all'}.csv")


# ---------------------------------------------------------------- الغرامات الإدارية المستقلة
def _fine_dict(f, e=None):
    """تفاصيل غرامة إدارية واحدة."""
    emp = e or f.employee
    ws = week_start_of(f.fine_date)
    return {
        "id": f.id, "employee_id": f.employee_id, "name": emp.name,
        "employee_code": emp.employee_code or "", "branch": emp.branch, "shift": emp.shift,
        "employment_type": emp.employment_type, "date": f.fine_date.isoformat(),
        "weekday": f.fine_date.strftime("%A"), "week_start": ws.isoformat(),
        "week": week_number(ws)[1], "status": FINE_STATUS,
        "fine": round(float(f.amount or 0), 2), "reason": f.reason or "", "note": f.note or "",
        "created_by": f.created_by or "",
        "created_at": f.created_at.isoformat() if f.created_at else "", "source": "standalone",
    }


@app.get("/api/fines/manual")
def list_manual_fines(employee_id: Optional[int] = None, start: Optional[date] = None,
                      end: Optional[date] = None, branch: Optional[str] = None,
                      role: str = Depends(require_auth), db: Session = Depends(get_db)):
    """الغرامات الإدارية المستقلة بس (من غير تأخير ولا غياب)."""
    branch = supervisor_scope(role) or branch
    if start and end and end < start:
        raise HTTPException(400, "تاريخ النهاية يجب ألا يسبق تاريخ البداية")
    query = (db.query(Fine, Employee).join(Employee, Fine.employee_id == Employee.id)
             .filter(Fine.amount > 0))
    if employee_id:
        query = query.filter(Fine.employee_id == employee_id)
    if start:
        query = query.filter(Fine.fine_date >= start)
    if end:
        query = query.filter(Fine.fine_date <= end)
    if branch in BRANCHES:
        query = query.filter(Employee.branch == branch)
    rows = query.order_by(Fine.fine_date.desc(), Employee.name).all()
    return {"count": len(rows), "total": round(sum(float(f.amount or 0) for f, _ in rows), 2),
            "items": [_fine_dict(f, e) for f, e in rows]}


@app.post("/api/fines/manual")
def create_manual_fine(data: FineIn, request: Request,
                       role: str = Depends(require_auth), db: Session = Depends(get_db)):
    """تسجيل غرامة إدارية على مندوب في أي تاريخ — من غير تسجيل أي حالة حضور.

    المشرف المرتبط بفرع يسجّل غرامات موظفي فرعه بس، وكل غرامة بتتسجل باسم الساجل."""
    e = db.query(Employee).filter(Employee.id == data.employee_id).first()
    if not e:
        raise HTTPException(404, "الموظف غير موجود")
    scope = supervisor_scope(role)
    if scope and e.branch != scope:
        raise HTTPException(403, f"الغرامات بتاعة {e.branch} مش في نطاقك — فرعك {scope}")
    amount = round(float(data.amount or 0), 2)
    if amount <= 0:
        raise HTTPException(400, "قيمة الغرامة لازم تكون أكبر من صفر")
    reason = (data.reason or "").strip()
    if not reason:
        raise HTTPException(400, "اكتب سبب الغرامة")
    f = Fine(employee_id=e.id, fine_date=data.fine_date, amount=amount,
             reason=reason, note=(data.note or "").strip(), created_by=who_role(role))
    db.add(f)
    db.commit()
    db.refresh(f)
    publish_event("fines", action="create", names=[e.name], by=who_role(role),
                  client=client_tag(request))
    return _fine_dict(f, e)


@app.put("/api/fines/manual/{fine_id}")
def update_manual_fine(fine_id: int, data: FineEditIn, request: Request,
                       role: str = Depends(require_admin), db: Session = Depends(get_db)):
    """تعديل غرامة إدارية: القيمة والتاريخ والسبب والملاحظة."""
    f = db.query(Fine).filter(Fine.id == fine_id).first()
    if not f:
        raise HTTPException(404, "الغرامة غير موجودة")
    e = db.query(Employee).filter(Employee.id == f.employee_id).first()
    if not e:
        raise HTTPException(404, "الموظف غير موجود")
    if data.fine_date is not None:
        f.fine_date = data.fine_date
    if data.amount is not None:
        amount = round(float(data.amount), 2)
        if amount <= 0:
            raise HTTPException(400, "قيمة الغرامة لازم تكون أكبر من صفر")
        f.amount = amount
    if data.reason is not None:
        f.reason = data.reason.strip()
    if data.note is not None:
        f.note = data.note.strip()
    if not (f.reason or "").strip():
        raise HTTPException(400, "اكتب سبب الغرامة")
    db.commit()
    db.refresh(f)
    publish_event("fines", action="update", names=[e.name], by=who_role(role),
                  client=client_tag(request))
    return _fine_dict(f, e)


@app.delete("/api/fines/manual/{fine_id}")
def delete_manual_fine(fine_id: int, request: Request, role: str = Depends(require_admin),
                       db: Session = Depends(get_db)):
    """مسح غرامة إدارية."""
    f = db.query(Fine).filter(Fine.id == fine_id).first()
    if not f:
        raise HTTPException(404, "الغرامة غير موجودة")
    e = db.query(Employee).filter(Employee.id == f.employee_id).first()
    name = e.name if e else ""
    db.delete(f)
    db.commit()
    publish_event("fines", action="delete", names=[name], by=who_role(role),
                  client=client_tag(request))
    return {"message": "تم مسح الغرامة الإدارية"}


# ---------------------------------------------------------------- طلبات حضور DAS
# المناديب بيسجّلوا حضور/انصراف من صفحة DAS، والطلب بيجي هنا Pending ومشرف الفرع
# (أو الأدمن) يوافق عليه أو يرفضه. الروستر هو مصدر الحقيقة: الموافقة بتسجّل حضور
# رسمي في جدول attendance بنفس قواعد النظام، والتقارير والحسابات ما بتتأثرش.
#
# الأمان بين النظامين: كل نداء من DAS لازم يوصل مع هيدر X-DAS-Secret بمفتاح
# سر سيرفري (DAS_SECRET). المفتاح عمرو ما بيروح لبراوزير، والموظف بيتعرّف من
# employee_code حصرًا (مش من الاسم لأن الاسم ممكن يتكرر أو يتغيّر)، والوقت
# المسجّل هو وقت السيرفر نفسه مش وقت جهاز المندوب.
DAS_SECRET = os.getenv("DAS_SECRET", "")
DAS_ACTIONS = ("check_in", "check_out")
DAS_STATUSES = ("Pending", "Approved", "Rejected")


class DasRequest(Base):
    __tablename__ = "das_requests"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    action = Column(String(20), nullable=False)                       # check_in | check_out
    attendance_date = Column(Date, nullable=False, index=True)        # يوم الشيفت المحسوب سيرفريًا
    status = Column(String(20), nullable=False, default="Pending", index=True)
    requested_at = Column(DateTime, nullable=False)                   # وقت استلام السيرفر (الحقيقة الموثّقة)
    courier_local_at = Column(String(40), nullable=True, default="")  # وقت جهاز المندوب — للعرض فقط، مش موثّق
    decided_by = Column(String(40), nullable=True, default="")
    decided_at = Column(DateTime, nullable=True)
    decision_note = Column(String(300), nullable=True, default="")
    checkin_id = Column(Integer, nullable=True, index=True)           # طلب الحضور اللي الانصراف بيتبنى بعده
    source = Column(String(10), nullable=False, default="DAS")        # DAS | QR
    employee = relationship("Employee")


# قيود يقينية (SQLite): مفيش أكتر من طلب Pending بنفس النوع لنفس الموظف في نفس يوم
# الشيفت، ومفيش انصرافين مفتوحين (Pending/Approved) على نفس طلب الحضور.
Index("uq_das_pending", DasRequest.employee_id, DasRequest.attendance_date, DasRequest.action,
      unique=True, sqlite_where=text("status = 'Pending'"))
Index("uq_das_open_checkout", DasRequest.checkin_id, unique=True,
      sqlite_where=text("action = 'check_out' AND status IN ('Pending','Approved')"))

# الجدول اتعرّف بعد create_all بتاع القاعدة — بنعمله إنشاء خاص بيه (idempotent)
Base.metadata.create_all(bind=engine, tables=[DasRequest.__table__])
# ترقية قاعدة موجودة: عمود مصدر الطلب (DAS / QR) من غير ما نلمس الصفوف القديمة.
try:
    with engine.begin() as _conn:
        _das_cols = {c["name"] for c in inspect(_conn).get_columns("das_requests")}
        if "source" not in _das_cols:
            _conn.execute(text(
                "ALTER TABLE das_requests ADD COLUMN source VARCHAR(10) NOT NULL DEFAULT 'DAS'"))
except Exception:
    pass


class DasRequestIn(BaseModel):
    action: str = Field(min_length=1, max_length=20)
    employee_code: str = Field(min_length=1, max_length=60)
    courier_name: str = Field(default="", max_length=120)
    courier_local_at: str = Field(default="", max_length=40)


class DasDecideIn(BaseModel):
    decision: str = Field(min_length=1, max_length=20)   # approve | reject
    note: str = Field(default="", max_length=300)


def require_das_secret(request: Request):
    """حماية واجهات الربط بين النظامين: سيرفر DAS بس اللي يعرف المفتاح."""
    if not DAS_SECRET:
        raise HTTPException(503, "ربط DAS غير مفعّل — حدّد المتغير DAS_SECRET على السيرفر")
    given = request.headers.get("x-das-secret", "")
    if not given or not secrets.compare_digest(given.encode(), DAS_SECRET.encode()):
        raise HTTPException(403, "مفتاح ربط DAS غير صحيح")


def cairo_dt(dt: datetime) -> datetime:
    """توقيت القاهرة (UTC+3) — نفس منطق today_local() بتاع النظام."""
    return dt + timedelta(hours=3)


def cairo_wall(dt: datetime) -> datetime:
    """وقت حائط القاهرة الحقيقي من UTC — بيحترم التوقيت الصيفي/الشتوي."""
    try:
        from zoneinfo import ZoneInfo
        local = dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Africa/Cairo"))
        return local.replace(tzinfo=None)
    except Exception:
        return cairo_dt(dt)


def das_shift_day(now: Optional[datetime] = None) -> date:
    """يوم الشيفت بتاريخ القاهرة (اليوم اللي تسجيل الحضور هيرتبط به في الروستر)."""
    return cairo_dt(now or utcnow()).date()


def das_request_dict(r: DasRequest, e: Optional[Employee] = None) -> dict:
    emp = e or r.employee
    return {
        "id": r.id, "action": r.action, "status": r.status,
        "employee_id": r.employee_id,
        "employee_name": emp.name if emp else "",
        "employee_code": (emp.employee_code or "") if emp else "",
        "branch": (emp.branch or "") if emp else "",
        "shift": (emp.shift or "") if emp else "",
        "attendance_date": r.attendance_date.isoformat(),
        "requested_at": r.requested_at.isoformat() if r.requested_at else "",
        "courier_local_at": r.courier_local_at or "",
        "decided_by": r.decided_by or "",
        "decided_at": r.decided_at.isoformat() if r.decided_at else "",
        "decision_note": r.decision_note or "",
        "checkin_id": r.checkin_id,
        "source": (r.source or "DAS"),
    }


def _das_owned_checkin(db: Session, e: Employee) -> Optional[DasRequest]:
    """آخر موافقة حضور مفتوحة (مالهاش انصراف Pending/Approved بعدها)."""
    checkins = (db.query(DasRequest)
                .filter(DasRequest.employee_id == e.id, DasRequest.action == "check_in",
                        DasRequest.status == "Approved")
                .order_by(DasRequest.attendance_date.desc(), DasRequest.id.desc())
                .limit(30).all())
    for c in checkins:
        open_co = (db.query(DasRequest)
                   .filter(DasRequest.checkin_id == c.id, DasRequest.action == "check_out",
                           DasRequest.status.in_(("Pending", "Approved"))).first())
        if open_co is None:
            return c
    return None


@app.post("/api/das/requests", dependencies=[Depends(require_das_secret)])
def create_das_request(data: DasRequestIn, db: Session = Depends(get_db)):
    """DAS بيبعت طلب حضور/انصراف جديد — بيتسجل Pending والمشرف يقرّر بعدها في داشبورد الروستر.

    الموظف بيتعرّف من employee_code حصرًا (مش من الاسم)، ووقت الطلب بيتسجل من السيرفر.
    يوم الشيفت بيتحدد من توقيت القاهرة: الحضور = اليوم، والانصراف بيرث يوم موافقته
    الحضور — فالشيفت الليلي بعد منتصف الليل بيفضل على نفس يوم الحضور من غير لفّ."""
    if data.action not in DAS_ACTIONS:
        raise HTTPException(400, "نوع الطلب غير صحيح")
    code = data.employee_code.strip()
    e = db.query(Employee).filter(Employee.employee_code == code).first()
    if not e:
        raise HTTPException(404, "الكود مش مربوط بموظف في الروستر — من شاشة ربط الروستر في لوحة إدارة DAS اربط الكود")
    if not e.active:
        raise HTTPException(404, "الموظف غير نشط في الروستر")

    now = utcnow()
    if data.action == "check_in":
        day = das_shift_day(now)
        dup = (db.query(DasRequest)
               .filter(DasRequest.employee_id == e.id, DasRequest.action == "check_in",
                       DasRequest.attendance_date == day, DasRequest.status == "Pending").first())
        if dup:
            raise HTTPException(409, "طلب حضور اليوم قيد الموافقة بالفعل")
        done = (db.query(DasRequest)
                .filter(DasRequest.employee_id == e.id, DasRequest.action == "check_in",
                        DasRequest.attendance_date == day, DasRequest.status == "Approved").first())
        if done:
            raise HTTPException(409, "حضورك اليوم مسجّل ومعتمد بالفعل — سجّل الانصراف")
        pending_out = db.query(DasRequest).filter(
            DasRequest.employee_id == e.id, DasRequest.action == "check_out",
            DasRequest.status == "Pending").first()
        if pending_out:
            raise HTTPException(409, "في طلب انصراف لسه مستني موافقة — استنى قرار المشرف الأول")
        row = DasRequest(employee_id=e.id, action="check_in", attendance_date=day,
                         status="Pending", requested_at=now,
                         courier_local_at=(data.courier_local_at or "")[:40])
    else:
        checkin = _das_owned_checkin(db, e)
        if checkin is None:
            raise HTTPException(409, "مفيش حضور معتمد ينفع تسجّل انصراف بعده — سجّل حضور الأول واستنى موافقة المشرف")
        open_co = (db.query(DasRequest)
                   .filter(DasRequest.checkin_id == checkin.id,
                           DasRequest.action == "check_out",
                           DasRequest.status.in_(("Pending", "Approved"))).first())
        if open_co:
            msg = "طلب الانصراف ده قيد الموافقة بالفعل" if open_co.status == "Pending" else "انصرافك مسجّل ومعتمد بالفعل"
            raise HTTPException(409, msg)
        row = DasRequest(employee_id=e.id, action="check_out", attendance_date=checkin.attendance_date,
                         status="Pending", requested_at=now, checkin_id=checkin.id,
                         courier_local_at=(data.courier_local_at or "")[:40])

    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "طلب متكرر — فيه طلب شبه ده مفتوح في نفس اللحظة")
    db.refresh(row)
    publish_event("das_requests", action="create",
                  names=[(data.courier_name or "").strip() or e.name], branch=e.branch, by="DAS", client="")
    return das_request_dict(row, e)


@app.get("/api/das/requests", dependencies=[Depends(require_das_secret)])
def list_das_requests(employee_code: Optional[str] = None, updated_since: Optional[str] = None,
                      limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    """مزامنة الحالة لجانب DAS: كل الطلبات لسه Pending أو اتغيّرت (إنشاء/قرار) من توقيت معيّن."""
    q = db.query(DasRequest)
    if employee_code:
        emp = db.query(Employee).filter(Employee.employee_code == employee_code.strip()).first()
        if emp is None:
            return {"requests": []}
        q = q.filter(DasRequest.employee_id == emp.id)
    since = _parse_stamp(updated_since or "")
    if since is not None:
        q = q.filter(or_(DasRequest.status == "Pending",
                         DasRequest.requested_at >= since,
                         DasRequest.decided_at >= since))
    rows = q.order_by(DasRequest.id.desc()).limit(limit).all()
    return {"requests": [das_request_dict(r) for r in rows]}


@app.get("/api/das/requests/inbox")
def das_requests_inbox(status: Optional[str] = None, limit: int = Query(60, ge=1, le=200),
                       role: str = Depends(require_auth), db: Session = Depends(get_db)):
    """القائمة اللي بتظهر للمشرف/الأدمن في داشبورد الروستر (Pending الأول).

    مشرف الفرع (supervisor:فرع) بيستلم طلبات فرعه فقط، والأدمن يشوف الفرعين."""
    branch = None
    if role == "supervisor" and not _has_branch_supervisors():
        branch = None            # مشرف عام بدون حسابات فرع: يشوف كل حاجة (ويقرّر أيضًا)
    else:
        branch = role_branch(role)
    q = db.query(DasRequest)
    if branch:
        q = q.join(Employee, DasRequest.employee_id == Employee.id).filter(Employee.branch == branch)
    if status in DAS_STATUSES:
        q = q.filter(DasRequest.status == status)
    rows = q.order_by(DasRequest.id.desc()).limit(limit).all()
    # Pending أعلى القايمة وبعده الأحدث أولًا
    order = {"Pending": 0, "Approved": 1, "Rejected": 2}
    rows = sorted(rows, key=lambda r: (order.get(r.status, 3), -r.id))
    return {"requests": [das_request_dict(r) for r in rows],
            "viewer": {"role": role, "branch": branch or "", "name": role_name(role),
                       "can_decide": _das_decision_allowed(role, branch or "QCD 1")
                       if role != "supervisor" else not _has_branch_supervisors()},
            "branches": list(BRANCHES)}


def _das_decision_allowed(role: str, req_branch: str) -> bool:
    """مين يقدّر يوافق/يرفض على طلب فرع معيّن.

    الأدمن لأي فرع. الحسابات المرتبطة بفرع (بالاسم أو بكلمة مرور الفرع) لفرعها
    فقط. المشرف العام (بلا فرع) للقرارات بس لما مفيش حسابات فرع متحددة إطلاقًا."""
    if role == "admin":
        return True
    branch = role_branch(role)
    if branch:
        return branch == req_branch
    if role == "supervisor":
        return not _has_branch_supervisors()
    return False


def record_das_attendance(db: Session, e: Employee, day: date, action: str, requested_at: datetime,
                          actor: str = "DAS", source: str = "DAS") -> Attendance:
    """تسجيل رسمي للحضور في جدول attendance عند الموافقة — بقيد (موظف + يوم) الأساسي.

    مفيش سجل لليوم: بنسجّل Present من غير غرامة (تصرف الموظف إنه وصل) والملاحظة
    بتوثّق وقت الطلب الفعلي (وقت السيرفر بتوقيت القاهرة). السجل موجود: بنضيف وقت
    الطلب في الملاحظة بس — حالة الحضور والغرامة اللي سجلها المشرف قبل كده مش
    بتلمس، عشان تسجيل الانصراف ما يبقاش بيحتّم تغيير غرامة المشرف؛ يقدر يعدّل أو
    يبرّر بعد كده طبيعي من صفحة الحضور."""
    clock = (cairo_wall(requested_at) if source == "QR" else cairo_dt(requested_at)).strftime("%H:%M")
    tag = ("حضور " if action == "check_in" else "انصراف ") + (source or "DAS")
    mark = f"{tag} {clock}"
    row = db.query(Attendance).filter(Attendance.employee_id == e.id,
                                      Attendance.attendance_date == day).first()
    if row is None:
        row = Attendance(employee_id=e.id, attendance_date=day, status="Present", fine=0,
                         reason="", note=mark, recorded_by=actor, updated_at=utcnow())
        db.add(row)
        db.flush()
        return row
    row.recorded_by = actor
    row.note = ((row.note or "") + ("\n" if row.note else "") + mark)[:500]
    if not row.status:
        row.status = "Present"
        row.fine = 0
        row.updated_at = utcnow()
    return row


@app.post("/api/das/requests/{req_id}/decide")
def decide_das_request(req_id: int, data: DasDecideIn, request: Request,
                       role: str = Depends(require_auth), db: Session = Depends(get_db)):
    """الموافقة أو الرفض على طلب حضور/انصراف جاي من DAS.

    الموافقة بتسجّل الحضور رسميًا في جدول attendance (وقت الطلب الموثّق بيتكتب في
    الملاحظة مش وقت الموافقة)، والرفض بيتسجل بملاحظة اختيارية. القرار بيتسجل باسم
    مين قرّر ووقتها، وبيمشي حدث مباشر يعيد رندر القايمة في كل التابات المتصلة."""
    if data.decision not in ("approve", "reject"):
        raise HTTPException(400, "نوع القرار غير صحيح")
    row = db.query(DasRequest).filter(DasRequest.id == req_id, DasRequest.status == "Pending").first()
    if row is None:
        raise HTTPException(409, "الطلب ده اتقرر عليه من قبل — حدّث القائمة")
    e = db.query(Employee).filter(Employee.id == row.employee_id).first()
    if e is None:
        raise HTTPException(404, "الموظف غير موجود في الروستر")
    if not _das_decision_allowed(role, e.branch):
        raise HTTPException(403, f"القرار على طلبات {e.branch} متاح لمشرف الفرع بس")

    if data.decision == "reject":
        row.status = "Rejected"
        row.decision_note = (data.note or "").strip()[:300]
    else:
        if not e.active:
            raise HTTPException(400, "الموظف غير نشط — فعّل الموظف قبل الموافقة")
        source = (row.source or "DAS")
        record_das_attendance(db, e, row.attendance_date, row.action, row.requested_at,
                              actor=who_role(role), source=source)
        if row.action == "check_out" and source == "QR":
            save_attendance_checkout(db, e, row.attendance_date, row.requested_at)
        row.status = "Approved"
        row.decision_note = (data.note or "").strip()[:300]
    row.decided_by = who_role(role)
    row.decided_at = utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "الطلب اتقرر عليه من مستخدم تاني — حدّث القائمة")
    db.refresh(row)
    publish_event("das_requests", action=("approve" if row.status == "Approved" else "reject"),
                  names=[e.name], branch=e.branch, by=who_role(role), client=client_tag(request))
    return das_request_dict(row, e)


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


@app.get("/api/schedule/weeks")
def schedule_weeks(_: bool = Depends(require_auth), db: Session = Depends(get_db)):
    """كل الأسابيع اللي ليها جدول + أسبوع النهارده — اللي المستخدم بيختار منهم
    الأسبوع اللي هيتطبّق عليه الشيت وقت الاستيراد."""
    rows = db.execute(select(WeekSchedule.week_start, func.count(WeekSchedule.id))
                      .group_by(WeekSchedule.week_start)
                      .order_by(WeekSchedule.week_start)).all()
    weeks = []
    for ws, cnt in rows:
        info = week_info(ws)
        info["employees"] = int(cnt or 0)
        weeks.append(info)
    return {"weeks": weeks, "today": week_info(week_param(None))}


@app.get("/api/schedule")
def get_schedule(branch: str, week: Optional[date] = None, role: str = Depends(require_auth),
                 db: Session = Depends(get_db)):
    branch = supervisor_scope(role) or (branch if branch in BRANCHES else BRANCHES[0])
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
                    role: str = Depends(require_auth), db: Session = Depends(get_db)):
    ws = week_param(week)
    emps = sorted((SimpleNamespace(**ov(e, s)) for e, s in week_pairs(db, ws, supervisor_scope(role) or branch)),
                  key=lambda e: (e.branch, shift_sort_key(e.shift), e.name))
    return csv_response(
        ["الفرع", "الشيفت", "الاسم", "الكود", "النوع", "يوم الإجازة", "Beginning البريك", "مدة البريك (دقيقة)"],
        [[e.branch, e.shift, e.name, e.employee_code or "", e.employment_type, WD_AR.get(e.day_off, e.day_off),
          e.break_start or "", (e.break_min or 60) if e.break_start else ""] for e in emps],
        f"bankai_schedule_week{week_number(ws)[1]}_{(branch or 'all').replace(' ', '')}.csv")


@app.get("/api/schedule/export.xlsx")
def schedule_export_xlsx(branch: Optional[str] = None, week: Optional[date] = None,
                         role: str = Depends(require_auth), db: Session = Depends(get_db)):
    wk = week_param(week)
    emps = [SimpleNamespace(**ov(e, s)) for e, s in week_pairs(db, wk, supervisor_scope(role) or branch)]

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
        # أعمدة الشمال لسه ما استُخدمتش: البلوك الأول ياخد كل اللي قبل اسمه، وأي بلوك بعده
        # ياخد بس اللي بعد آخر عمود بلوك قبله (عشان ما يسرقش أعمدة بلوك السابق).
        left_start = 1
        for i, c in enumerate(marks):
            stop = marks[i + 1] if i + 1 < len(marks) else ws.max_column + 1
            cols = {}
            for cc in range(c + 1, stop):
                k = _col_kind(hdr.get(cc, ""))
                if k and k not in cols:
                    cols[k] = cc
            # الشيفت/الإجازة/البريك ممكن يكونون قبل عمود الاسم (شيتات برّه أو تصدير الجدول نفسه
            # العملة فيه: الفرع، الشيفت، الاسم) — بنقراهم كمان عن طريق عنوان العمود.
            for cc in range(left_start, c):
                k = _col_kind(hdr.get(cc, ""))
                if k and k not in cols:
                    cols[k] = cc
            # المحتوى أصدق من العنوان: لو عنوان BREAK فوق أيام أو العكس، نصحّح حسب القيم الفعلية.
            # في أعمدة الشمال (قبل الاسم) ما نصحّحش بريك من غير عنوان — قيم البريك شكلها
            # زي أعمدة رقمية/أكواد كتير وده ممكن يلبس عمود غلط في بلوك غلط.
            content = {}
            for cc in range(left_start, stop):
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
                    if cc < c:
                        continue        # شمال الاسم: البريك بيتقرا من عنوانه بس
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
            left_start = stop      # الأعمدة دي بقت حط كلها منطقة البلوك ده
        used.append(ws.title)
        for r in range(hdr_row + 1, ws.max_row + 1):
            for b in blocks:
                g = lambda k: _clean(ws.cell(r, b["cols"][k]).value) if k in b["cols"] else ""
                name = _clean(ws.cell(r, b["name"]).value)
                if _HDR(name) in _NAME_HDR:    # صف بيردّ عناوين بلوك تاني (قسم PART TIME في التصدير)
                    continue
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
    was_new_week = not peek_week(db, ws)   # الأسبوع ده لسه مفتوحش قبل كده؟
    plan = plan_schedule_import(db, data.branch, rows, data.decisions, data.deactivate_missing, ws)
    hint = None
    m = re.search(r"qcd\s*[-_]?\s*([12])", f"{data.filename} {' '.join(sheets)}", re.I)
    if m:
        hint = f"QCD {m.group(1)}"
    plan.update({"branch": data.branch, "week": week_info(ws), "sheets": sheets, "notes": notes, "branch_hint": hint,
                 "branch_mismatch": bool(hint and hint != data.branch), "week_created": was_new_week})
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

# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════
# 📷 حضور بالـ QR — كود متغيّر على شاشة المشرف داخل الروستر، بدون GPS
#    الـQR بيتغيّر كل 30 ثانية، والتوكن موقّع ومربوط بالفرع. تسجيل الحضور
#    بيحسب التأخير من شيفت الأسبوع: أقل من 5 دقائق = حاضر، وبعدها Late
#    + الغرامة الثابتة مرة واحدة. تصحيح Absent بيتسجل في attendance_audit.
# ═══════════════════════════════════════════════════════════════════════
QR_TOKEN_SECONDS = 30
QR_TOKEN_GRACE_SECONDS = 10
QR_LATE_GRACE_MIN = int(os.getenv("QR_LATE_GRACE_MIN", "5"))


class AttendanceAudit(Base):
    """أثر تدقيق لتسجيلات الحضور اللي بتتغيّر من QR (وبالذات تصحيح Absent)."""
    __tablename__ = "attendance_audit"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    attendance_date = Column(Date, nullable=False, index=True)
    old_status = Column(String(20), nullable=True, default="")
    old_fine = Column(Float, nullable=False, default=0)
    new_status = Column(String(20), nullable=False, default="")
    new_fine = Column(Float, nullable=False, default=0)
    source = Column(String(10), nullable=False, default="QR")
    note = Column(String(300), nullable=True, default="")
    actor = Column(String(60), nullable=True, default="QR")
    created_at = Column(DateTime, default=utcnow, nullable=False)


Base.metadata.create_all(bind=engine, tables=[AttendanceAudit.__table__])

# جدول الانصرافات (بيتكتب عند موافقة المشرف فقط).
with engine.begin() as _conn:
    _conn.execute(text("""
        CREATE TABLE IF NOT EXISTS attendance_checkouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            checkout_date TEXT NOT NULL,
            checked_out_at TEXT NOT NULL,
            method TEXT DEFAULT 'QR',
            note TEXT DEFAULT ''
        )
    """))
    _conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_checkout_employee_day "
        "ON attendance_checkouts(employee_id, checkout_date)"
    ))


def qr_now_local() -> datetime:
    """وقت حائط القاهرة — نفس الساعة اللي المشرف شايفها على شاشة الروستر."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Africa/Cairo")).replace(tzinfo=None)


def _qr_mac(branch: str, bucket: int) -> str:
    return hmac.new(SECRET_KEY.encode(), f"attendance-qr|{branch}|{bucket}".encode(),
                    hashlib.sha256).hexdigest()[:20]


def qr_token(branch: str = "QCD 2", ts: float | None = None) -> str:
    """توكن موقّع: v1|الفرع|نافذة-30-ثانية|HMAC."""
    branch = norm_branch(branch) or "QCD 2"
    bucket = int((ts if ts is not None else time.time()) // QR_TOKEN_SECONDS)
    return f"v1|{branch}|{bucket}|{_qr_mac(branch, bucket)}"


def qr_token_branch(token: str) -> Optional[str]:
    """يرجّع الفرع لو التوكن سليم ولسه في نافذته (مع سماح 10 ثواني)."""
    try:
        version, branch, raw_bucket, mac = str(token).split("|")
        bucket = int(raw_bucket)
    except (ValueError, AttributeError):
        return None
    if version != "v1" or branch not in BRANCHES:
        return None
    now = time.time()
    now_bucket = int(now // QR_TOKEN_SECONDS)
    if bucket == now_bucket - 1:
        if (now - now_bucket * QR_TOKEN_SECONDS) > QR_TOKEN_GRACE_SECONDS:
            return None
    elif bucket != now_bucket:
        return None
    if not hmac.compare_digest(mac, _qr_mac(branch, bucket)):
        return None
    return branch


def qr_live_payload(branch: str) -> dict:
    branch = norm_branch(branch) or "QCD 2"
    return {
        "token": qr_token(branch),
        "branch": branch,
        "expires_in": max(1, QR_TOKEN_SECONDS - int(time.time()) % QR_TOKEN_SECONDS),
        "interval": QR_TOKEN_SECONDS,
        "grace_min": QR_LATE_GRACE_MIN,
        "late_after_min": QR_LATE_GRACE_MIN,
    }


def _qr_employee(db: Session, code: str) -> Optional[Employee]:
    code = (code or "").strip()
    if not code:
        return None
    return (db.query(Employee)
            .filter(func.upper(Employee.employee_code) == code.upper())
            .first())


def qr_shift_context(db: Session, e: Employee, now_local: datetime):
    """يوم الشيفت + شيفت الأسبوع + دقايق التأخير، مع معالجة الشيفت الليلي.

    الشيفت الليلي اللي بيعدّي منتصف الليل: التسجيل بعد منتصف الليل بيتحسب على
    يوم بداية الشيفت، والوقت بيتزاد 1440 دقيقة عشان حساب التأخير يفضل صح.
    """
    day = now_local.date()
    now_min = now_local.hour * 60 + now_local.minute
    sched = ensure_week(db, week_start_of(day)).get(e.id)
    shift = (sched.shift if sched else e.shift) or ""
    bounds = parse_shift(shift)
    if bounds and bounds[1] > 1440 and now_min < bounds[1] - 1440:
        day -= timedelta(days=1)
        now_min += 1440
        sched = ensure_week(db, week_start_of(day)).get(e.id)
        shift = (sched.shift if sched else e.shift) or ""
        bounds = parse_shift(shift)
    late = max(0, now_min - bounds[0]) if bounds else 0
    return SimpleNamespace(day=day, schedule=sched, shift=shift, bounds=bounds,
                           now_min=now_min, late_minutes=late)


def _qr_shift_window(ctx) -> Optional[tuple[int, int]]:
    if not ctx.bounds:
        return None
    return ctx.bounds


def _qr_late_fine(db: Session, e: Employee) -> float:
    """الغرامة الثابتة للتأخير لفرع الموظف ونوع تعاقده، مرة واحدة في اليوم."""
    settings = get_settings(db)
    branch_fines = (settings.get("branch_fines") or {}).get(e.branch) or {}
    value = branch_fines.get(f"late_{(e.employment_type or 'FT').lower()}")
    if value is None:
        value = settings.get("fine_late") or 0.0
    return float(value or 0)


def _qr_status_for(ctx, db: Session, e: Employee) -> tuple[str, float]:
    if ctx.late_minutes >= QR_LATE_GRACE_MIN:
        return "Late", _qr_late_fine(db, e)
    return "Present", 0.0


def _qr_clock_for(row: Attendance) -> str:
    if not row:
        return ""
    match = re.search(r"حضور QR\s+([0-2]\d:[0-5]\d)", row.note or "")
    if match:
        return match.group(1)
    if not row.updated_at:
        return ""
    return cairo_wall(row.updated_at).strftime("%H:%M")


def _qr_late_from_row(row: Attendance, ctx) -> int:
    if not row or row.status != "Late" or not ctx.bounds:
        return 0
    stamp_min = None
    match = re.search(r"حضور QR\s+([0-2]\d):([0-5]\d)", row.note or "")
    if match:
        stamp_min = int(match.group(1)) * 60 + int(match.group(2))
    elif row.updated_at:
        stamp = cairo_wall(row.updated_at)
        stamp_min = stamp.hour * 60 + stamp.minute
    if stamp_min is None:
        return 0
    if ctx.bounds[1] > 1440 and stamp_min < ctx.bounds[1] - 1440:
        stamp_min += 1440
    return max(0, stamp_min - ctx.bounds[0])


def _qr_write_audit(db: Session, e: Employee, day: date, old_status: str, old_fine: float,
                    new_status: str, new_fine: float, note: str) -> None:
    """أثر التدقيق مش جزء من نجاح الحضور — لو فشل ما بنفشلش العملية."""
    try:
        db.add(AttendanceAudit(
            employee_id=e.id, attendance_date=day,
            old_status=old_status or "", old_fine=float(old_fine or 0),
            new_status=new_status, new_fine=float(new_fine or 0),
            source="QR", note=note[:300], actor="QR", created_at=utcnow(),
        ))
        db.commit()
    except Exception:
        db.rollback()


def save_attendance_checkout(db: Session, e: Employee, day: date, requested_at: datetime) -> None:
    """وقت الانصراف المعتمد — في جدول منفصل عشان ما يلمسش حالة الحضور أو الغرامة."""
    stamp = cairo_wall(requested_at).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(text("""
        INSERT INTO attendance_checkouts (employee_id, checkout_date, checked_out_at, method, note)
        VALUES (:eid, :day, :stamp, 'QR', '')
        ON CONFLICT(employee_id, checkout_date)
        DO UPDATE SET checked_out_at = :stamp, method = 'QR'
    """), {"eid": e.id, "day": day.isoformat(), "stamp": stamp})


class QrAttendanceIn(BaseModel):
    token: str = Field(min_length=5, max_length=300)
    employee_code: str = Field(min_length=1, max_length=60)
    # للتوافق مع نسخة قديمة من التطبيق كانت بتبعت الموقع — بيتجاهل.
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy: Optional[float] = None


class QrCheckoutIn(BaseModel):
    employee_code: str = Field(min_length=1, max_length=60)


@app.get("/api/admin/qr/attendance")
def qr_attendance_live(branch: Optional[str] = None, role: str = Depends(require_auth)):
    """كود الـQR الحالي لشاشة المشرف. مشرف الفرع بياخد فرعه تلقائيًا."""
    scoped = role_branch(role)
    target = scoped or norm_branch(branch) or "QCD 2"
    return qr_live_payload(target)


@app.get("/api/admin/qr/live")
def qr_live(role: str = Depends(require_auth)):
    """نسخة قديمة من نفس الـendpoint — بنسيبها للتوافق."""
    return qr_live_payload(role_branch(role) or "QCD 2")


@app.get("/api/das/qr/live", dependencies=[Depends(require_das_secret)])
def das_qr_live():
    """بوابة DAS بتقرأ كود QCD 2 من هنا — نفس التوكن الموجود في الروستر."""
    return qr_live_payload("QCD 2")


@app.get("/api/das/employee", dependencies=[Depends(require_das_secret)])
def das_employee(employee_code: str = Query(..., min_length=1, max_length=60),
                 db: Session = Depends(get_db)):
    """بيانات المندوب من الروستر — المصدر الرسمي للفرع والشيفت."""
    e = _qr_employee(db, employee_code)
    if not e:
        raise HTTPException(404, "الكود مش مسجل في الروستر")
    sched = peek_week(db, week_start_of(qr_now_local().date())).get(e.id)
    return {
        "employee_id": e.id, "name": e.name, "employee_code": e.employee_code or "",
        "branch": e.branch, "shift": (sched.shift if sched else e.shift),
        "day_off": (sched.day_off if sched else e.day_off),
        "employment_type": e.employment_type, "active": bool(e.active),
        "attendance_only": e.branch == "QCD 2",
    }


@app.post("/api/attendance/qr")
def attendance_qr(data: QrAttendanceIn, request: Request, db: Session = Depends(get_db)):
    """تسجيل حضور فوري بعد مسح QR — بدون GPS وبدون موافقة مشرف."""
    branch = qr_token_branch(data.token)
    if not branch:
        raise HTTPException(410, "كود الحضور انتهت صلاحيته — امسح الكود المعروض دلوقتي على شاشة المشرف")

    e = _qr_employee(db, data.employee_code)
    if not e:
        raise HTTPException(404, "كودك مش متسجل في الروستر — كلّم المشرف")
    if not e.active:
        raise HTTPException(403, "حسابك موقوف في الروستر")
    if (e.branch or "").strip() != branch:
        raise HTTPException(409, f"الـQR ده بتاع {branch} — امسح الـQR بتاع فرعك ({e.branch})")

    now_local = qr_now_local()
    ctx = qr_shift_context(db, e, now_local)
    day = ctx.day
    if not ctx.schedule:
        raise HTTPException(409, "مش مضاف في جدول الأسبوع ده — كلّم المشرف")
    if (ctx.schedule.day_off or "") == day.strftime("%A"):
        raise HTTPException(409, "النهاردة إجازتك — مفيش تسجيل حضور")
    if not ctx.bounds:
        raise HTTPException(409, f"شيفتك «{ctx.shift}» مش مقروء — كلّم المشرف يظبطه")

    status, fine = _qr_status_for(ctx, db, e)
    clock = now_local.strftime("%H:%M")
    note = f"حضور QR {clock}"
    if status == "Late":
        note += f" · متأخر {ctx.late_minutes} دقيقة"

    existing = (db.query(Attendance)
                .filter(Attendance.employee_id == e.id, Attendance.attendance_date == day)
                .first())

    if existing is not None:
        old_status = existing.status or ""
        old_fine = float(existing.fine or 0)
        if old_status in ("Present", "Late"):
            return {"ok": True, "already": True, "corrected": False, "status": old_status,
                    "time": _qr_clock_for(existing), "late_minutes": _qr_late_from_row(existing, ctx),
                    "fine": old_fine, "employee": e.name, "branch": e.branch,
                    "attendance_date": day.isoformat(), "shift": ctx.shift}
        if old_status == "Absent":
            correction_note = ((existing.note or "") + ("\n" if existing.note else "")
                               + f"تصحيح بعد QR {clock}")[:500]
            item = AttendanceItem(employee_id=e.id, status=status, fine=fine,
                                  reason="", note=correction_note)
            res = _save_items(db, day, [item], actor="QR")
            if res["errors"]:
                raise HTTPException(400, res["errors"][0].get("detail", "تعذر تصحيح الحضور"))
            _qr_write_audit(db, e, day, old_status, old_fine, status, fine,
                            f"تصحيح بعد QR {clock}")
            publish_event("attendance", date=day.isoformat(), ids=[e.id], names=[e.name],
                          count=1, by="QR", client=client_tag(request))
            return {"ok": True, "already": False, "corrected": True, "status": status,
                    "time": clock, "late_minutes": ctx.late_minutes, "fine": float(fine),
                    "employee": e.name, "branch": e.branch,
                    "attendance_date": day.isoformat(), "shift": ctx.shift}
        # Excused أو أي حالة يدوية تانية: ما بنكتبش فوق قرار المشرف.
        return {"ok": True, "already": True, "corrected": False, "status": old_status,
                "time": _qr_clock_for(existing), "late_minutes": 0, "fine": old_fine,
                "employee": e.name, "branch": e.branch,
                "attendance_date": day.isoformat(), "shift": ctx.shift}

    item = AttendanceItem(employee_id=e.id, status=status, fine=fine, reason="", note=note)
    res = _save_items(db, day, [item], actor="QR")
    if res["errors"]:
        raise HTTPException(400, res["errors"][0].get("detail", "تعذر تسجيل الحضور"))

    _qr_write_audit(db, e, day, "", 0, status, fine, note)
    publish_event("attendance", date=day.isoformat(), ids=[e.id], names=[e.name],
                  count=1, by="QR", client=client_tag(request))
    return {"ok": True, "already": False, "corrected": False, "status": status,
            "time": clock, "late_minutes": ctx.late_minutes, "fine": float(fine),
            "employee": e.name, "branch": e.branch,
            "attendance_date": day.isoformat(), "shift": ctx.shift}


@app.post("/api/attendance/qr/checkout", dependencies=[Depends(require_das_secret)])
def qr_checkout_request(data: QrCheckoutIn, request: Request, db: Session = Depends(get_db)):
    """طلب انصراف من التطبيق — بيتسجل Pending والمشرف يوافق عليه من الروستر."""
    e = _qr_employee(db, data.employee_code)
    if not e:
        raise HTTPException(404, "كودك مش متسجل في الروستر — كلّم المشرف")
    if not e.active:
        raise HTTPException(403, "حسابك موقوف في الروستر")

    now_local = qr_now_local()
    ctx = qr_shift_context(db, e, now_local)
    day = ctx.day
    att = (db.query(Attendance)
           .filter(Attendance.employee_id == e.id, Attendance.attendance_date == day)
           .first())
    if att is None and ctx.bounds and ctx.bounds[1] > 1440:
        # الشيفت الليلي: لو الانصراف بعد نهاية الشيفت، سجل الحضور على اليوم السابق.
        prev = day - timedelta(days=1)
        prev_att = (db.query(Attendance)
                    .filter(Attendance.employee_id == e.id, Attendance.attendance_date == prev)
                    .first())
        if prev_att is not None:
            day, att = prev, prev_att
    if att is None or (att.status or "") not in ("Present", "Late"):
        raise HTTPException(409, "مفيش حضور مسجل في اليوم ده — سجّل حضورك الأول")

    existing = (db.query(DasRequest)
                .filter(DasRequest.employee_id == e.id, DasRequest.action == "check_out",
                        DasRequest.attendance_date == day,
                        DasRequest.status.in_(("Pending", "Approved")))
                .order_by(DasRequest.id.desc()).first())
    if existing is not None:
        return {"ok": True, "already": True, "status": existing.status,
                "request_id": existing.id, "attendance_date": day.isoformat(),
                "time": cairo_wall(existing.requested_at).strftime("%H:%M") if existing.requested_at else "",
                "message": "طلب الانصراف قيد موافقة المشرف" if existing.status == "Pending"
                           else "انصرافك معتمد بالفعل"}

    row = DasRequest(employee_id=e.id, action="check_out", attendance_date=day,
                     status="Pending", requested_at=utcnow(), courier_local_at="",
                     checkin_id=None, source="QR")
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = (db.query(DasRequest)
                    .filter(DasRequest.employee_id == e.id, DasRequest.action == "check_out",
                            DasRequest.attendance_date == day,
                            DasRequest.status.in_(("Pending", "Approved")))
                    .order_by(DasRequest.id.desc()).first())
        if existing is None:
            raise HTTPException(409, "طلب متكرر — جرّب تاني")
        return {"ok": True, "already": True, "status": existing.status,
                "request_id": existing.id, "attendance_date": day.isoformat(),
                "time": "", "message": "طلب الانصراف قيد موافقة المشرف"}
    db.refresh(row)
    publish_event("das_requests", action="create", names=[e.name], branch=e.branch,
                  by="QR", client=client_tag(request))
    return {"ok": True, "already": False, "status": "Pending", "request_id": row.id,
            "attendance_date": day.isoformat(), "time": now_local.strftime("%H:%M"),
            "message": "تم إرسال طلب الانصراف — مستني موافقة المشرف"}


@app.get("/api/attendance/qr/checkout/status", dependencies=[Depends(require_das_secret)])
def qr_checkout_status(employee_code: str = Query(..., min_length=1, max_length=60),
                       db: Session = Depends(get_db)):
    e = _qr_employee(db, employee_code)
    if not e:
        raise HTTPException(404, "كودك مش متسجل في الروستر")
    row = (db.query(DasRequest)
           .filter(DasRequest.employee_id == e.id, DasRequest.action == "check_out")
           .order_by(DasRequest.id.desc()).first())
    co = None
    if row is not None:
        co = db.execute(text(
            "SELECT checked_out_at FROM attendance_checkouts "
            "WHERE employee_id = :eid AND checkout_date = :day"
        ), {"eid": e.id, "day": row.attendance_date.isoformat()}).first()
    return {
        "status": row.status if row else "",
        "request_id": row.id if row else None,
        "attendance_date": row.attendance_date.isoformat() if row else "",
        "requested_at": row.requested_at.isoformat() if row and row.requested_at else "",
        "decided_by": (row.decided_by or "") if row else "",
        "decided_at": row.decided_at.isoformat() if row and row.decided_at else "",
        "decision_note": (row.decision_note or "") if row else "",
        "checked_out_at": (co[0] if co else ""),
        "checkout_method": "QR",
    }


@app.get("/api/attendance/qr/status", dependencies=[Depends(require_das_secret)])
def qr_attendance_status(employee_code: str = Query(..., min_length=1, max_length=60),
                         db: Session = Depends(get_db)):
    """حالة حضور المندوب النهاردة + شيفته + طلب الانصراف — بيقرأها Bankai."""
    e = _qr_employee(db, employee_code)
    if not e:
        raise HTTPException(404, "كودك مش متسجل في الروستر")
    now_local = qr_now_local()
    ctx = qr_shift_context(db, e, now_local)
    day = ctx.day
    att = (db.query(Attendance)
           .filter(Attendance.employee_id == e.id, Attendance.attendance_date == day)
           .first())
    if att is None and ctx.bounds and ctx.bounds[1] > 1440:
        # After an overnight shift ends, keep showing the shift's own day.
        prev = day - timedelta(days=1)
        prev_att = (db.query(Attendance)
                    .filter(Attendance.employee_id == e.id,
                            Attendance.attendance_date == prev)
                    .first())
        if prev_att is not None:
            day, att = prev, prev_att
    window = _qr_shift_window(ctx)
    inside = bool(window and window[0] <= ctx.now_min <= window[1])
    day_off = bool(ctx.schedule and (ctx.schedule.day_off or "") == day.strftime("%A"))
    preview = None
    if ((att is None or (att.status or "") == "Absent")
            and not day_off and ctx.bounds):
        preview_status, preview_fine = _qr_status_for(ctx, db, e)
        preview = {
            "status": preview_status,
            "late_minutes": ctx.late_minutes,
            "fine": float(preview_fine),
            "at": now_local.strftime("%H:%M"),
        }
    co = db.execute(text(
        "SELECT checked_out_at FROM attendance_checkouts "
        "WHERE employee_id = :eid AND checkout_date = :day"
    ), {"eid": e.id, "day": day.isoformat()}).first()
    req = (db.query(DasRequest)
           .filter(DasRequest.employee_id == e.id, DasRequest.action == "check_out",
                   DasRequest.attendance_date == day)
           .order_by(DasRequest.id.desc()).first())
    return {
        "employee": e.name, "employee_code": e.employee_code or "", "branch": e.branch,
        "employment_type": e.employment_type, "date": day.isoformat(), "shift": ctx.shift,
        "day_off": day_off, "server_time": now_local.strftime("%H:%M"),
        "grace_min": QR_LATE_GRACE_MIN,
        "window": {"opens": hhmm(window[0] % 1440) if window else "",
                   "closes": hhmm(window[1] % 1440) if window else ""},
        "can_check_in": bool((att is None or (att.status or "") == "Absent")
                              and not day_off and ctx.bounds),
        "inside_shift": inside,
        "preview": preview,
        "attendance": ({
            "status": att.status, "fine": float(att.fine or 0), "note": att.note or "",
            "recorded_by": att.recorded_by or "", "time": _qr_clock_for(att),
            "late_minutes": _qr_late_from_row(att, ctx),
        } if att else None),
        "checkout": ({
            "status": req.status, "request_id": req.id,
            "decided_by": req.decided_by or "",
            "decided_at": req.decided_at.isoformat() if req.decided_at else "",
            "decision_note": req.decision_note or "",
            "checked_out_at": co[0] if co else "",
        } if req else ({"status": "Approved", "checked_out_at": co[0]} if co else None)),
    }


@app.get("/qr", response_class=HTMLResponse)
def qr_screen(pw: str = "", branch: Optional[str] = None):
    """شاشة كبيرة احتياطية للـQR — الأساس بقى الكارت داخل sidebar الروستر."""
    if not pw or not secrets.compare_digest(pw, ADMIN_PASSWORD):
        return HTMLResponse("<h3 style='font-family:sans-serif'>كلمة السر غلط</h3>", status_code=401)
    from urllib.parse import quote as _quote
    target = norm_branch(branch) or "QCD 2"
    token = qr_token(target)
    expires = max(1, QR_TOKEN_SECONDS - int(time.time()) % QR_TOKEN_SECONDS)
    img = ("https://api.qrserver.com/v1/create-qr-code/?size=480x480&margin=8&data="
           + _quote(token))
    html = f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="15">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>حضور بالـ QR</title>
<style>
 body{{margin:0;background:#070B12;color:#F3F7FC;font-family:system-ui,-apple-system,sans-serif;
      display:grid;place-items:center;min-height:100vh}}
 .card{{background:#fff;padding:18px;border-radius:18px}}
 h1{{font-size:20px;margin:0 0 12px;text-align:center}}
 p{{margin:12px 0 0;text-align:center;color:#475569;font-size:14px}}
</style></head><body>
<div><h1>QR الحضور — {target}</h1><div class="card"><img src="{img}" width="480" height="480" alt="QR"></div>
<p>الكود يتغيّر كل 30 ثانية · يتجدد خلال {expires} ث</p></div>
</body></html>"""
    return HTMLResponse(html)
