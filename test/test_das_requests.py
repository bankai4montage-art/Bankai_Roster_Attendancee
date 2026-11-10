# -*- coding: utf-8 -*-
"""اختبارات تكامل: واجهات طلبات الحضور من DAS في الروستر.

بتغطي: إنشاء الطلبات بالمفتاح السري، منع التكرارات، الشفّ الليلي، شاشة الربط
بالكود حصرًا، عزل الفروع في القرارات، صلاحيات المشرف العام، والموافقة بتسجّل
حضور رسمي من غير ما تغيّر تسجيلات المشرف أو حسابات التقارير.

تشغيل:  python -m pytest test/test_das_requests.py -v
"""
import os
import sys
import tempfile

import pytest

_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _repo)
_db = os.path.join(tempfile.mkdtemp(), "test_bankai_das.db")
os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")
os.environ["ADMIN_PASSWORD"] = "admin-test-pass"
os.environ["SUPERVISOR_PASSWORD"] = "gen-sup-pass"
os.environ["SUPERVISOR_BRANCH_PASSWORDS"] = "QCD 1=sup-qcd1;QCD 2=sup-qcd2"
os.environ["DAS_SECRET"] = "das-test-secret"
os.environ["SECRET_KEY"] = "unit-test-secret-key"
os.environ["SUPERVISOR_ACCOUNTS"] = "Karim:karim-pass:QCD 2;Elsuid:elsuid-pass:QCD 1"

import app  # noqa: E402  (الاستيراد بعد تجهيز البيئة عشان يشغّل على داتابيز الاختبار)
from fastapi.testclient import TestClient  # noqa: E402

SECRET = os.environ["DAS_SECRET"]
DAS_HDR = {"X-DAS-Secret": SECRET}


@pytest.fixture(scope="module")
def client():
    c = TestClient(app.app)
    return c


@pytest.fixture(autouse=True)
def _restore_qr_clock():
    original = app.qr_now_local
    yield
    app.qr_now_local = original


