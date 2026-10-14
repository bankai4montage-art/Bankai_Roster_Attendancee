import os
import csv
import io
import hmac
import re
import hashlib
import secrets
import sqlite3
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional, List, Dict

from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import (create_engine, Column, Integer, String, Boolean, Date, DateTime, Float,
                        ForeignKey, UniqueConstraint, func, case, or_, inspect, text)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from starlette.background import BackgroundTask

APP_NAME = "Bankai Roster & Attendance"
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bankai.db")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ChangeThisPassword123!")
# المفتاح السري لتوقيع جلسات الدخول. لو مش متحدد بيتولد من كلمة المرور فتفضل الجلسات شغالة بعد إعادة التشغيل.
SECRET_KEY = (os.getenv("SECRET_KEY") or os.getenv("ADMIN_TOKEN")
              or hashlib.sha256(("bankai|" + ADMIN_PASSWORD).encode()).hexdigest())
TOKEN_TTL_SECONDS = 30 * 24 * 3600

BRANCHES = ("QCD 1", "QCD 2")
EMP_TYPES = ("FT", "PT")
WEEKDAYS = ("Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
STATUSES = ("Present", "Late", "Absent", "Excused")
NO_FINE_STATUSES = ("Present", "Excused")

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

if ADMIN_PASSWORD == "ChangeThisPassword123!":
    print("WARNING: ADMIN_PASSWORD is the default value. Set a strong ADMIN_PASSWORD environment variable.")

app = FastAPI(title=APP_NAME, version="2.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
security = HTTPBearer(auto_error=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------- auth
def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    exp = str(int(time.time()) + TOKEN_TTL_SECONDS)
    return f"{exp}.{_sign(exp)}"


def valid_token(token: str) -> bool:
    try:
        exp, sig = token.split(".", 1)
        return hmac.compare_digest(sig, _sign(exp)) and int(exp) > time.time()
    except Exception:
        return False


def require_auth(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials or not valid_token(credentials.credentials):
        raise HTTPException(status_code=401, detail="جلسة الدخول غير صالحة. سجّل الدخول مرة أخرى.")
    return True


_login_state: Dict[str, List[float]] = {}  # ip -> [failed_count, locked_until]
MAX_FAILS = 8
LOCK_SECONDS = 300


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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


class AttendanceIn(BaseModel):
    employee_id: int
    attendance_date: date
    status: str
    fine: float = Field(default=0, ge=0, le=1000000)
    reason: str = Field(default="", max_length=500)
    note: str = Field(default="", max_length=500)


class AttendanceBulkIn(BaseModel):
    attendance_date: date
    items: List[AttendanceItem]


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


# ---------------------------------------------------------------- pages / auth routes
@app.get("/", response_class=HTMLResponse)
def home():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/health")
def health():
    return {"status": "ok", "app": APP_NAME}


@app.post("/api/login")
def login(data: LoginIn, request: Request):
    ip = client_ip(request)
    fails, locked_until = _login_state.get(ip, [0, 0.0])
    now = time.time()
    if locked_until > now:
        minutes = int((locked_until - now) // 60) + 1
        raise HTTPException(429, f"محاولات كثيرة خاطئة. حاول مرة أخرى بعد {minutes} دقيقة.")
    if not secrets.compare_digest(data.password.encode(), ADMIN_PASSWORD.encode()):
        fails += 1
        if fails >= MAX_FAILS:
            _login_state[ip] = [0, now + LOCK_SECONDS]
        else:
            _login_state[ip] = [fails, 0.0]
        raise HTTPException(401, "كلمة المرور غير صحيحة")
    _login_state.pop(ip, None)
    return {"token": make_token(), "message": "تم تسجيل الدخول"}


@app.get("/api/me")
def me(_: bool = Depends(require_auth)):
    return {"app": APP_NAME, "authenticated": True}


# ---------------------------------------------------------------- settings
@app.get("/api/settings")
def read_settings(_: bool = Depends(require_auth), db: Session = Depends(get_db)):
    return get_settings(db)


@app.put("/api/settings")
def update_settings(data: SettingsIn, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    for key, value in (("fine_late", data.fine_late), ("fine_absent", data.fine_absent)):
        row = db.query(Setting).filter(Setting.key == key).first()
        if row:
            row.value = str(value)
        else:
            db.add(Setting(key=key, value=str(value)))
    db.commit()
    return get_settings(db)


# ---------------------------------------------------------------- employees
@app.get("/api/employees")
def list_employees(branch: Optional[str] = None, active_only: bool = False,
                   _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    q = db.query(Employee)
    if branch in BRANCHES:
        q = q.filter(Employee.branch == branch)
    if active_only:
        q = q.filter(Employee.active.is_(True))
    return [employee_dict(e) for e in q.order_by(Employee.branch, Employee.name).all()]


@app.post("/api/employees")
def create_employee(data: EmployeeIn, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    check_employee_values(data.branch, data.employment_type, data.day_off)
    code = (data.employee_code or "").strip() or None
    if code and db.query(Employee).filter(Employee.employee_code == code).first():
        raise HTTPException(409, "كود الموظف مستخدم بالفعل")
    e = Employee(name=data.name.strip(), employee_code=code, branch=data.branch, shift=data.shift.strip(),
                 employment_type=data.employment_type, day_off=data.day_off, active=data.active,
                 phone=(data.phone or "").strip() or None)
    db.add(e)
    db.commit()
    db.refresh(e)
    return employee_dict(e)


@app.put("/api/employees/{employee_id}")
def update_employee(employee_id: int, data: EmployeeIn, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    e = db.query(Employee).filter(Employee.id == employee_id).first()
    if not e:
        raise HTTPException(404, "الموظف غير موجود")
    check_employee_values(data.branch, data.employment_type, data.day_off)
    code = (data.employee_code or "").strip() or None
    if code and db.query(Employee).filter(Employee.employee_code == code, Employee.id != employee_id).first():
        raise HTTPException(409, "كود الموظف مستخدم بالفعل")
    e.name = data.name.strip()
    e.shift = data.shift.strip()
    e.employee_code = code
    e.branch = data.branch
    e.employment_type = data.employment_type
    e.day_off = data.day_off
    e.active = data.active
    e.phone = (data.phone or "").strip() or None
    db.commit()
    db.refresh(e)
    return employee_dict(e)


@app.delete("/api/employees/{employee_id}")
def delete_employee(employee_id: int, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    e = db.query(Employee).filter(Employee.id == employee_id).first()
    if not e:
        raise HTTPException(404, "الموظف غير موجود")
    # نحافظ على سجل الحضور القديم: إيقاف بدل الحذف النهائي.
    e.active = False
    db.commit()
    return {"message": "تم إيقاف الموظف مع الاحتفاظ بسجل حضوره السابق"}


@app.post("/api/employees/bulk")
def bulk_employees(data: BulkEmployeesIn, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
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
            if shift is not None:
                e.shift = shift
            if data.employment_type is not None:
                e.employment_type = data.employment_type
            if data.day_off is not None:
                e.day_off = data.day_off
    db.commit()
    return {"updated": len(emps)}


@app.post("/api/employees/import")
def import_employees(data: ImportIn, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
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
            match.name, match.branch, match.shift = name, branch, shift
            if etype:
                match.employment_type = etype
            if day:
                match.day_off = day
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
            if code:
                by_code[code] = e
            by_name.setdefault((name.lower(), branch), e)
            created += 1

    if data.dry_run:
        db.rollback()
    else:
        db.commit()
    return {"created": created, "updated": updated, "skipped": skipped,
            "errors": errors, "dry_run": data.dry_run}


@app.get("/api/employees/export.csv")
def export_employees(_: bool = Depends(require_auth), db: Session = Depends(get_db)):
    rows = db.query(Employee).order_by(Employee.branch, Employee.shift, Employee.name).all()
    return csv_response(
        ["name", "employee_code", "branch", "shift", "employment_type", "day_off", "phone", "active"],
        [[e.name, e.employee_code or "", e.branch, e.shift, e.employment_type, e.day_off,
          e.phone or "", "yes" if e.active else "no"] for e in rows],
        "bankai_employees.csv")


# ---------------------------------------------------------------- roster & attendance
@app.get("/api/roster")
def roster(attendance_date: date = Query(...), branch: Optional[str] = None,
           _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    weekday = attendance_date.strftime("%A")
    base = db.query(Employee).filter(Employee.active.is_(True))
    if branch in BRANCHES:
        base = base.filter(Employee.branch == branch)
    employees = base.filter(Employee.day_off != weekday).order_by(Employee.branch, Employee.shift, Employee.name).all()
    off = base.filter(Employee.day_off == weekday).order_by(Employee.branch, Employee.name).all()
    ids = [e.id for e in employees]
    existing = {}
    if ids:
        rows = db.query(Attendance).filter(Attendance.attendance_date == attendance_date,
                                           Attendance.employee_id.in_(ids)).all()
        existing = {a.employee_id: attendance_dict(a) for a in rows}
    out = []
    for e in employees:
        row = employee_dict(e)
        row["attendance"] = existing.get(e.id)
        out.append(row)
    return {"date": attendance_date.isoformat(), "weekday": weekday, "employees": out,
            "off_employees": [employee_dict(e) for e in off]}


def _save_items(db: Session, day: date, items: List[AttendanceItem]):
    weekday = day.strftime("%A")
    ids = list({i.employee_id for i in items})
    emps = {e.id: e for e in db.query(Employee).filter(Employee.id.in_(ids)).all()}
    existing = {a.employee_id: a for a in db.query(Attendance).filter(
        Attendance.attendance_date == day, Attendance.employee_id.in_(ids)).all()}
    saved = cleared = 0
    errors, touched = [], []
    for it in items:
        e = emps.get(it.employee_id)
        if not e:
            errors.append({"employee_id": it.employee_id, "detail": "الموظف غير موجود"}); continue
        if not e.active:
            errors.append({"employee_id": e.id, "name": e.name, "detail": "الموظف غير نشط"}); continue
        if e.day_off == weekday:
            errors.append({"employee_id": e.id, "name": e.name, "detail": "هذا الموظف في إجازة في هذا اليوم"}); continue
        status = (it.status or "").strip()
        row = existing.get(e.id)
        if status == "":
            if row:
                db.delete(row)
                existing.pop(e.id, None)
                cleared += 1
            touched.append(e.id)
            continue
        if status not in STATUSES:
            errors.append({"employee_id": e.id, "name": e.name, "detail": "حالة الحضور غير صحيحة"}); continue
        fine = 0.0 if status in NO_FINE_STATUSES else float(it.fine or 0)
        reason = "" if status == "Present" else (it.reason or "").strip()
        note = (it.note or "").strip()
        if row:
            row.status, row.fine, row.reason, row.note = status, fine, reason, note
            row.updated_at = utcnow()
        else:
            row = Attendance(employee_id=e.id, attendance_date=day, status=status,
                             fine=fine, reason=reason, note=note)
            db.add(row)
            existing[e.id] = row
        saved += 1
        touched.append(e.id)
    db.flush()
    records = {eid: (attendance_dict(existing[eid]) if eid in existing else None) for eid in touched}
    db.commit()
    return saved, cleared, errors, records


@app.post("/api/attendance")
def save_attendance(data: AttendanceIn, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    if data.status not in STATUSES:
        raise HTTPException(400, "حالة الحضور غير صحيحة")
    item = AttendanceItem(employee_id=data.employee_id, status=data.status, fine=data.fine,
                          reason=data.reason, note=data.note)
    _, _, errors, records = _save_items(db, data.attendance_date, [item])
    if errors:
        code = 404 if errors[0]["detail"] == "الموظف غير موجود" else 400
        raise HTTPException(code, errors[0]["detail"])
    return records[data.employee_id]


@app.post("/api/attendance/bulk")
def save_attendance_bulk(data: AttendanceBulkIn, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    if not data.items:
        raise HTTPException(400, "لا توجد تغييرات للحفظ")
    if len(data.items) > 2000:
        raise HTTPException(400, "عدد التسجيلات كبير جدًا في طلب واحد")
    saved, cleared, errors, records = _save_items(db, data.attendance_date, data.items)
    return {"saved": saved, "cleared": cleared, "errors": errors, "records": records}


# ---------------------------------------------------------------- dashboard
@app.get("/api/dashboard")
def dashboard(attendance_date: date = Query(...), _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    weekday = attendance_date.strftime("%A")
    active = db.query(Employee).filter(Employee.active.is_(True)).all()
    att = {a.employee_id: a for a in db.query(Attendance).filter(Attendance.attendance_date == attendance_date).all()}

    def blank():
        return {"scheduled": 0, "present": 0, "late": 0, "absent": 0, "excused": 0, "unmarked": 0, "fines": 0.0}

    totals = blank()
    branches = {b: dict(blank(), shifts={}) for b in BRANCHES}
    off_today = 0
    for e in active:
        if e.day_off == weekday:
            off_today += 1
            continue
        b = branches.setdefault(e.branch, dict(blank(), shifts={}))
        sh = b["shifts"].setdefault(e.shift, blank())
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

    start = attendance_date - timedelta(days=6)
    trend_rows = (db.query(Attendance.attendance_date, Attendance.status, func.count(Attendance.id))
                  .select_from(Attendance).join(Employee, Attendance.employee_id == Employee.id)
                  .filter(Employee.active.is_(True), Attendance.attendance_date >= start,
                          Attendance.attendance_date <= attendance_date)
                  .group_by(Attendance.attendance_date, Attendance.status).all())
    trend_map = {}
    for d, st, c in trend_rows:
        trend_map.setdefault(d, {})[st] = int(c)
    trend = []
    for i in range(7):
        d = start + timedelta(days=i)
        m = trend_map.get(d, {})
        trend.append({"date": d.isoformat(), "present": m.get("Present", 0), "late": m.get("Late", 0),
                      "absent": m.get("Absent", 0), "excused": m.get("Excused", 0)})

    w_start = attendance_date - timedelta(days=29)
    w_rows = (db.query(Employee.id, Employee.name, Employee.branch, Employee.shift,
                       func.sum(case((Attendance.status == "Absent", 1), else_=0)).label("absent"),
                       func.sum(case((Attendance.status == "Late", 1), else_=0)).label("late"),
                       func.coalesce(func.sum(Attendance.fine), 0).label("fines"))
              .select_from(Attendance).join(Employee, Attendance.employee_id == Employee.id)
              .filter(Employee.active.is_(True), Attendance.attendance_date >= w_start,
                      Attendance.attendance_date <= attendance_date)
              .group_by(Employee.id, Employee.name, Employee.branch, Employee.shift).all())
    watch = []
    for r in w_rows:
        absent, late = int(r.absent or 0), int(r.late or 0)
        if absent or late:
            watch.append({"id": r.id, "name": r.name, "branch": r.branch, "shift": r.shift,
                          "absent": absent, "late": late, "fines": round(float(r.fines or 0), 2)})
    watch.sort(key=lambda x: (x["absent"] * 2 + x["late"], x["fines"]), reverse=True)

    return {"date": attendance_date.isoformat(), "weekday": weekday, "active_total": len(active),
            "off_today": off_today, "totals": totals, "branches": branches,
            "trend": trend, "watchlist": watch[:8]}


# ---------------------------------------------------------------- reports
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
    for r in rows:
        c = _counts(r)
        rate = round((c["present"] + c["late"]) * 100 / c["total"], 1) if c["total"] else 0
        out.append(dict(c, id=r.id, name=r.name, employee_code=r.code or "", branch=r.branch,
                        shift=r.shift, employment_type=r.etype, active=bool(r.active), rate=rate))
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
    return {"employee": employee_dict(e), "start": start.isoformat(), "end": end.isoformat(),
            "stats": stats, "records": records}


@app.get("/api/reports/export.csv")
def export_csv(start: date, end: date, branch: Optional[str] = None,
               _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    _check_range(start, end)
    q = db.query(Attendance, Employee).join(Employee, Attendance.employee_id == Employee.id).filter(
        Attendance.attendance_date >= start, Attendance.attendance_date <= end)
    if branch in BRANCHES:
        q = q.filter(Employee.branch == branch)
    rows = q.order_by(Attendance.attendance_date, Employee.branch, Employee.name).all()
    return csv_response(
        ["Date", "Branch", "Employee Code", "Name", "Type", "Shift", "Day Off", "Status", "Fine (EGP)", "Reason", "Note"],
        [[a.attendance_date.isoformat(), e.branch, e.employee_code or "", e.name, e.employment_type,
          e.shift, e.day_off, a.status, f"{a.fine:.2f}", a.reason or "", a.note or ""] for a, e in rows],
        f"bankai_attendance_{start}_{end}.csv")


@app.get("/api/reports/employees.csv")
def export_employees_report(start: date, end: date, branch: Optional[str] = None, q: Optional[str] = None,
                            _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    data = report_employees(start, end, branch, q, True, db)
    return csv_response(
        ["Branch", "Employee Code", "Name", "Type", "Shift", "Records", "Present", "Late", "Absent", "Excused",
         "Attendance Rate %", "Fines (EGP)"],
        [[r["branch"], r["employee_code"], r["name"], r["employment_type"], r["shift"], r["total"], r["present"],
          r["late"], r["absent"], r["excused"], r["rate"], f"{r['fine_total']:.2f}"] for r in data["employees"]],
        f"bankai_employees_summary_{start}_{end}.csv")


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


class ScheduleAddItem(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    employment_type: str = "FT"
    day_off: Optional[str] = None
    break_start: Optional[str] = None


class ScheduleAddIn(BaseModel):
    branch: str
    shift: str = Field(min_length=1, max_length=80)
    items: List[ScheduleAddItem]


class ScheduleApplyItem(BaseModel):
    id: int
    day_off: Optional[str] = None
    break_start: Optional[str] = None      # "" = مسح البريك، None = بدون تغيير
    break_min: Optional[int] = Field(default=None, ge=5, le=120)
    off_locked: Optional[bool] = None
    shift: Optional[str] = Field(default=None, max_length=80)


class ScheduleApplyIn(BaseModel):
    items: List[ScheduleApplyItem]


class ScheduleAutoIn(BaseModel):
    branch: str
    shift: Optional[str] = None
    include_locked: bool = False
    allowed_days: List[str] = Field(default_factory=lambda: list(WEEKDAYS))
    distribute_off: bool = True
    distribute_breaks: bool = True
    overwrite_breaks: bool = False
    break_min: int = Field(default=60, ge=5, le=120)


def _check_branch(branch):
    if branch not in BRANCHES:
        raise HTTPException(400, "الفرع يجب أن يكون QCD 1 أو QCD 2")


@app.get("/api/schedule")
def get_schedule(branch: str, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    _check_branch(branch)
    emps = db.query(Employee).filter(Employee.branch == branch, Employee.active.is_(True)).order_by(Employee.name).all()
    names = {d.name for d in db.query(ShiftDef).filter(ShiftDef.branch == branch).all()} | {e.shift for e in emps}
    return {"branch": branch, "days": list(WEEKDAYS), "shifts": sorted(names, key=shift_sort_key),
            "employees": [employee_dict(e) for e in emps]}


@app.post("/api/schedule/shifts")
def create_shift(data: ShiftIn, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    _check_branch(data.branch)
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "اسم الشيفت مطلوب")
    if not db.query(ShiftDef).filter(ShiftDef.branch == data.branch, ShiftDef.name == name).first():
        db.add(ShiftDef(branch=data.branch, name=name))
        db.commit()
    return {"name": name}


@app.delete("/api/schedule/shifts")
def delete_shift(branch: str, name: str, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    _check_branch(branch)
    if db.query(Employee).filter(Employee.branch == branch, Employee.shift == name, Employee.active.is_(True)).count():
        raise HTTPException(400, "الشيفت فيه موظفين. انقلهم لشيفت آخر أو أوقفهم أولًا.")
    db.query(ShiftDef).filter(ShiftDef.branch == branch, ShiftDef.name == name).delete()
    db.commit()
    return {"message": "تم حذف الشيفت"}


@app.post("/api/schedule/shifts/rename")
def rename_shift(data: ShiftRenameIn, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    _check_branch(data.branch)
    new = data.new.strip()
    if not new:
        raise HTTPException(400, "اسم الشيفت مطلوب")
    if new == data.old:
        return {"name": new, "moved": 0}
    moved = db.query(Employee).filter(Employee.branch == data.branch, Employee.shift == data.old).update({"shift": new})
    db.query(ShiftDef).filter(ShiftDef.branch == data.branch, ShiftDef.name == data.old).delete()
    if not db.query(ShiftDef).filter(ShiftDef.branch == data.branch, ShiftDef.name == new).first():
        db.add(ShiftDef(branch=data.branch, name=new))
    db.commit()
    return {"name": new, "moved": moved}


@app.post("/api/schedule/add")
def schedule_add(data: ScheduleAddIn, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    _check_branch(data.branch)
    shift = data.shift.strip()
    if not shift:
        raise HTTPException(400, "الشيفت مطلوب")
    if not data.items:
        raise HTTPException(400, "اكتب اسم واحد على الأقل")
    if len(data.items) > 500:
        raise HTTPException(400, "الحد الأقصى 500 اسم في المرة")
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
            if e.active and e.shift == shift:
                notes.append({"name": e.name, "type": "same"})
            else:
                notes.append({"name": e.name, "type": "moved" if e.active else "reactivated",
                              "from": e.shift if e.active else None})
                e.shift, e.active = shift, True
                if day:
                    e.day_off, e.off_locked = day, True
                if bs:
                    e.break_start = bs
        else:
            e = Employee(name=name, branch=data.branch, shift=shift, employment_type=etype,
                         day_off=day or "Friday", off_locked=bool(day), break_start=bs, break_min=60, active=True)
            db.add(e)
            existing[name.lower()] = e
            created += 1
            notes.append({"name": name, "type": "created"})
        out.append(e)
    db.commit()
    for e in out:
        db.refresh(e)
    return {"created": created, "notes": notes, "employees": [employee_dict(e) for e in out]}


@app.post("/api/schedule/apply")
def schedule_apply(data: ScheduleApplyIn, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    if not data.items:
        raise HTTPException(400, "لا توجد تغييرات للحفظ")
    if len(data.items) > 2000:
        raise HTTPException(400, "عدد التعديلات كبير جدًا في طلب واحد")
    emps = {e.id: e for e in db.query(Employee).filter(Employee.id.in_([i.id for i in data.items])).all()}
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
        if it.day_off is not None:
            e.day_off = it.day_off
        if bs is not None:
            e.break_start = bs or None
        if it.break_min is not None:
            e.break_min = it.break_min
        if it.off_locked is not None:
            e.off_locked = it.off_locked
        if sh is not None:
            e.shift = sh
            if not db.query(ShiftDef).filter(ShiftDef.branch == e.branch, ShiftDef.name == sh).first():
                db.add(ShiftDef(branch=e.branch, name=sh))
        updated += 1
    db.commit()
    return {"updated": updated, "errors": errors}


@app.post("/api/schedule/auto")
def schedule_auto(data: ScheduleAutoIn, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    """يقترح توزيع إجازات (متوازن على أيام الأسبوع داخل كل شيفت) وبريكات متدرّجة. مابيحفظش حاجة — الواجهة بتعرض الاقتراح وانت تحفظ."""
    _check_branch(data.branch)
    allowed = [d for d in WEEKDAYS if d in data.allowed_days]
    if data.distribute_off and not allowed:
        raise HTTPException(400, "اختر يوم واحد على الأقل متاح للإجازة")
    q = db.query(Employee).filter(Employee.branch == data.branch, Employee.active.is_(True))
    if data.shift:
        q = q.filter(Employee.shift == data.shift)
    groups = {}
    for e in q.order_by(Employee.name).all():
        groups.setdefault(e.shift, []).append(e)
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
def schedule_export(branch: Optional[str] = None, _: bool = Depends(require_auth), db: Session = Depends(get_db)):
    q = db.query(Employee).filter(Employee.active.is_(True))
    if branch in BRANCHES:
        q = q.filter(Employee.branch == branch)
    emps = sorted(q.all(), key=lambda e: (e.branch, shift_sort_key(e.shift), e.name))
    return csv_response(
        ["الفرع", "الشيفت", "الاسم", "الكود", "النوع", "يوم الإجازة", "بداية البريك", "مدة البريك (دقيقة)"],
        [[e.branch, e.shift, e.name, e.employee_code or "", e.employment_type, WD_AR.get(e.day_off, e.day_off),
          e.break_start or "", (e.break_min or 60) if e.break_start else ""] for e in emps],
        f"bankai_schedule_{(branch or 'all').replace(' ', '')}.csv")


# ---------------------------------------------------------------- backup
@app.get("/api/backup")
def backup(_: bool = Depends(require_auth)):
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
