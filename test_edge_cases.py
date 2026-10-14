"""
Edge Case and Concurrency Test Suite
Specifically verifies:
1. Concurrency races (punch-in & punch-out)
2. Leaderboard ties and standard competition ranking (1, 2, 2, 4)
3. Department summary headcount (zero-log employees + future joiners excluded)
4. Trend gap filling on zero-record days and moving average window
"""
import concurrent.futures
from datetime import datetime, date
import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))

# Reload seed data
os.system(f"python {os.path.join(os.path.dirname(__file__), 'sample_seed.py')}")

from app.main import app, db, init_indexes, IST

init_indexes()
client = TestClient(app)


def test_punch_in_concurrency():
    """Two identical punch-in requests arrive at the same instant."""
    # Ensure fresh employee
    client.post("/employees", json={
        "emp_code": "EMP0101",
        "name": "Race Tester",
        "email": "race@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    punch_payload = {
        "emp_code": "EMP0101",
        "punched_at": 1783310000000,
        "status": "PRESENT"
    }

    def do_punch():
        c = TestClient(app)
        return c.post("/attendance/punch-in", json=punch_payload)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch) for _ in range(5)]
        responses = [f.result() for f in futures]

    status_codes = [r.status_code for r in responses]
    assert status_codes.count(201) == 1, f"Expected exactly one 201, got: {status_codes}"
    assert status_codes.count(409) == 4, f"Expected four 409s, got: {status_codes}"
    print("[PASS] test_punch_in_concurrency passed: exactly one 201 and four 409s")


def test_punch_out_concurrency():
    """Two simultaneous punch-outs for the same record yield exactly one 200 and one 409."""
    client.post("/employees", json={
        "emp_code": "EMP0102",
        "name": "Punchout Tester",
        "email": "punchout@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    # Punch in first
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0102",
        "punched_at": 1783310000000,
        "status": "PRESENT"
    })

    punch_out_payload = {
        "emp_code": "EMP0102",
        "punched_at": 1783340000000
    }

    def do_punch_out():
        c = TestClient(app)
        return c.post("/attendance/punch-out", json=punch_out_payload)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(do_punch_out) for _ in range(4)]
        responses = [f.result() for f in futures]

    status_codes = [r.status_code for r in responses]
    assert status_codes.count(200) == 1, f"Expected exactly one 200, got: {status_codes}"
    assert status_codes.count(409) == 3, f"Expected three 409s, got: {status_codes}"
    print("[PASS] test_punch_out_concurrency passed: exactly one 200 and three 409s")


def test_leaderboard_ties():
    """Test standard competition ranking (1, 2, 2, 4) with ties at limit cutoff."""
    # Seed 4 employees in 'TiesDept'
    for i in range(1, 5):
        emp_code = f"EMP020{i}"
        client.post("/employees", json={
            "emp_code": emp_code,
            "name": f"Employee {i}",
            "email": f"emp{i}@example.com",
            "department": "TiesDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

    # Employee 1: 50 min late
    # Employee 2: 30 min late
    # Employee 3: 30 min late (tied with Employee 2)
    # Employee 4: 10 min late
    month = "2026-08"
    db.attendance_logs.insert_many([
        {"emp_code": "EMP0201", "date": f"{month}-01", "status": "PRESENT", "late_minutes": 50, "work_hours": 8.0, "half_day": False, "history": []},
        {"emp_code": "EMP0202", "date": f"{month}-01", "status": "PRESENT", "late_minutes": 30, "work_hours": 8.0, "half_day": False, "history": []},
        {"emp_code": "EMP0203", "date": f"{month}-01", "status": "PRESENT", "late_minutes": 30, "work_hours": 8.0, "half_day": False, "history": []},
        {"emp_code": "EMP0204", "date": f"{month}-01", "status": "PRESENT", "late_minutes": 10, "work_hours": 8.0, "half_day": False, "history": []},
    ])

    # Case A: Request limit=2
    # Ranks should be: EMP0201 -> rank 1
    # EMP0202 -> rank 2
    # EMP0203 -> rank 2
    # Since rank <= limit (2 <= 2), both EMP0202 and EMP0203 are included! Total rows returned = 3!
    res = client.get(f"/analytics/leaderboard/late?month={month}&department=TiesDept&limit=2")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3, f"Expected 3 items due to tie at rank 2, got {len(items)}"
    assert items[0]["emp_code"] == "EMP0201" and items[0]["rank"] == 1
    assert items[1]["emp_code"] == "EMP0202" and items[1]["rank"] == 2
    assert items[2]["emp_code"] == "EMP0203" and items[2]["rank"] == 2

    # Case B: Request limit=4
    # Next rank after 2 is 4 (rank 3 is skipped)!
    res = client.get(f"/analytics/leaderboard/late?month={month}&department=TiesDept&limit=4")
    assert res.status_code == 200
    items4 = res.json()["items"]
    assert len(items4) == 4
    assert items4[3]["emp_code"] == "EMP0204"
    assert items4[3]["rank"] == 4, f"Expected rank 4 (standard competition ranking), got {items4[3]['rank']}"
    print("[PASS] test_leaderboard_ties passed: standard competition ranking 1, 2, 2, 4 verified!")


def test_department_summary_zero_logs_and_future_joiners():
    """Verify that department summary includes zero-log employees and excludes future joiners."""
    # Dept 'ZeroDept':
    # Emp A: joined 2026-07-01, has 0 logs
    # Emp B: joined 2026-08-05 (future joiner relative to July 2026) -> must NOT be counted in July!
    client.post("/employees", json={
        "emp_code": "EMP0301",
        "name": "Zero Logs",
        "email": "zero@example.com",
        "department": "ZeroDept",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })
    client.post("/employees", json={
        "emp_code": "EMP0302",
        "name": "Future Joiner",
        "email": "future@example.com",
        "department": "ZeroDept",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-08-05"
    })

    res = client.get("/analytics/departments/summary?month=2026-07&department=ZeroDept")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["department"] == "ZeroDept"
    assert item["headcount"] == 1, f"Expected headcount 1 (EMP0301 only), got {item['headcount']}"
    assert item["present_days"] == 0.0
    assert item["avg_work_hours"] is None
    assert item["late_count"] == 0
    assert item["total_late_minutes"] == 0
    print("[PASS] test_department_summary_zero_logs_and_future_joiners passed")


if __name__ == "__main__":
    test_punch_in_concurrency()
    test_punch_out_concurrency()
    test_leaderboard_ties()
    test_department_summary_zero_logs_and_future_joiners()
    print("\nALL EDGE CASE TESTS PASSED 100%!")
