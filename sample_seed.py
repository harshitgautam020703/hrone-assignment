"""
Enhanced seed script: loads both clean sample data AND messy edge-case data.

Messy data includes:
  - Missing punch-outs (open records)
  - Manual corrections with audit trail (history entries)
  - Half-day records
  - Overnight shift records
  - ABSENT and LEAVE records
  - Employees with zero logs (tests headcount R9)
  - Overlapping-adjacent records for analytics edge cases
  - Late punch-ins at various thresholds (to test R2 boundary at 10 min exactly)

Usage:
  python sample_seed.py            # loads both clean + messy
  python sample_seed.py --clean    # loads only sample_data/ (original)
  python sample_seed.py --messy    # loads only messy edge-case data
"""
import argparse
import os
import pathlib
from datetime import datetime, timedelta, timezone

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
here = pathlib.Path(__file__).parent / "sample_data"

opts = json_util.JSONOptions(tz_aware=True)
db = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True
)[os.getenv("MONGO_DB", "attendance_db")]


def load_clean():
    """Load the original 9 sample documents."""
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        db[name].delete_many({})
        db[name].insert_many(docs)
        print(f"[clean] loaded {len(docs):>2} documents into '{db.name}.{name}'")


def ist(year, month, day, hour, minute, second=0):
    """Create a UTC-aware datetime from IST wall-clock values."""
    return datetime(year, month, day, hour, minute, second, tzinfo=IST).astimezone(UTC)


def load_messy():
    """
    Seed messy / edge-case data for realistic analytics and audit trail testing.
    Inserts additional employees and attendance logs without dropping existing ones.
    """
    # ── Messy employees ─────────────────────────────────────────────────────
    messy_employees = [
        # Employee with zero logs (headcount test, R9)
        {
            "emp_code": "EMP9001",
            "name": "Zero Logs Employee",
            "email": "zerologs@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
            "created_at": datetime(2026, 7, 1, 4, 0, tzinfo=UTC),
        },
        # Employee who joins mid-month (R7 boundary test)
        {
            "emp_code": "EMP9002",
            "name": "Mid Month Joiner",
            "email": "midmonth@example.com",
            "department": "Sales",
            "shift_start": "10:00",
            "shift_end": "19:00",
            "joined_on": "2026-07-15",
            "created_at": datetime(2026, 7, 15, 4, 30, tzinfo=UTC),
        },
        # Overnight shift employee
        {
            "emp_code": "EMP9003",
            "name": "Night Shift Worker",
            "email": "night@example.com",
            "department": "Operations",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-06-01",
            "created_at": datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        },
    ]

    for emp in messy_employees:
        db.employees.update_one(
            {"emp_code": emp["emp_code"]}, {"$setOnInsert": emp}, upsert=True
        )
    print(f"[messy] upserted {len(messy_employees)} employees")

    # ── Messy attendance logs ────────────────────────────────────────────────
    messy_logs = [
        # --- Missing punch-out (open record) ---
        {
            "emp_code": "EMP9002",
            "date": "2026-07-15",
            "status": "PRESENT",
            "punch_in": ist(2026, 7, 15, 10, 5),   # on time
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # --- R2 boundary: exactly 10:00 late → 0 (not late) ---
        {
            "emp_code": "EMP9002",
            "date": "2026-07-16",
            "status": "PRESENT",
            "punch_in": ist(2026, 7, 16, 10, 10),  # exactly 10 min after 10:00 = not late
            "punch_out": ist(2026, 7, 16, 19, 5),
            "work_hours": 8.92,
            "late_minutes": 0,                      # grace boundary
            "overtime_minutes": 0,                  # 5 min < 30
            "half_day": False,
            "history": [],
        },
        # --- R2 boundary: 10:01 late → 10 min ---
        {
            "emp_code": "EMP9002",
            "date": "2026-07-17",
            "status": "WFH",
            "punch_in": ist(2026, 7, 17, 10, 11),  # 11 min late
            "punch_out": ist(2026, 7, 17, 19, 0),
            "work_hours": 8.82,
            "late_minutes": 11,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # --- Half-day with correction history ---
        {
            "emp_code": "EMP9002",
            "date": "2026-07-21",
            "status": "PRESENT",
            "punch_in": ist(2026, 7, 21, 10, 0),
            "punch_out": ist(2026, 7, 21, 14, 0),  # 4 hrs → half_day
            "work_hours": 4.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": True,
            "history": [
                {
                    "at": datetime(2026, 7, 22, 9, 0, tzinfo=UTC),
                    "by": "hr.manager",
                    "reason": "Employee had doctor's appointment, confirmed half day",
                    "changes": {
                        "status": {"from": "ABSENT", "to": "PRESENT"},
                        "punch_in": {
                            "from": None,
                            "to": ist(2026, 7, 21, 10, 0),
                        },
                        "punch_out": {
                            "from": None,
                            "to": ist(2026, 7, 21, 14, 0),
                        },
                        "work_hours": {"from": None, "to": 4.0},
                        "half_day": {"from": False, "to": True},
                    },
                }
            ],
        },
        # --- Overnight shift: date = day shift started ---
        {
            "emp_code": "EMP9003",
            "date": "2026-07-14",  # shift started 14th evening
            "status": "PRESENT",
            "punch_in": ist(2026, 7, 14, 21, 55),  # 5 min early
            "punch_out": ist(2026, 7, 15, 6, 45),  # 45 min OT
            "work_hours": 8.83,
            "late_minutes": 0,
            "overtime_minutes": 45,
            "half_day": False,
            "history": [],
        },
        # --- ABSENT record (no punch times) ---
        {
            "emp_code": "EMP9003",
            "date": "2026-07-16",
            "status": "ABSENT",
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # --- LEAVE record ---
        {
            "emp_code": "EMP9003",
            "date": "2026-07-17",
            "status": "LEAVE",
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # --- Very late + high OT (leaderboard edge case) ---
        {
            "emp_code": "EMP9002",
            "date": "2026-07-22",
            "status": "ON_DUTY",
            "punch_in": ist(2026, 7, 22, 12, 30),  # 2.5 hrs late
            "punch_out": ist(2026, 7, 22, 22, 0),   # 3 hrs OT
            "work_hours": 9.5,
            "late_minutes": 150,
            "overtime_minutes": 180,
            "half_day": False,
            "history": [],
        },
        # --- Legacy record (missing half_day and history fields) ---
        {
            "emp_code": "EMP9003",
            "date": "2026-07-07",
            "status": "PRESENT",
            "punch_in": ist(2026, 7, 7, 22, 2),
            "punch_out": ist(2026, 7, 8, 6, 5),
            "work_hours": 8.05,
            "late_minutes": 2,
            "overtime_minutes": 0,
            # half_day and history intentionally omitted (legacy)
        },
    ]

    inserted = 0
    for log in messy_logs:
        result = db.attendance_logs.update_one(
            {"emp_code": log["emp_code"], "date": log["date"]},
            {"$setOnInsert": log},
            upsert=True,
        )
        if result.upserted_id:
            inserted += 1
    print(f"[messy] upserted {inserted}/{len(messy_logs)} attendance logs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the attendance database")
    parser.add_argument("--clean", action="store_true", help="Load only clean sample data")
    parser.add_argument("--messy", action="store_true", help="Load only messy edge-case data")
    args = parser.parse_args()

    if args.clean:
        load_clean()
    elif args.messy:
        load_messy()
    else:
        load_clean()
        load_messy()

    print("Done.")
