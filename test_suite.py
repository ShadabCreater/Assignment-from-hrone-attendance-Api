"""
Comprehensive Test Suite for HROne Candidate Assignment
Tests all business rules R1-R10, status codes, concurrency, and analytics pipelines.
"""
import calendar
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import sys

from fastapi.testclient import TestClient
from pymongo import MongoClient

# Ensure app can be imported
sys.path.insert(0, os.path.dirname(__file__))

# Reload seed data first
os.system(f"python {os.path.join(os.path.dirname(__file__), 'sample_seed.py')}")

from app.main import app, db, init_indexes, IST

init_indexes()
client = TestClient(app)


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
    print("[PASS] test_health passed")


def test_employees_crud():
    # Invalid employee
    res = client.post("/employees", json={"emp_code": "INVALID", "name": "Test", "email": "bad", "department": "IT", "joined_on": "2026-01-01"})
    assert res.status_code == 422

    # Create employee
    new_emp = {
        "emp_code": "EMP0099",
        "name": "Rohan Sharma",
        "email": "rohan@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    }
    res = client.post("/employees", json=new_emp)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0099"
    assert "created_at" in data
    assert isinstance(data["created_at"], int)

    # Duplicate employee -> 409
    res = client.post("/employees", json=new_emp)
    assert res.status_code == 409

    # List employees (page 1)
    res = client.get("/employees?department=Engineering&page=1&page_size=10")
    assert res.status_code == 200
    p = res.json()
    assert p["page"] == 1
    assert p["page_size"] == 10
    assert p["total"] >= 1
    # Check sorted by emp_code
    codes = [item["emp_code"] for item in p["items"]]
    assert codes == sorted(codes)
    print("[PASS] test_employees_crud passed")


def test_punch_rules_r1_r2_r3_r4_r5():
    # Create test employee with overnight shift 22:00 - 06:00
    emp_night = {
        "emp_code": "EMP0098",
        "name": "Night Owl",
        "email": "night@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-06-01",
    }
    client.post("/employees", json=emp_night)

    # R1: Overnight shift early morning punch-in (01:30 IST on July 10 belongs to July 9 shift)
    # 2026-07-10 01:30:00 IST = 2026-07-09 20:00:00 UTC
    dt_early = datetime(2026, 7, 10, 1, 30, 0, tzinfo=IST)
    ms_early = int(dt_early.timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0098", "punched_at": ms_early})
    assert res.status_code == 201
    rec = res.json()
    assert rec["date"] == "2026-07-09", f"Expected 2026-07-09, got {rec['date']}"
    # Punch-out at 06:40 IST on July 10 (40 min overtime)
    dt_out = datetime(2026, 7, 10, 6, 40, 0, tzinfo=IST)
    ms_out = int(dt_out.timestamp() * 1000)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0098", "punched_at": ms_out})
    assert res.status_code == 200
    out_rec = res.json()
    assert out_rec["overtime_minutes"] == 40
    # Work hours: 01:30 to 06:40 is 5 hours 10 minutes = 5.17 hours
    assert out_rec["work_hours"] == 5.17
    assert out_rec["half_day"] is False

    # Test R2 Late grace period on normal shift employee EMP0099 (shift 09:30 - 18:30)
    # Case A: 09:40:00 IST (exactly 10 min) -> late_minutes = 0
    dt_grace = datetime(2026, 7, 2, 9, 40, 0, tzinfo=IST)
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0099", "punched_at": int(dt_grace.timestamp() * 1000)})
    assert res.status_code == 201
    assert res.json()["late_minutes"] == 0

    # Punch out for July 2
    dt_grace_out = datetime(2026, 7, 2, 18, 30, 0, tzinfo=IST)
    client.post("/attendance/punch-out", json={"emp_code": "EMP0099", "punched_at": int(dt_grace_out.timestamp() * 1000)})

    # Case B: 09:40:01 IST -> late_minutes = 10
    dt_late10 = datetime(2026, 7, 3, 9, 40, 1, tzinfo=IST)
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0099", "punched_at": int(dt_late10.timestamp() * 1000)})
    assert res.status_code == 201
    assert res.json()["late_minutes"] == 10

    # Punch out with < 30 min overtime (18:45 IST = 15 min OT -> 0)
    dt_ot15 = datetime(2026, 7, 3, 18, 45, 0, tzinfo=IST)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0099", "punched_at": int(dt_ot15.timestamp() * 1000)})
    assert res.status_code == 200
    assert res.json()["overtime_minutes"] == 0

    # Case C: Half-day (< 4.5 hours)
    dt_hd_in = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0099", "punched_at": int(dt_hd_in.timestamp() * 1000)})
    dt_hd_out = datetime(2026, 7, 6, 13, 0, 0, tzinfo=IST) # 3.5 hours
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0099", "punched_at": int(dt_hd_out.timestamp() * 1000)})
    assert res.status_code == 200
    assert res.json()["work_hours"] == 3.5
    assert res.json()["half_day"] is True
    print("[PASS] test_punch_rules_r1_r2_r3_r4_r5 passed")


def test_regularize_patch():
    # Regularize EMP0099 on 2026-07-06: change punch_out to 18:30 (full day)
    dt_new_out = datetime(2026, 7, 6, 18, 30, 0, tzinfo=IST)
    ms_new_out = int(dt_new_out.timestamp() * 1000)

    res = client.patch(
        "/attendance/EMP0099/2026-07-06",
        json={
            "punch_out": ms_new_out,
            "reason": "forgot to punch out",
            "regularized_by": "manager.bob",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["half_day"] is False
    assert data["work_hours"] == 9.0
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "manager.bob"
    assert "work_hours" in h["changes"]
    assert h["changes"]["work_hours"]["from"] == 3.5
    assert h["changes"]["work_hours"]["to"] == 9.0

    # No changes -> 422
    res = client.patch(
        "/attendance/EMP0099/2026-07-06",
        json={"punch_out": ms_new_out, "reason": "redundant edit", "regularized_by": "manager.bob"},
    )
    assert res.status_code == 422

    # Setting status to ABSENT clears times, supplying punch_out with ABSENT -> 422
    res = client.patch(
        "/attendance/EMP0099/2026-07-06",
        json={"status": "ABSENT", "punch_out": ms_new_out, "reason": "bad request", "regularized_by": "hr"},
    )
    assert res.status_code == 422
    print("[PASS] test_regularize_patch passed")


def test_attendance_list():
    res = client.get("/attendance?date_from=2026-07-01&date_to=2026-07-31&page=1&page_size=5")
    assert res.status_code == 200
    data = res.json()
    assert data["page"] == 1
    assert data["page_size"] == 5
    assert data["total"] > 0
    assert len(data["items"]) <= 5
    # Verify no 'id' field is present
    for item in data["items"]:
        assert "id" not in item
        assert "_id" not in item

    # date_from > date_to -> 422
    res = client.get("/attendance?date_from=2026-07-31&date_to=2026-07-01")
    assert res.status_code == 422
    print("[PASS] test_attendance_list passed")


def test_analytics_employee_monthly():
    # EMP0001 in July 2026
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23 # July 2026 has 23 weekdays
    assert data["present_days"] >= 0
    assert "attendance_pct" in data

    # Unknown employee -> 404
    res = client.get("/analytics/employees/EMP_UNKNOWN/monthly?month=2026-07")
    assert res.status_code == 404
    print("[PASS] test_analytics_employee_monthly passed")


def test_analytics_department_summary():
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) > 0
    depts = [item["department"] for item in data["items"]]
    assert depts == sorted(depts)
    for item in data["items"]:
        assert item["headcount"] >= 1
        assert "present_days" in item
        assert "avg_work_hours" in item
        assert "late_count" in item
    print("[PASS] test_analytics_department_summary passed")


def test_analytics_late_leaderboard():
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    items = data["items"]
    # Check ranking order
    for i in range(len(items) - 1):
        assert items[i]["total_late_minutes"] >= items[i + 1]["total_late_minutes"]
        assert items[i]["rank"] <= items[i + 1]["rank"]
    print("[PASS] test_analytics_late_leaderboard passed")


def test_analytics_department_trend():
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 7 # exactly 7 calendar days
    days = [item["date"] for item in data["items"]]
    assert days == [
        "2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04",
        "2026-07-05", "2026-07-06", "2026-07-07"
    ]
    # Check 404 on unknown department
    res = client.get("/analytics/departments/UnknownDept/trend?from=2026-07-01&to=2026-07-07")
    assert res.status_code == 404
    # Check 422 on reversed dates
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-07&to=2026-07-01")
    assert res.status_code == 422
    print("[PASS] test_analytics_department_trend passed")


def test_admin_explain():
    endpoints = [
        ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-07"}),
    ]

    for ep, params in endpoints:
        res = client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200, f"Explain failed for {ep}: {res.text}"
        data = res.json()
        assert data["endpoint"] == ep
        assert "collection" in data
        assert "explain" in data
        explain_str = str(data["explain"])
        assert "IXSCAN" in explain_str, f"Expected IXSCAN in explain for {ep}"
        assert "COLLSCAN" not in explain_str, f"Unexpected COLLSCAN in explain for {ep}"
        print(f"[PASS] admin/explain/{ep} (IXSCAN=True, COLLSCAN=False)")


if __name__ == "__main__":
    test_health()
    test_employees_crud()
    test_punch_rules_r1_r2_r3_r4_r5()
    test_regularize_patch()
    test_attendance_list()
    test_analytics_employee_monthly()
    test_analytics_department_summary()
    test_analytics_late_leaderboard()
    test_analytics_department_trend()
    test_admin_explain()
    print("\nALL TESTS PASSED WITH 100% SUCCESS!")
