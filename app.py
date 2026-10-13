import os
import csv
import io
from datetime import date, datetime, time
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Boolean, Date, DateTime, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

APP_NAME = "Bankai Roster & Attendance"
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bankai.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

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
    created_at = Column(DateTime, default=datetime.utcnow)

class Attendance(Base):
    __tablename__ = "attendance"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    attendance_date = Column(Date, nullable=False, index=True)
    status = Column(String(20), nullable=False)
    fine = Column(Float, nullable=False, default=0)
    reason = Column(String(500), nullable=True, default="")
    note = Column(String(500), nullable=True, default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    employee = relationship("Employee")
    __table_args__ = (UniqueConstraint("employee_id", "attendance_date", name="uq_employee_attendance_day"),)

Base.metadata.create_all(bind=engine)
app = FastAPI(title=APP_NAME, version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class EmployeeIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    employee_code: Optional[str] = Field(default=None, max_length=60)
    branch: str
    shift: str = Field(min_length=1, max_length=80)
    employment_type: str
    day_off: str = "Friday"
    active: bool = True

class AttendanceIn(BaseModel):
    employee_id: int
    attendance_date: date
    status: str
    fine: float = Field(default=0, ge=0, le=1000000)
    reason: str = Field(default="", max_length=500)
    note: str = Field(default="", max_length=500)

def employee_dict(e):
    return {
        "id": e.id, "name": e.name, "employee_code": e.employee_code or "",
        "branch": e.branch, "shift": e.shift, "employment_type": e.employment_type,
        "day_off": e.day_off, "active": e.active
    }

def attendance_dict(a):
    return {
        "id": a.id, "employee_id": a.employee_id,
        "attendance_date": a.attendance_date.isoformat(), "status": a.status,
        "fine": a.fine, "reason": a.reason or "", "note": a.note or "",
        "updated_at": a.updated_at.isoformat() if a.updated_at else ""
    }

@app.get("/", response_class=HTMLResponse)
def home():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/health")
def health():
    return {"status": "ok", "app": APP_NAME}

@app.get("/api/me")
def me():
    return {"app": APP_NAME, "authenticated": True}

@app.get("/api/employees")
def list_employees(branch: Optional[str] = None, active_only: bool = False, db: Session = Depends(get_db)):
    q = db.query(Employee)
    if branch in ("QCD 1", "QCD 2"):
        q = q.filter(Employee.branch == branch)
    if active_only:
        q = q.filter(Employee.active.is_(True))
    return [employee_dict(e) for e in q.order_by(Employee.branch, Employee.name).all()]

@app.post("/api/employees")
def create_employee(data: EmployeeIn, db: Session = Depends(get_db)):
    if data.branch not in ("QCD 1", "QCD 2"):
        raise HTTPException(400, "الفرع يجب أن يكون QCD 1 أو QCD 2")
    if data.employment_type not in ("FT", "PT"):
        raise HTTPException(400, "نوع التعاقد يجب أن يكون FT أو PT")
    if data.day_off not in ("Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"):
        raise HTTPException(400, "يوم الإجازة غير صحيح")
    code = (data.employee_code or "").strip() or None
    if code and db.query(Employee).filter(Employee.employee_code == code).first():
        raise HTTPException(409, "كود الموظف مستخدم بالفعل")
    e = Employee(name=data.name.strip(), employee_code=code, branch=data.branch, shift=data.shift.strip(),
                 employment_type=data.employment_type, day_off=data.day_off, active=data.active)
    db.add(e)
    db.commit()
    db.refresh(e)
    return employee_dict(e)

@app.put("/api/employees/{employee_id}")
def update_employee(employee_id: int, data: EmployeeIn, db: Session = Depends(get_db)):
    e = db.query(Employee).filter(Employee.id == employee_id).first()
    if not e:
        raise HTTPException(404, "الموظف غير موجود")
    if data.branch not in ("QCD 1", "QCD 2") or data.employment_type not in ("FT", "PT"):
        raise HTTPException(400, "بيانات الفرع أو نوع التعاقد غير صحيحة")
    if data.day_off not in ("Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"):
        raise HTTPException(400, "يوم الإجازة غير صحيح")
    code = (data.employee_code or "").strip() or None
    if code and db.query(Employee).filter(Employee.employee_code == code, Employee.id != employee_id).first():
        raise HTTPException(409, "كود الموظف مستخدم بالفعل")
    for k in ("name", "branch", "shift", "employment_type", "day_off", "active"):
        setattr(e, k, getattr(data, k))
    e.name = data.name.strip()
    e.shift = data.shift.strip()
    e.employee_code = code
    db.commit()
    db.refresh(e)
    return employee_dict(e)

@app.delete("/api/employees/{employee_id}")
def delete_employee(employee_id: int, db: Session = Depends(get_db)):
    e = db.query(Employee).filter(Employee.id == employee_id).first()
    if not e:
        raise HTTPException(404, "الموظف غير موجود")
    e.active = False
    db.commit()
    return {"message": "تم إيقاف الموظف مع الاحتفاظ بسجل حضوره السابق"}

@app.get("/api/roster")
def roster(attendance_date: date = Query(...), branch: Optional[str] = None, db: Session = Depends(get_db)):
    weekday = attendance_date.strftime("%A")
    q = db.query(Employee).filter(Employee.active.is_(True), Employee.day_off != weekday)
    if branch in ("QCD 1", "QCD 2"):
        q = q.filter(Employee.branch == branch)
    employees = q.order_by(Employee.branch, Employee.shift, Employee.name).all()
    ids = [e.id for e in employees]
    existing = {}
    if ids:
        rows = db.query(Attendance).filter(Attendance.attendance_date == attendance_date, Attendance.employee_id.in_(ids)).all()
        existing = {a.employee_id: attendance_dict(a) for a in rows}
    out = []
    for e in employees:
        row = employee_dict(e)
        row["attendance"] = existing.get(e.id)
        out.append(row)
    return {"date": attendance_date.isoformat(), "weekday": weekday, "employees": out}

@app.post("/api/attendance")
def save_attendance(data: AttendanceIn, db: Session = Depends(get_db)):
    if data.status not in ("Present", "Absent", "Late"):
        raise HTTPException(400, "حالة الحضور غير صحيحة")
    e = db.query(Employee).filter(Employee.id == data.employee_id).first()
    if not e:
        raise HTTPException(404, "الموظف غير موجود")
    if not e.active:
        raise HTTPException(400, "الموظف غير نشط")
    if e.day_off == data.attendance_date.strftime("%A"):
        raise HTTPException(400, "هذا الموظف في إجازة في هذا اليوم")
    if data.status == "Present":
        data.fine = 0
        data.reason = ""
    row = db.query(Attendance).filter(Attendance.employee_id == data.employee_id,
                                     Attendance.attendance_date == data.attendance_date).first()
    if row:
        row.status, row.fine, row.reason, row.note = data.status, data.fine, data.reason.strip(), data.note.strip()
        row.updated_at = datetime.utcnow()
    else:
        row = Attendance(employee_id=data.employee_id, attendance_date=data.attendance_date,
                         status=data.status, fine=data.fine, reason=data.reason.strip(), note=data.note.strip())
        db.add(row)
    db.commit()
    db.refresh(row)
    return attendance_dict(row)

@app.get("/api/reports/summary")
def report_summary(start: date, end: date, branch: Optional[str] = None, db: Session = Depends(get_db)):
    if end < start:
        raise HTTPException(400, "تاريخ النهاية يجب ألا يسبق تاريخ البداية")
    q = db.query(Attendance, Employee).join(Employee, Attendance.employee_id == Employee.id).filter(
        Attendance.attendance_date >= start, Attendance.attendance_date <= end)
    if branch in ("QCD 1", "QCD 2"):
        q = q.filter(Employee.branch == branch)
    rows = q.all()
    summary = {"total": len(rows), "present": 0, "absent": 0, "late": 0, "fine_total": 0.0, "by_branch": {}}
    for a, e in rows:
        key = e.branch
        if key not in summary["by_branch"]:
            summary["by_branch"][key] = {"total": 0, "present": 0, "absent": 0, "late": 0, "fine_total": 0.0}
        bucket = summary["by_branch"][key]
        summary["total"] += 0
        bucket["total"] += 1
        if a.status == "Present":
            summary["present"] += 1; bucket["present"] += 1
        elif a.status == "Absent":
            summary["absent"] += 1; bucket["absent"] += 1
        elif a.status == "Late":
            summary["late"] += 1; bucket["late"] += 1
        summary["fine_total"] += a.fine or 0
        bucket["fine_total"] += a.fine or 0
    summary["fine_total"] = round(summary["fine_total"], 2)
    for b in summary["by_branch"].values():
        b["fine_total"] = round(b["fine_total"], 2)
    return {"start": start.isoformat(), "end": end.isoformat(), **summary}

@app.get("/api/reports/performance")
def performance_metrics(db: Session = Depends(get_db)):
    current_month = date.today().replace(day=1)
    rows = db.query(Attendance, Employee).join(Employee, Attendance.employee_id == Employee.id).filter(
        Attendance.attendance_date >= current_month, Employee.active.is_(True)).all()
    
    stats = {}
    for a, e in rows:
        if e.id not in stats:
            stats[e.id] = {"name": e.name, "branch": e.branch, "present": 0, "absent": 0, "late": 0, "fine": 0}
        if a.status == "Present": stats[e.id]["present"] += 1
        elif a.status == "Absent": stats[e.id]["absent"] += 1
        elif a.status == "Late": stats[e.id]["late"] += 1
        stats[e.id]["fine"] += a.fine or 0
        
    ranked = sorted(stats.values(), key=lambda x: (x["absent"], x["late"], -x["present"]))
    top_performers = ranked[:5]
    needs_attention = sorted(stats.values(), key=lambda x: (-x["absent"], -x["late"], -x["fine"]))[:5]
    
    return {"top": top_performers, "attention": needs_attention}

@app.get("/api/reports/export.csv")
def export_csv(start: date, end: date, branch: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(Attendance, Employee).join(Employee, Attendance.employee_id == Employee.id).filter(
        Attendance.attendance_date >= start, Attendance.attendance_date <= end)
    if branch in ("QCD 1", "QCD 2"):
        q = q.filter(Employee.branch == branch)
    rows = q.order_by(Attendance.attendance_date, Employee.branch, Employee.name).all()
    buffer = io.StringIO()
    buffer.write("\ufeff")
    writer = csv.writer(buffer)
    writer.writerow(["Date", "Branch", "Employee Code", "Name", "Type", "Shift", "Day Off", "Status", "Fine (EGP)", "Reason", "Note"])
    for a, e in rows:
        writer.writerow([a.attendance_date.isoformat(), e.branch, e.employee_code or "", e.name, e.employment_type,
                         e.shift, e.day_off, a.status, f"{a.fine:.2f}", a.reason or "", a.note or ""])
    buffer.seek(0)
    filename = f"bankai_attendance_{start}_{end}.csv"
    return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})
