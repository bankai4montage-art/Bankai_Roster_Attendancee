import os
import csv
import io
import hmac
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
        "day_off": e.day_off, "active": e.active, "phone": e.phone or ""
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