def _login(client, password):
    r = client.post("/api/login", json={"password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _mk_employee(client, admin, name, code, branch="QCD 1", shift=None, week=None):
    url = "/api/employees" + (f"?week={week}" if week else "")
    r = client.post(url, headers=_auth(admin), json={
        "name": name, "employee_code": code, "branch": branch,
        "shift": shift or ("10 PM - 6 AM" if branch != "QCD 1" else "8 AM - 4 PM"),
        "employment_type": "FT", "day_off": "Friday",
    })
    assert r.status_code == 200, r.text
    return r.json()


def _das(client, action, code, name=""):
    return client.post("/api/das/requests", headers=DAS_HDR, json={
        "action": action, "employee_code": code,
        "courier_name": name, "courier_local_at": "09:15",
    })


def _approve(client, token, request_id, decision="approve", note=""):
    return client.post(f"/api/das/requests/{request_id}/decide", headers=_auth(token),
                       json={"decision": decision, "note": note})


def _emp_attendance_row(employee_id, day):
    db = app.SessionLocal()
    try:
        return (db.query(app.Attendance)
                .filter(app.Attendance.employee_id == employee_id,
                        app.Attendance.attendance_date == day).first())
    finally:
        db.close()


# ---------------------------------------------------------------- الأمان
def test_rejects_wrong_secret(client):
    r = client.post("/api/das/requests", headers={"X-DAS-Secret": "nope"},
                    json={"action": "check_in", "employee_code": "X1"})
    assert r.status_code == 403
    r = client.post("/api/das/requests", json={"action": "check_in", "employee_code": "X1"})
    assert r.status_code == 403


def test_unknown_action(client):
    r = client.post("/api/das/requests", headers=DAS_HDR,
                    json={"action": "teleport", "employee_code": "X1"})
    assert r.status_code == 400


# ---------------------------------------------------------------- الربط بالكود حصرًا
def test_unmapped_code_is_rejected(client, admin):
    """كود مسجل في DAS بس مش مربوط في الروستر → مرفوض برسالة واضحة (مش ربط بالاسم)."""
    r = _das(client, "check_in", "DAS-UNMAPPED")
    assert r.status_code == 404
    assert "مش مربوط" in r.json()["detail"]


# ---------------------------------------------------------------- دورة الموافقة
def test_checkin_and_checkout_are_instant(client, admin):
    e = _mk_employee(client, admin, "مندوب فلو", "C100", "QCD 1")
    today_d = app.das_shift_day()
    today = today_d.isoformat()
    app.qr_now_local = lambda: app.datetime(today_d.year, today_d.month, today_d.day, 8, 0)

    # 1) انصراف قبل أي حضور → مرفوض
    r = _das(client, "check_out", "C100")
    assert r.status_code == 409
    assert "حضور" in r.json()["detail"]

    # 2) حضور → Approved فورًا (من غير Pending)
    r = _das(client, "check_in", "C100")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "Approved"
    assert body["attendance_date"] == today
    assert body["requested_at"]
    assert body["branch"] == "QCD 1"
    assert "حضور" in body["message"]
    rid = body["id"]

    # 3) الحضور اتسجل رسمي في attendance
    row = _emp_attendance_row(e["id"], today)
    assert row is not None and row.status == "Present" and float(row.fine or 0) == 0
    assert "حضور DAS" in (row.note or "")

    # 4) مفيش Pending في صندوق المشرف — القسم ده اتشال
    inbox = client.get("/api/das/requests/inbox", headers=_auth(admin)).json()
    assert all(x["id"] != rid or x["status"] == "Approved" for x in inbox["requests"])

    # 5) تكرار الحضور قبل الانصراف → already بنفس السجل
    r = _das(client, "check_in", "C100")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Approved" and r.json()["already"] is True
    assert r.json()["id"] == rid

    # 6) انصراف → Approved فورًا وبياخد يوم الحضور
    r = _das(client, "check_out", "C100")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "Approved"
    assert out["checkin_id"] == rid
    assert out["attendance_date"] == today
    assert out["time"]

    db = app.SessionLocal()
    try:
        co = db.execute(app.text(
            "SELECT checked_out_at, worked_minutes, early_minutes FROM attendance_checkouts "
            "WHERE employee_id = :eid AND checkout_date = :day"
        ), {"eid": e["id"], "day": today}).first()
    finally:
        db.close()
    assert co is not None and co[0]

    # 7) تكرار الانصراف → already
    r = _das(client, "check_out", "C100")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Approved" and r.json()["already"] is True

    # 8) حضور تاني بعد ما الدورة كملت → مرفوض
    r = _das(client, "check_in", "C100")
    assert r.status_code == 409


def test_early_checkout_records_worked_time_and_alerts_supervisor(client, admin):
    e = _mk_employee(client, admin, "مندوب بدري", "C110", "QCD 1")
    d = app.das_shift_day()
    app.qr_now_local = lambda: app.datetime(d.year, d.month, d.day, 9, 4)
    r = _das(client, "check_in", "C110")
    assert r.status_code == 200, r.text

    # الشيفت 8 ص - 4 م: انصراف 3 م يعني ساعة قبل النهاية
    app.qr_now_local = lambda: app.datetime(d.year, d.month, d.day, 15, 0)
    r = _das(client, "check_out", "C110")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "Approved"
    assert out["early"] is True and out["early_minutes"] == 60
    assert out["worked_minutes"] == 356
    assert "قبل نهاية الشيفت" in out["message"]

    # المشرف بياخد حدث فيه الإنذار والوقت
    events = app.EVENTS.snapshot(0)["events"]
    ev = next(x for x in reversed(events)
              if x.get("kind") == "attendance" and x.get("action") == "check_out"
              and x.get("names") == [e["name"]])
    assert ev["early"] is True and ev["early_minutes"] == 60
    assert ev["duration_min"] == 356 and ev["time"] == "15:00"


def test_checkout_after_shift_end_is_not_early(client, admin):
    emp = _mk_employee(client, admin, "مندوب منتظم", "C120", "QCD 1")
    d = app.das_shift_day()
    app.qr_now_local = lambda: app.datetime(d.year, d.month, d.day, 8, 0)
    assert _das(client, "check_in", "C120").status_code == 200
    app.qr_now_local = lambda: app.datetime(d.year, d.month, d.day, 16, 0)
    r = _das(client, "check_out", "C120")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["early"] is False and out["early_minutes"] == 0
    assert out["worked_minutes"] == 480


def test_general_supervisor_bounds_when_no_branch_passwords():
    saved = dict(app.BRANCH_SUPERVISOR_PASSWORDS)
    saved_acc = dict(app.BRANCH_SUPERVISOR_ACCOUNTS)
    try:
        app.BRANCH_SUPERVISOR_PASSWORDS = {}
        app.BRANCH_SUPERVISOR_ACCOUNTS = {}
        assert app._das_decision_allowed("supervisor", "QCD 1") is True
        assert app._das_decision_allowed("supervisor", "QCD 2") is True
    finally:
        app.BRANCH_SUPERVISOR_PASSWORDS = saved
        app.BRANCH_SUPERVISOR_ACCOUNTS = saved_acc


# ---------------------------------------------------------------- الشيفت الليلي (لفّ منتصف الليل)
def test_night_shift_checkout_inherits_shift_day(client, admin):
    """انصراف بعد منتصف الليل بيفضل على يوم الحضور — مش يوم القاهرة الجديد."""
    day = app.date.fromisoformat("2026-10-29")
    week = app.week_start_of(day).isoformat()
    e = _mk_employee(client, admin, "شيفت ليلي", "C300", "QCD 2",
                    shift="10 PM - 6 AM", week=week)
    app.qr_now_local = lambda: app.datetime(2026, 10, 29, 22, 0)
    r = _das(client, "check_in", "C300")
    assert r.status_code == 200, r.text
    assert r.json()["attendance_date"] == day.isoformat()

    # 5 الصبح: لسه جوه الشيفت اللي بدأ 10 بالليل، ويومه 29-10
    app.qr_now_local = lambda: app.datetime(2026, 10, 30, 5, 0)
    r = _das(client, "check_out", "C300")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["attendance_date"] == day.isoformat()
    assert out["worked_minutes"] == 420
    assert out["early"] is True and out["early_minutes"] == 60

    row = _emp_attendance_row(e["id"], day)
    assert row is not None and row.status == "Present"


# ---------------------------------------------------------------- تصحيح الحضور
def test_auto_checkin_corrects_an_absent_record(client, admin):
    """لو الروستر سجّل غياب والمندوب عمل حضور، الحضور يتصحح فورًا والغرامة تتصفّر."""
    e = _mk_employee(client, admin, "حافظ", "C400", "QCD 1")
    day = app.das_shift_day()
    app.qr_now_local = lambda: app.datetime(day.year, day.month, day.day, 8, 0)
    db = app.SessionLocal()
    try:
        db.add(app.Attendance(employee_id=e["id"], attendance_date=day, status="Absent",
                              fine=50, reason="غياب بدون إذن", updated_at=app.utcnow()))
        db.commit()
    finally:
        db.close()

    r = _das(client, "check_in", "C400")
    assert r.status_code == 200, r.text
    row = _emp_attendance_row(e["id"], day)
    assert row.status in ("Present", "Late")
    assert float(row.fine or 0) == 0          # غرامة الغياب اتصفّرت
    assert "تصحيح بعد DAS" in (row.note or "")


# ---------------------------------------------------------------- مزامنة DAS
def test_das_sync_endpoint_filters(client, admin):
    _mk_employee(client, admin, "زامن", "C500", "QCD 1")
    r = _das(client, "check_in", "C500")
    rid = r.json()["id"]

    # بدون تصفية: الطلب موجود
    r = client.get("/api/das/requests?employee_code=C500", headers=DAS_HDR)
    assert r.status_code == 200
    assert any(x["id"] == rid for x in r.json()["requests"])

    # بتوقيت مستقبلي: مفيش طلبات جديدة → مفيش نتايج
    r = client.get("/api/das/requests?employee_code=C500&updated_since=2030-01-01T00:00:00", headers=DAS_HDR)
    assert r.status_code == 200
    assert not any(x["id"] == rid for x in r.json()["requests"])

    # من بداية الزمن: الطلب المعتمد يرجع للمزامنة
    r = client.get("/api/das/requests?employee_code=C500&updated_since=2000-01-01T00:00:00", headers=DAS_HDR)
    assert r.status_code == 200
    assert any(x["id"] == rid for x in r.json()["requests"])

    # كود مش موجود
    r = client.get("/api/das/requests?employee_code=GHOST", headers=DAS_HDR)
    assert r.json()["requests"] == []


def test_wrong_token_cannot_decide(client, admin):
    _mk_employee(client, admin, "تيمون", "C600", "QCD 1")
    r = _das(client, "check_in", "C600")
    rid = r.json()["id"]
    r = client.post(f"/api/das/requests/{rid}/decide", headers=_auth("bad.token.here"),
                    json={"decision": "approve", "note": ""})
    assert r.status_code == 401


# ---------------------------------------------------------------- الحسابات بالاسم
def test_named_supervisor_login_and_scope(client, admin):
    r = client.post("/api/login", json={"password": "karim-pass"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "supervisor:Karim:QCD 2"
    me = client.get("/api/me", headers=_auth(body["token"])).json()
    assert me["branch"] == "QCD 2" and me["name"] == "Karim"
    emps = client.get("/api/employees?active_only=false", headers=_auth(body["token"])).json()
    assert emps and all(e["branch"] == "QCD 2" for e in emps)


def test_named_supervisor_attendance_scope_and_attribution(client, admin):
    s = _login(client, "elsuid-pass")   # مشرف QCD 1
    emps = client.get("/api/employees?active_only=false", headers=_auth(s)).json()
    assert all(e["branch"] == "QCD 1" for e in emps)
    e1 = emps[0]
    day = app.today_local()
    while day.strftime("%A") == "Friday":
        day = day + app.timedelta(days=1)
    r = client.post("/api/attendance", headers=_auth(s), json={
        "employee_id": e1["id"], "attendance_date": day.isoformat(), "status": "Present"})
    assert r.status_code == 200, r.text
    assert r.json()["recorded_by"] == "مشرف Elsuid — QCD 1"

    # موظف من فرع تاني → مرفوض صريح (مش بيتسجّل غياب غلط)
    other = [e for e in client.get("/api/employees?active_only=false", headers=_auth(admin)).json()
             if e["branch"] == "QCD 2"][0]
    r = client.post("/api/attendance", headers=_auth(s), json={
        "employee_id": other["id"], "attendance_date": day.isoformat(), "status": "Absent"})
    assert r.status_code == 400
    assert "مش في نطاقك" in r.json()["detail"]


def test_named_supervisor_fines_attribution(client, admin):
    s2 = _login(client, "karim-pass")   # مشرف QCD 2
    emps2 = client.get("/api/employees?active_only=false", headers=_auth(s2)).json()
    emp = emps2[0]
    r = client.post("/api/fines/manual", headers=_auth(s2), json={
        "employee_id": emp["id"], "fine_date": app.today_local().isoformat(),
        "amount": 25, "reason": "تأخير تكرر"})
    assert r.status_code == 200, r.text
    assert r.json()["created_by"] == "مشرف Karim — QCD 2"
    month_fines = client.get("/api/fines", headers=_auth(s2)).json()
    assert all(x["branch"] == "QCD 2" for x in month_fines["by_employee"])

    # غرامة على موظف فرع تاني → ممنوع
    a_emp = [e for e in client.get("/api/employees?active_only=false", headers=_auth(admin)).json()
             if e["branch"] == "QCD 1"][0]
    r = client.post("/api/fines/manual", headers=_auth(s2), json={
        "employee_id": a_emp["id"], "fine_date": app.today_local().isoformat(),
        "amount": 25, "reason": "x"})
    assert r.status_code == 403


@pytest.fixture(scope="module")
def admin(client):
    return _login(client, "admin-test-pass")
