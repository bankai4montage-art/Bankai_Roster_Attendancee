# -*- coding: utf-8 -*-
"""اختبارات صفحة «الشريك»: إضافة المناديب، عزل الفروع، والعدّاد اليومي.

تشغيل: python -m pytest test/test_partners.py -v
"""
import os
import sys
import tempfile

import pytest

_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _repo)
if "app" not in sys.modules:
    _db = os.path.join(tempfile.mkdtemp(), "test_bankai_partners.db")
    os.environ["DATABASE_URL"] = "sqlite:///" + _db.replace("\\", "/")
    os.environ["ADMIN_PASSWORD"] = "admin-test-pass"
    os.environ["SUPERVISOR_PASSWORD"] = "gen-sup-pass"
    os.environ["SUPERVISOR_BRANCH_PASSWORDS"] = "QCD 1=sup-qcd1;QCD 2=sup-qcd2"
    os.environ["SECRET_KEY"] = "partners-test-secret-key"

import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

WEEK = "2026-11-08"


@pytest.fixture(scope="module")
def client():
    return TestClient(app.app)


@pytest.fixture(scope="module")
def admin(client):
    r = client.post("/api/login", json={"password": "admin-test-pass"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _mk(client, token, name, code, branch="QCD 1", active=True):
    r = client.post("/api/employees", headers=_auth(token), json={
        "name": name, "employee_code": code, "branch": branch,
        "shift": "8 AM - 4 PM", "employment_type": "FT", "day_off": "Friday",
        "active": active,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _add(client, token, ids, week=WEEK):
    return client.post("/api/partners", headers=_auth(token),
                       json={"week": week, "employee_ids": ids})


def test_empty_week_and_days(client, admin):
    r = client.get(f"/api/partners?week={WEEK}", headers=_auth(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["drivers"] == []
    assert body["total"] == 0
    assert len(body["days"]) == 7
    assert body["days"][0]["date"] == WEEK
    assert body["days"][-1]["date"] == "2026-11-14"
    assert set(body["totals"].values()) == {0}


def test_add_is_idempotent_and_keeps_the_week(client, admin):
    a = _mk(client, admin, "أحمد الشريك", "P-1001", "QCD 1")
    b = _mk(client, admin, "محمود الشريك", "P-1002", "QCD 2")
    r = _add(client, admin, [a, b])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 2
    assert len(body["payload"]["drivers"]) == 2
    # same week, same people: no duplicates
    r2 = _add(client, admin, [a, b])
    assert r2.status_code == 200, r2.text
    assert r2.json()["added"] == 0
    assert len(r2.json()["payload"]["drivers"]) == 2
    # another week is independent
    r3 = client.get("/api/partners?week=2026-11-15", headers=_auth(admin))
    assert r3.json()["drivers"] == []
    # keep ids for later tests
    app._partner_test_ids = {"a": a, "b": b}


def test_counter_goes_up_and_down(client, admin):
    body = client.get(f"/api/partners?week={WEEK}", headers=_auth(admin)).json()
    driver = next(d for d in body["drivers"] if d["employee_id"] == app._partner_test_ids["a"])
    pid = driver["id"]
    r = client.put(f"/api/partners/{pid}/count", headers=_auth(admin),
                   json={"date": "2026-11-10", "count": 3})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 3
    assert r.json()["driver_total"] == 3
    assert r.json()["day_total"] == 3
    assert r.json()["week_total"] == 3
    # decrement to zero removes the stored row but keeps the day key at zero
    r2 = client.put(f"/api/partners/{pid}/count", headers=_auth(admin),
                    json={"date": "2026-11-10", "count": 0})
    assert r2.status_code == 200, r2.text
    assert r2.json()["count"] == 0
    body2 = client.get(f"/api/partners?week={WEEK}", headers=_auth(admin)).json()
    assert body2["total"] == 0
    assert body2["totals"]["2026-11-10"] == 0
    assert body2["drivers"][0]["counts"]["2026-11-10"] == 0


def test_counter_rejects_dates_outside_the_week(client, admin):
    body = client.get(f"/api/partners?week={WEEK}", headers=_auth(admin)).json()
    pid = body["drivers"][0]["id"]
    r = client.put(f"/api/partners/{pid}/count", headers=_auth(admin),
                   json={"date": "2026-11-15", "count": 1})
    assert r.status_code == 400
    r2 = client.put(f"/api/partners/{pid}/count", headers=_auth(admin),
                    json={"date": "2026-11-10", "count": -1})
    assert r2.status_code == 422


def test_branch_supervisor_scope(client):
    sup1 = client.post("/api/login", json={"password": "sup-qcd1"}).json()["token"]
    a = app._partner_test_ids["a"]
    b = app._partner_test_ids["b"]
    # sees only QCD 1 partners
    body = client.get(f"/api/partners?week={WEEK}", headers=_auth(sup1)).json()
    assert all(d["branch"] == "QCD 1" for d in body["drivers"])
    assert {d["employee_id"] for d in body["drivers"]} == {a}
    # cannot add a QCD 2 employee
    r = _add(client, sup1, [b])
    assert r.status_code == 403
    # can add his branch
    r2 = _add(client, sup1, [a])
    assert r2.status_code == 200
    assert r2.json()["added"] == 0


def test_remove_driver_and_counts(client, admin):
    body = client.get(f"/api/partners?week={WEEK}", headers=_auth(admin)).json()
    driver = next(d for d in body["drivers"] if d["employee_id"] == app._partner_test_ids["a"])
    pid = driver["id"]
    client.put(f"/api/partners/{pid}/count", headers=_auth(admin),
               json={"date": "2026-11-11", "count": 2})
    r = client.delete(f"/api/partners/{pid}", headers=_auth(admin))
    assert r.status_code == 200, r.text
    assert r.json()["payload"]["total"] == 0
    assert all(d["id"] != pid for d in r.json()["payload"]["drivers"])
    db = app.SessionLocal()
    try:
        assert db.query(app.PartnerCount).filter(app.PartnerCount.partner_id == pid).count() == 0
    finally:
        db.close()


def test_validation_and_auth(client, admin):
    assert client.get(f"/api/partners?week={WEEK}").status_code in (401, 403)
    assert _add(client, admin, []).status_code == 400
    assert _add(client, admin, [999999]).status_code == 404
    sup1 = client.post("/api/login", json={"password": "sup-qcd1"}).json()["token"]
    # supervisor cannot edit another branch's driver
    other = next(d for d in client.get(f"/api/partners?week={WEEK}", headers=_auth(admin)).json()["drivers"]
                 if d["branch"] == "QCD 2")
    r = client.put(f"/api/partners/{other['id']}/count", headers=_auth(sup1),
                   json={"date": "2026-11-10", "count": 1})
    assert r.status_code == 403
