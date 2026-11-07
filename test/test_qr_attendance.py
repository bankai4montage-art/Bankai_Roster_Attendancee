# -*- coding: utf-8 -*-
"""اختبارات حضور QCD 2 بالـ QR: السماح، الغرامة مرة واحدة، تصحيح Absent،
الفرع الغلط، والانصراف بموافقة المشرف.

بيتشارك نفس قاعدة الاختبار مع test_das_requests.py لما الاتنين يتشغلوا مع بعض.
"""
import os
import sys
import tempfile
import time
from datetime import datetime

import pytest

_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _repo)
if "app" not in sys.modules:
    _db = os.path.join(tempfile.mkdtemp(), "test_bankai_qr.db")
    os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")
    os.environ["ADMIN_PASSWORD"] = "admin-test-pass"
    os.environ["SUPERVISOR_PASSWORD"] = "gen-sup-pass"
    os.environ["SUPERVISOR_BRANCH_PASSWORDS"] = "QCD 1=sup-qcd1;QCD 2=sup-qcd2"
    os.environ["DAS_SECRET"] = "das-test-secret"
    os.environ["SECRET_KEY"] = "unit-test-secret-key"

import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SHIFT_DAY = datetime(2026, 11, 7)  # السبت — يوم شيفت ثابت للاختبارات


@pytest.fixture(scope="module")
def client():
    return TestClient(app.app)


