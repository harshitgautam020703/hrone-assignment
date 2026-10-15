"""
Integration tests for the full attendance lifecycle using TestClient + a real
(or test) MongoDB instance.

Set TEST_MONGO_URI / TEST_MONGO_DB to point at a throwaway test DB.
If not set, uses mongodb://localhost:27017 / attendance_test_db.

Run:  pytest tests/test_integration.py -v
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

# Override the DB before importing app
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGO_DB", "attendance_test_db")

from app.main import app, get_db, create_indexes

UTC = timezone.utc


@pytest.fixture(autouse=True)
def clean_db():
    """Drop test collections before and after each test."""
    db = get_db()
    db.employees.drop()
    db.attendance_logs.drop()
    create_indexes(db)
    yield
    db.employees.drop()
    db.attendance_logs.drop()


@pytest.fixture
def client():
    return TestClient(app)


def _ms(year, month, day, hour, minute, second=0) -> int:
    """Create epoch ms from IST wall clock (UTC+5:30)."""
    from datetime import timedelta, timezone
    IST = timezone(timedelta(hours=5, minutes=30))
    dt = datetime(year, month, day, hour, minute, second, tzinfo=IST)
    return int(dt.timestamp() * 1000)


def _create_emp(client, emp_code="EMP0001", shift_start="09:30", shift_end="18:30"):
    resp = client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Test User",
        "email": f"{emp_code.lower()}@example.com",
        "department": "Engineering",
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": "2026-01-01",
    })
    assert resp.status_code == 201, resp.json()
    return resp.json()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
class TestEmployees:
    def test_create_employee(self, client):
        r = client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-05",
        })
        assert r.status_code == 201
        data = r.json()
        assert data["emp_code"] == "EMP0001"
        assert isinstance(data["created_at"], int)
        assert data["created_at"] > 100_000_000_000  # epoch ms range

    def test_duplicate_emp_code_is_409(self, client):
        _create_emp(client)
        r = client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Duplicate",
            "email": "dup@example.com",
            "department": "HR",
            "joined_on": "2026-01-01",
        })
        assert r.status_code == 409

    def test_list_employees_pagination(self, client):
        for i in range(1, 6):
            _create_emp(client, f"EMP000{i}")
        r = client.get("/employees?page=1&page_size=2")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

    def test_list_employees_wrong_page_start(self, client):
        """Page 1 should return first items (bug in original: used page*size)."""
        for i in range(1, 4):
            _create_emp(client, f"EMP000{i}")
        r = client.get("/employees?page=1&page_size=10")
        assert len(r.json()["items"]) == 3  # all 3 should appear on page 1

    def test_shift_start_equals_end_is_422(self, client):
        r = client.post("/employees", json={
            "emp_code": "EMP0099",
            "name": "Bad Shift",
            "email": "bad@example.com",
            "department": "HR",
            "shift_start": "09:30",
            "shift_end": "09:30",
            "joined_on": "2026-01-01",
        })
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Punch-in
# ---------------------------------------------------------------------------
class TestPunchIn:
    def test_punch_in_returns_record(self, client):
        _create_emp(client)
        punch_ms = _ms(2026, 7, 6, 9, 28)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": punch_ms,
        })
        assert r.status_code == 201
        data = r.json()
        assert data["emp_code"] == "EMP0001"
        assert data["date"] == "2026-07-06"
        assert data["late_minutes"] == 0
        assert data["punch_out"] is None
        assert data["history"] == []

    def test_late_minutes_computed(self, client):
        _create_emp(client)
        # 10:15:59 IST → 45 min late
        punch_ms = _ms(2026, 7, 6, 10, 15, 59)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_ms})
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 45

    def test_grace_boundary_exactly_10min(self, client):
        _create_emp(client)
        # 09:40:00 exactly → not late
        punch_ms = _ms(2026, 7, 6, 9, 40, 0)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_ms})
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 0

    def test_grace_boundary_one_second_past(self, client):
        _create_emp(client)
        # 09:40:01 → 10 min late
        punch_ms = _ms(2026, 7, 6, 9, 40, 1)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_ms})
        assert r.status_code == 201
        assert r.json()["late_minutes"] == 10

    def test_duplicate_punch_in_is_409(self, client):
        _create_emp(client)
        punch_ms = _ms(2026, 7, 6, 9, 0)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_ms})
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_ms})
        assert r.status_code == 409

    def test_unknown_employee_is_404(self, client):
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
        assert r.status_code == 404

    def test_seconds_epoch_rejected(self, client):
        """Values below 100_000_000_000 (seconds-looking) must be 422."""
        _create_emp(client)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": 1783312500,  # looks like seconds
        })
        assert r.status_code == 422

    def test_wfh_status_accepted(self, client):
        _create_emp(client)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": _ms(2026, 7, 6, 9, 0),
            "status": "WFH",
        })
        assert r.status_code == 201
        assert r.json()["status"] == "WFH"

    def test_absent_status_rejected(self, client):
        """Punch-in only accepts PRESENT, WFH, ON_DUTY."""
        _create_emp(client)
        r = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "status": "ABSENT",
        })
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Punch-out
# ---------------------------------------------------------------------------
class TestPunchOut:
    def _do_punch_in(self, client, punch_ms=None):
        _create_emp(client)
        if punch_ms is None:
            punch_ms = _ms(2026, 7, 6, 9, 28)
        r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_ms})
        assert r.status_code == 201
        return r.json()

    def test_punch_out_computes_all_fields(self, client):
        self._do_punch_in(client, _ms(2026, 7, 6, 9, 28))
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": _ms(2026, 7, 6, 18, 35),
        })
        assert r.status_code == 200
        data = r.json()
        assert data["work_hours"] == 9.12
        assert data["overtime_minutes"] == 0   # 5 min < 30
        assert data["half_day"] is False

    def test_overtime_40_min(self, client):
        self._do_punch_in(client, _ms(2026, 7, 6, 9, 30))
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": _ms(2026, 7, 6, 19, 10),
        })
        assert r.status_code == 200
        assert r.json()["overtime_minutes"] == 40

    def test_half_day(self, client):
        self._do_punch_in(client, _ms(2026, 7, 6, 9, 30))
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": _ms(2026, 7, 6, 13, 0),  # 3.5 hrs
        })
        assert r.status_code == 200
        data = r.json()
        assert data["half_day"] is True
        assert data["work_hours"] == 3.5

    def test_punch_out_before_punch_in_is_422(self, client):
        pi_ms = _ms(2026, 7, 6, 10, 0)
        self._do_punch_in(client, pi_ms)
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": _ms(2026, 7, 6, 9, 0),  # before punch_in
        })
        assert r.status_code == 422

    def test_punch_out_over_24h_is_422(self, client):
        pi_ms = _ms(2026, 7, 6, 9, 0)
        self._do_punch_in(client, pi_ms)
        r = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": _ms(2026, 7, 8, 9, 1),  # > 24h
        })
        assert r.status_code == 422

    def test_no_punch_in_is_404(self, client):
        _create_emp(client)
        r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001"})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Regularize
# ---------------------------------------------------------------------------
class TestRegularize:
    def _setup(self, client):
        _create_emp(client)
        client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": _ms(2026, 7, 6, 10, 5),  # 35 min late
        })
        client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": _ms(2026, 7, 6, 19, 10),  # 40 min OT
        })

    def test_regularize_punch_in_updates_late(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": _ms(2026, 7, 6, 9, 28),   # corrected to on-time
            "reason": "biometric glitch at entry gate",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["late_minutes"] == 0
        assert len(data["history"]) == 1
        h = data["history"][0]
        assert h["by"] == "hr.admin"
        assert "punch_in" in h["changes"]
        assert "late_minutes" in h["changes"]

    def test_no_change_is_422(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": _ms(2026, 7, 6, 10, 5),   # same as current
            "reason": "testing no change scenario",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 422

    def test_absent_clears_punches(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "reason": "attendance correction for no-show",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ABSENT"
        assert data["punch_in"] is None
        assert data["punch_out"] is None

    def test_absent_with_punch_in_is_422(self, client):
        self._setup(client)
        r = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "punch_in": _ms(2026, 7, 6, 9, 0),
            "reason": "this should fail",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 422

    def test_not_found_is_404(self, client):
        _create_emp(client)
        r = client.patch("/attendance/EMP0001/2099-01-01", json={
            "reason": "correction for a nonexistent date",
            "regularized_by": "hr.admin",
        })
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# List attendance
# ---------------------------------------------------------------------------
class TestListAttendance:
    def test_date_from_gt_date_to_is_422(self, client):
        r = client.get("/attendance?date_from=2026-08-01&date_to=2026-07-01")
        assert r.status_code == 422

    def test_returns_sorted_desc(self, client):
        _create_emp(client)
        for day in [6, 7, 8]:
            client.post("/attendance/punch-in", json={
                "emp_code": "EMP0001",
                "punched_at": _ms(2026, 7, day, 9, 0),
            })
        r = client.get("/attendance?emp_code=EMP0001")
        dates = [item["date"] for item in r.json()["items"]]
        assert dates == sorted(dates, reverse=True)

    def test_total_reflects_filter(self, client):
        _create_emp(client, "EMP0001")
        _create_emp(client, "EMP0002")
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": _ms(2026, 7, 6, 9, 0)})
        client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": _ms(2026, 7, 6, 9, 0)})
        r = client.get("/attendance?emp_code=EMP0001")
        assert r.json()["total"] == 1

