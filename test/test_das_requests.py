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


def _login(client, password):
    r = client.post("/api/login", json={"password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _mk_employee(client, admin, name, code, branch="QCD 1"):
    r = client.post("/api/employees", headers=_auth(admin), json={
        "name": name, "employee_code": code, "branch": branch,
        "shift": "10 PM - 6 AM" if branch != "QCD 1" else "8 AM - 4 PM",
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
def test_checkin_full_flow(client, admin):
    e = _mk_employee(client, admin, "مندوب فلو", "C100", "QCD 1")
    today = app.das_shift_day().isoformat()

    # 1) انصراف قبل أي حضور → مرفوض
    r = _das(client, "check_out", "C100")
    assert r.status_code == 409
    assert "حضور" in r.json()["detail"]

    # 2) حضور → Pending
    r = _das(client, "check_in", "C100")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "Pending"
    assert body["attendance_date"] == today
    assert body["requested_at"]
    assert body["branch"] == "QCD 1"
    rid = body["id"]

    # 3) الطلب يظهر في صندوق الأدمن
    inbox = client.get("/api/das/requests/inbox", headers=_auth(admin)).json()
    ids = [x["id"] for x in inbox["requests"]]
    assert rid in ids

    # 4) انصراف قبل موافقة الحضور → مرفوض (مفيش حضور معتمد)
    r = _das(client, "check_out", "C100")
    assert r.status_code == 409

    # 5) تكرار الحضور → مرفوض
    assert _das(client, "check_in", "C100").status_code == 409

    # 6) موافقة الأدمن → معتمد
    r = _approve(client, admin, rid, decision="approve", note="تمام")
    assert r.status_code == 200 and r.json()["status"] == "Approved"
    assert r.json()["decided_by"] == "أدمن"

    # 7) حضور رسمي في جدول attendance بوقت الطلب (مش وقت الموافقة) وبدون غرامة
    row = _emp_attendance_row(e["id"], today)
    assert row is not None
    assert row.status == "Present"
    assert row.fine == 0
    clock = app.cairo_dt(app.datetime.fromisoformat(body["requested_at"].replace("Z", ""))).strftime("%H:%M")
    assert clock in (row.note or "")

    # 8) حضور تاني في نفس اليوم بعد الاعتماد → مرفوض
    assert _das(client, "check_in", "C100").status_code == 409

    # 9) انصراف → Pending ويرث يوم الحضور
    r = _das(client, "check_out", "C100")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["checkin_id"] == rid
    assert out["attendance_date"] == today

    # 10) تكرار الانصراف → مرفوض
    assert _das(client, "check_out", "C100").status_code == 409

    # 11) موافقة الانصراف → الملاحظة فيها الحضور والانصراف والخاتمة
    r = _approve(client, admin, out["id"])
    assert r.status_code == 200
    row = _emp_attendance_row(e["id"], today)
    assert "انصراف DAS" in (row.note or "")
    assert "حضور DAS" in (row.note or "")
    assert row.status == "Present"

    # 12) حضور تاني في نفس يوم الشيفت بعد الاعتماد → مرفوض (حضور واحد معتمد لكل يوم)
    r = _das(client, "check_in", "C100")
    assert r.status_code == 409


def test_reject_flow(client, admin):
    _mk_employee(client, admin, "مندوب رفض", "C101", "QCD 1")
    r = _das(client, "check_in", "C101")
    rid = r.json()["id"]
    r = _approve(client, admin, rid, decision="reject", note="غلط تقفيل")
    assert r.status_code == 200 and r.json()["status"] == "Rejected"
    # بعد الرفض ينفع طلب حضور جديد
    r = _das(client, "check_in", "C101")
    assert r.status_code == 200


def test_decide_twice_is_conflict(client, admin):
    _mk_employee(client, admin, "مندوب تكرار", "C102", "QCD 1")
    r = _das(client, "check_in", "C102")
    rid = r.json()["id"]
    assert _approve(client, admin, rid).status_code == 200
    r = _approve(client, admin, rid)
    assert r.status_code == 409


# ---------------------------------------------------------------- عزل الفروع والصلاحيات
def test_branch_isolation_in_decisions(client, admin):
    e = _mk_employee(client, admin, "مشرف اختبار", "C200", "QCD 2")
    r = _das(client, "check_in", "C200")
    rid = r.json()["id"]

    sup1 = _login(client, "sup-qcd1")   # مشرف QCD 1
    sup2 = _login(client, "sup-qcd2")   # مشرف QCD 2
    r = _approve(client, sup1, rid)
    assert r.status_code == 403
    r = _approve(client, sup2, rid)
    assert r.status_code == 200
    assert r.json()["decided_by"] == "مشرف QCD 2"

    # صندوق مشرف QCD 2 بيظهر الطلب بتاعه بس
    inbox = client.get("/api/das/requests/inbox", headers=_auth(sup2)).json()
    assert inbox["viewer"]["branch"] == "QCD 2"
    assert all(x["branch"] == "QCD 2" for x in inbox["requests"])


def test_general_supervisor_readonly_when_branch_passwords_set(client, admin):
    """لو كلمات مرور الفرع متحدة، المشرف العام مشاهدة فقط من غير قرارات."""
    _mk_employee(client, admin, "مشرف عام تيست", "C201", "QCD 1")
    r = _das(client, "check_in", "C201")
    rid = r.json()["id"]
    gen = _login(client, "gen-sup-pass")
    inbox = client.get("/api/das/requests/inbox", headers=_auth(gen)).json()
    assert inbox["viewer"]["can_decide"] is False
    r = _approve(client, gen, rid)
    assert r.status_code == 403


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
    e = _mk_employee(client, admin, "شilibift ليلي", "C300", "QCD 1")
    today = app.das_shift_day().isoformat()
    r = _das(client, "check_in", "C300")
    rid = r.json()["id"]

    # بنحاكي إن الحضور كان إمبارح بالليل — بحقن الطلب مباشرة (بعد الموافقة)
    db = app.SessionLocal()
    try:
        row = db.query(app.DasRequest).filter(app.DasRequest.id == rid).first()
        row.attendance_date = app.date.fromisoformat("2026-10-29")
        row.requested_at = app.datetime(2026, 10, 29, 19, 0, 0)
        db.commit()
    finally:
        db.close()

    r = _approve(client, admin, rid)
    assert r.status_code == 200

    # الانصراف (الوقت الحقيقي بعد منتصف الليل) بيرث يوم الحضور 29-10 مش يوم النهاردة
    r = _das(client, "check_out", "C300")
    assert r.status_code == 200, r.text
    assert r.json()["attendance_date"] == "2026-10-29"

    # والتسجيل الرسمي اتحط على نفس اليوم
    _approve(client, admin, r.json()["id"])
    row = _emp_attendance_row(e["id"], app.date.fromisoformat("2026-10-29"))
    assert row is not None and row.status == "Present"


# ---------------------------------------------------------------- الحفاظ على تسجيلات المشرف
def test_approval_never_overwrites_supervisor_record(client, admin):
    """لو المشرف سجل الحضور قبل الموافقة، موافقة DAS بتضيف توثيق بس وتحافظ على الحالة والغرامة."""
    e = _mk_employee(client, admin, "حافظ", "C400", "QCD 1")
    today = app.das_shift_day()
    db = app.SessionLocal()
    try:
        db.add(app.Attendance(employee_id=e["id"], attendance_date=today, status="Absent",
                              fine=50, reason="غياب بدون إذن", updated_at=app.utcnow()))
        db.commit()
    finally:
        db.close()

    r = _das(client, "check_in", "C400")
    rid = r.json()["id"]
    today_iso = today.isoformat()
    r = _approve(client, admin, rid)
    assert r.status_code == 200
    row = _emp_attendance_row(e["id"], today)
    assert row.status == "Absent"          # الحالة ما اتلمستش
    assert row.fine == 50                  # الغرامة ما اتلمستش
    assert "حضور DAS" in (row.note or "")  # التوثيق اتضاف في الملاحظة


# ---------------------------------------------------------------- مزامنة DAS
def test_das_sync_endpoint_filters(client, admin):
    _mk_employee(client, admin, "زامن", "C500", "QCD 1")
    r = _das(client, "check_in", "C500")
    rid = r.json()["id"]

    # بدون تصفية: الطلب موجود
    r = client.get("/api/das/requests?employee_code=C500", headers=DAS_HDR)
    assert r.status_code == 200
    assert any(x["id"] == rid for x in r.json()["requests"])

    # بتوقيت مستقبلي: المطلوب لسه Pending يرجع؛ اللي اتقرر بعد التوقيت بس يترجع
    r = client.get("/api/das/requests?employee_code=C500&updated_since=2030-01-01T00:00:00", headers=DAS_HDR)
    assert r.status_code == 200
    data = r.json()["requests"]
    assert any(x["id"] == rid for x in data)      # لسه Pending → لازم يرجع

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


def test_named_supervisor_decides_das_with_name(client, admin):
    e = _mk_employee(client, admin, "بنانا", "N100", "QCD 2")
    r = _das(client, "check_in", "N100")
    rid = r.json()["id"]
    s2 = _login(client, "karim-pass")
    r = _approve(client, s2, rid)
    assert r.status_code == 200, r.text
    assert r.json()["decided_by"] == "مشرف Karim — QCD 2"
    row = _emp_attendance_row(e["id"], app.das_shift_day().isoformat())
    assert row is not None and row.recorded_by == "مشرف Karim — QCD 2"
    # مشرف فرع تاني → 403
    s1 = _login(client, "elsuid-pass")
    _mk_employee(client, admin, "بجدي", "N101", "QCD 1")
    r2 = _das(client, "check_in", "N101")
    r = _approve(client, s1, r2.json()["id"]) if False else _approve(client, s2, r2.json()["id"])
    assert r.status_code == 403
    # مشرف فرع بصوره (بلا اسم) لسه بيمشي مع القاعدة القديمة — نفس الطلب اللي استنى (N101 = QCD 1)
    legacy = _login(client, "sup-qcd1")
    r2b2 = _approve(client, legacy, r2.json()["id"])
    assert r2b2.status_code == 200
    assert r2b2.json()["decided_by"] == "مشرف QCD 1"


def test_general_supervisor_readonly_when_any_branch_accounts(client, admin):
    _mk_employee(client, admin, "بنوب", "N102", "QCD 1")
    r = _das(client, "check_in", "N102")
    rid = r.json()["id"]
    gen = _login(client, "gen-sup-pass")
    r = _approve(client, gen, rid)
    assert r.status_code == 403


# ----------------------------------------------------------------
@pytest.fixture(scope="module")
def admin(client):
    return _login(client, "admin-test-pass")
