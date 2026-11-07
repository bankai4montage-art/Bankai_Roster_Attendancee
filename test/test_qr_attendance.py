# -*- coding: utf-8 -*-
"""اختبارات حضور QCD 2 بالـ QR: السماح، الغرامة مرة واحدة، تصحيح Absent،
الفرع الغلط، والانصراف بموافقة المشرف.

بيتشارك نفس قاعدة الاختبار مع test_das_requests.py لما الاتنين يتشغلوا مع بعض.
"""
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

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


def test_absent_status_still_allows_the_correction_scan(client, admin):
    emp = _mk(client, admin, "QR-A2")
    db = app.SessionLocal()
    db.add(app.Attendance(employee_id=emp["id"], attendance_date=SHIFT_DAY.date(),
                          status="Absent", fine=50, reason="", note="",
                          recorded_by="المشرف"))
    db.commit()
    db.close()
    app.qr_now_local = lambda: SHIFT_DAY.replace(hour=9, minute=4)
    r = client.get("/api/attendance/qr/status",
                   headers={"X-DAS-Secret": app.DAS_SECRET},
                   params={"employee_code": "QR-A2"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["attendance"]["status"] == "Absent"
    assert body["can_check_in"] is True


def test_night_shift_status_keeps_the_shift_day(client, admin):
    emp = _mk(client, admin, "QR-N1", shift="10 PM - 6 AM")
    db = app.SessionLocal()
    db.add(app.Attendance(employee_id=emp["id"], attendance_date=SHIFT_DAY.date() - timedelta(days=1),
                          status="Present", fine=0, reason="", note="حضور QR 22:05",
                          recorded_by="QR"))
    db.commit()
    db.close()
    app.qr_now_local = lambda: SHIFT_DAY.replace(hour=7, minute=0)
    r = client.get("/api/attendance/qr/status",
                   headers={"X-DAS-Secret": app.DAS_SECRET},
                   params={"employee_code": "QR-N1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["date"] == (SHIFT_DAY.date() - timedelta(days=1)).isoformat()
    assert body["attendance"]["status"] == "Present"


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


def test_weekly_history_lists_days_and_summary(client, admin):
    emp = _mk(client, admin, "QR-H1", day_off="Friday")
    db = app.SessionLocal()
    db.add_all([
        app.Attendance(employee_id=emp["id"], attendance_date=SHIFT_DAY.date() - timedelta(days=6),
                       status="Present", fine=0, reason="", note="حضور QR 09:02",
                       recorded_by="QR"),
        app.Attendance(employee_id=emp["id"], attendance_date=SHIFT_DAY.date() - timedelta(days=5),
                       status="Late", fine=25, reason="", note="حضور QR 09:09 · متأخر 9 دقيقة",
                       recorded_by="QR"),
        app.Attendance(employee_id=emp["id"], attendance_date=SHIFT_DAY.date() - timedelta(days=4),
                       status="Absent", fine=50, reason="", note="", recorded_by="المشرف"),
        app.Attendance(employee_id=emp["id"], attendance_date=SHIFT_DAY.date() - timedelta(days=3),
                       status="Excused", fine=0, reason="مرض", note="", recorded_by="المشرف"),
    ])
    db.commit()
    db.close()
    app.qr_now_local = lambda: SHIFT_DAY.replace(hour=9, minute=4)
    r = client.get("/api/attendance/qr/history",
                   headers={"X-DAS-Secret": app.DAS_SECRET},
                   params={"employee_code": "QR-H1", "weeks": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["weeks"]) >= 1
    week = body["weeks"][0]
    assert week["week_start"] == "2026-11-01"
    assert len(week["days"]) == 7
    assert week["summary"]["present"] == 1
    assert week["summary"]["late"] == 1
    assert week["summary"]["absent"] == 1
    assert week["summary"]["excused"] == 1
    assert week["summary"]["off"] == 1
    assert week["summary"]["fine"] == 75
    late_day = next(d for d in week["days"] if d["status"] == "Late")
    assert late_day["late_minutes"] == 9 and late_day["fine"] == 25
    off_day = next(d for d in week["days"] if d["status"] == "OFF")
    assert off_day["weekday"] == "Friday"


def test_any_time_check_in_and_the_fine_preview(client, admin):
    _mk(client, admin, "QR-OFF", day_off="Saturday")
    r = _check(client, "QR-OFF", SHIFT_DAY.replace(hour=9, minute=4))
    assert r.status_code == 409
    assert "إجازتك" in r.json()["detail"]

    db = app.SessionLocal()
    _set_fine(db, value=25)
    db.close()
    _mk(client, admin, "QR-LATE", shift="9 AM - 6 PM")

    app.qr_now_local = lambda: SHIFT_DAY.replace(hour=20, minute=0)
    r = client.get("/api/attendance/qr/status",
                   headers={"X-DAS-Secret": app.DAS_SECRET},
                   params={"employee_code": "QR-LATE"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["can_check_in"] is True
    assert body["preview"]["status"] == "Late"
    assert body["preview"]["fine"] == 25
    assert body["preview"]["late_minutes"] == 660

    r = _check(client, "QR-LATE", SHIFT_DAY.replace(hour=20, minute=0))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Late" and r.json()["fine"] == 25

    _mk(client, admin, "QR-EARLY", shift="9 AM - 6 PM")
    r = _check(client, "QR-EARLY", SHIFT_DAY.replace(hour=7, minute=0))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Present" and r.json()["fine"] == 0


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