@pytest.fixture(scope="module")
def admin(client):
    r = client.post("/api/login", json={"password": app.ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _mk(client, admin, code, *, branch="QCD 2", shift="9 AM - 6 PM",
        day_off="Friday", typ="FT"):
    r = client.post("/api/employees", headers=_auth(admin), json={
        "name": f"موظف {code}", "employee_code": code, "branch": branch,
        "shift": shift, "employment_type": typ, "day_off": day_off,
    })
    assert r.status_code == 200, r.text
    return r.json()


def _set_fine(db, branch="QCD 2", typ="FT", kind="late", value=25):
    key = f"bf|{branch}|{typ}|{kind}"
    row = db.get(app.Setting, key)
    if row is None:
        db.add(app.Setting(key=key, value=str(value)))
    else:
        row.value = str(value)
    db.commit()


def _check(client, code, when, branch="QCD 2", token=None):
    app.qr_now_local = lambda: when
    return client.post("/api/attendance/qr", json={
        "token": token or app.qr_token(branch),
        "employee_code": code,
    })


def _row(employee_id):
    db = app.SessionLocal()
    try:
        return (db.query(app.Attendance)
                .filter(app.Attendance.employee_id == employee_id).first())
    finally:
        db.close()


def test_grace_under_five_is_present(client, admin):
    db = app.SessionLocal()
    _set_fine(db)
    db.close()
    emp = _mk(client, admin, "QR-P1")
    r = _check(client, "QR-P1", SHIFT_DAY.replace(hour=9, minute=4))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "Present" and body["fine"] == 0

    r = _check(client, "QR-P1", SHIFT_DAY.replace(hour=9, minute=20))
    assert r.status_code == 200
    assert r.json()["already"] is True and r.json()["fine"] == 0
    row = _row(emp["id"])
    assert row is not None and row.status == "Present" and float(row.fine or 0) == 0


def test_five_minutes_is_late_and_the_fine_is_applied_once(client, admin):
    db = app.SessionLocal()
    _set_fine(db, value=25)
    db.close()
    emp = _mk(client, admin, "QR-L1")
    r = _check(client, "QR-L1", SHIFT_DAY.replace(hour=9, minute=5))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "Late" and body["fine"] == 25 and body["late_minutes"] == 5

    r = _check(client, "QR-L1", SHIFT_DAY.replace(hour=9, minute=55))
    assert r.status_code == 200
    body = r.json()
    assert body["already"] is True and body["fine"] == 25

    db = app.SessionLocal()
    rows = (db.query(app.Attendance)
            .filter(app.Attendance.employee_id == emp["id"]).all())
    assert len(rows) == 1 and float(rows[0].fine or 0) == 25
    db.close()


def test_absent_is_corrected_with_audit_and_note(client, admin):
    db = app.SessionLocal()
    _set_fine(db, value=25)
    _set_fine(db, kind="absent", value=50)
    db.close()
    emp = _mk(client, admin, "QR-A1")
    db = app.SessionLocal()
    db.add(app.Attendance(employee_id=emp["id"], attendance_date=SHIFT_DAY.date(),
                          status="Absent", fine=50, reason="", note="",
                          recorded_by="المشرف"))
    db.commit()
    db.close()

    r = _check(client, "QR-A1", SHIFT_DAY.replace(hour=9, minute=10))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["corrected"] is True and body["status"] == "Late" and body["fine"] == 25

    db = app.SessionLocal()
    row = (db.query(app.Attendance)
           .filter(app.Attendance.employee_id == emp["id"]).first())
    audit = (db.query(app.AttendanceAudit)
             .filter(app.AttendanceAudit.employee_id == emp["id"]).first())
    assert row.status == "Late" and float(row.fine or 0) == 25
    assert "تصحيح بعد QR" in (row.note or "")
    assert audit is not None
    assert audit.old_status == "Absent" and float(audit.old_fine or 0) == 50
    assert audit.new_status == "Late" and float(audit.new_fine or 0) == 25
    db.close()


def test_wrong_branch_and_expired_token_are_refused(client, admin):
    _mk(client, admin, "QR-W1")
    r = _check(client, "QR-W1", SHIFT_DAY.replace(hour=9, minute=4), branch="QCD 1")
    assert r.status_code == 409

    old = app.qr_token("QCD 2", ts=time.time() - 120)
    r = _check(client, "QR-W1", SHIFT_DAY.replace(hour=9, minute=4), token=old)
    assert r.status_code == 410


def test_branch_supervisor_only_gets_their_own_qr(client):
    r = client.post("/api/login", json={"password": "sup-qcd1"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    r = client.get("/api/admin/qr/attendance", headers=_auth(token),
                   params={"branch": "QCD 2"})
    assert r.status_code == 200, r.text
    assert r.json()["branch"] == "QCD 1"
    assert r.json()["interval"] == 30


def test_roster_directory_exposes_branch_and_attendance_only(client, admin):
    _mk(client, admin, "QR-DIR2")
    r = client.get("/api/das/employee", headers={"X-DAS-Secret": app.DAS_SECRET},
                   params={"employee_code": "QR-DIR2"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["branch"] == "QCD 2" and body["attendance_only"] is True
    assert body["shift"] == "9 AM - 6 PM"


def test_day_off_and_outside_shift_are_refused(client, admin):
    _mk(client, admin, "QR-OFF", day_off="Saturday")
    r = _check(client, "QR-OFF", SHIFT_DAY.replace(hour=9, minute=4))
    assert r.status_code == 409
    assert "إجازتك" in r.json()["detail"]

    _mk(client, admin, "QR-LATE", shift="9 AM - 6 PM")
    r = _check(client, "QR-LATE", SHIFT_DAY.replace(hour=20, minute=0))
    assert r.status_code == 409
    assert "مفيش شيفت" in r.json()["detail"]


def test_checkout_request_waits_for_approval(client, admin):
    emp = _mk(client, admin, "QR-C1")
    r = _check(client, "QR-C1", SHIFT_DAY.replace(hour=9, minute=4))
    assert r.status_code == 200, r.text

    app.qr_now_local = lambda: SHIFT_DAY.replace(hour=18, minute=0)
    headers = {"X-DAS-Secret": app.DAS_SECRET}
    r = client.post("/api/attendance/qr/checkout", headers=headers,
                    json={"employee_code": "QR-C1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "Pending" and body["already"] is False
    req_id = body["request_id"]

    r = client.post("/api/attendance/qr/checkout", headers=headers,
                    json={"employee_code": "QR-C1"})
    assert r.status_code == 200 and r.json()["already"] is True

    r = client.post(f"/api/das/requests/{req_id}/decide", headers=_auth(admin),
                    json={"decision": "approve"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Approved" and r.json()["source"] == "QR"

    db = app.SessionLocal()
    co = db.execute(app.text(
        "SELECT checked_out_at FROM attendance_checkouts WHERE employee_id=:eid"
    ), {"eid": emp["id"]}).first()
    row = (db.query(app.Attendance)
           .filter(app.Attendance.employee_id == emp["id"]).first())
    assert co is not None and co[0]
    assert "انصراف QR" in (row.note or "")
    assert row.status == "Present"  # الانصراف ما بيغيّرش الحضور ولا الغرامة
    db.close()

    r = client.get("/api/attendance/qr/status", headers=headers,
                   params={"employee_code": "QR-C1"})
    assert r.status_code == 200, r.text
    status = r.json()
    assert status["attendance"]["status"] == "Present"
    assert status["checkout"]["status"] == "Approved"
    assert status["checkout"]["checked_out_at"]
