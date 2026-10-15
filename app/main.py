"""
Employee Attendance & Analytics API
====================================
Contract: openapi.yaml
Data Model: DATA_MODEL.md
Run: uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import math
import os
import re
import time
from calendar import monthrange
from datetime import date as DateType, datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Header
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Formal domain states for attendance lifecycle
class AttendanceState(str, Enum):
    OUT = "OUT"
    PUNCHED_IN = "PUNCHED_IN"
    PUNCHED_OUT = "PUNCHED_OUT"


# 30-second TTL cache for optional Idempotency-Key header support
_idempotency_cache: dict[str, tuple[float, dict]] = {}

# ---------------------------------------------------------------------------
# 1. Environment & Database Configuration
# ---------------------------------------------------------------------------
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

_client: MongoClient | None = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI, tz_aware=True)
    return _client


def get_db():
    return get_client()[MONGO_DB]


def create_indexes(db) -> None:
    """
    Create required indexes idempotently at application startup.
    Guarantees IXSCAN for queries and prevents race conditions at the DB layer.
    """
    employees = db["employees"]
    logs = db["attendance_logs"]

    # employees indexes
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    employees.create_index([("department", ASCENDING)], name="idx_department")
    employees.create_index([("joined_on", ASCENDING)], name="idx_joined_on")

    # attendance_logs indexes
    # Natural key: prevents concurrent duplicate punch-ins atomically
    logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_emp_date",
    )
    # Covers GET /attendance sort and date ranges
    logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_date_desc_emp_asc",
    )
    # Compound index for late queries and leaderboard
    logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)],
        name="idx_date_late",
    )
    logs.create_index([("late_minutes", ASCENDING)], name="idx_late_minutes")
    logs.create_index([("status", ASCENDING)], name="idx_status")


# ---------------------------------------------------------------------------
# 2. Timezone & Business Rules (R1–R8)
# ---------------------------------------------------------------------------
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}


def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: Truncate sub-second precision; returns a UTC-aware datetime."""
    return dt.replace(microsecond=0)


def attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> DateType:
    """
    R1: Return IST attendance date. For overnight shifts (shift_end <= shift_start),
    a punch-in before shift_end belongs to the previous calendar day.
    """
    punch_ist = punch_in_utc.astimezone(IST)
    ist_date = punch_ist.date()

    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)

    if is_overnight:
        punch_minutes = punch_ist.hour * 60 + punch_ist.minute
        shift_end_minutes = eh * 60 + em
        if punch_minutes < shift_end_minutes:
            ist_date -= timedelta(days=1)

    return ist_date


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, att_date: DateType) -> int:
    """
    R2: Late if strictly > 10 min after shift_start.
    Then late_minutes = floor minutes since shift_start; otherwise 0.
    """
    sh, sm = map(int, shift_start.split(":"))
    shift_start_ist = datetime(
        att_date.year, att_date.month, att_date.day, sh, sm, 0, tzinfo=IST
    )
    punch_ist = punch_in_utc.astimezone(IST)
    elapsed_seconds = int((punch_ist - shift_start_ist).total_seconds())
    if elapsed_seconds <= 10 * 60:
        return 0
    return elapsed_seconds // 60


def compute_overtime_minutes(
    punch_out_utc: datetime, shift_end: str, att_date: DateType, shift_start: str
) -> int:
    """R3: Whole minutes between shift_end and punch_out, only if >= 30, else 0."""
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)

    end_date = att_date + timedelta(days=1) if is_overnight else att_date
    shift_end_ist = datetime(
        end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST
    )
    punch_ist = punch_out_utc.astimezone(IST)
    elapsed_seconds = int((punch_ist - shift_end_ist).total_seconds())
    if elapsed_seconds < 30 * 60:
        return 0
    return elapsed_seconds // 60


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    total_seconds = int((punch_out_utc - punch_in_utc).total_seconds())
    raw = total_seconds / 3600
    return math.floor(raw * 100 + 0.5) / 100


def compute_half_day(work_hours: float) -> bool:
    """R5: True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def round_half_up(value: float, decimals: int) -> float:
    """R8: Standard round half-up."""
    factor = 10 ** decimals
    return math.floor(value * factor + 0.5) / factor


def recompute_derived(
    punch_in_utc: datetime | None,
    punch_out_utc: datetime | None,
    shift_start: str,
    shift_end: str,
    att_date: DateType,
) -> dict:
    """Recomputes late_minutes, work_hours, overtime_minutes, half_day."""
    if punch_in_utc is None:
        return {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }

    late = compute_late_minutes(punch_in_utc, shift_start, att_date)
    if punch_out_utc is None:
        return {
            "late_minutes": late,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }

    wh = compute_work_hours(punch_in_utc, punch_out_utc)
    ot = compute_overtime_minutes(punch_out_utc, shift_end, att_date, shift_start)
    hd = compute_half_day(wh)
    return {
        "late_minutes": late,
        "work_hours": wh,
        "overtime_minutes": ot,
        "half_day": hd,
    }


def month_bounds(month: str) -> tuple[str, str, DateType, DateType]:
    year, mon = int(month[:4]), int(month[5:])
    _, last_day = monthrange(year, mon)
    d_from = DateType(year, mon, 1)
    d_to = DateType(year, mon, last_day)
    return d_from.isoformat(), d_to.isoformat(), d_from, d_to


def working_days_in_range(start: DateType, end: DateType) -> int:
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # Mon-Fri
            count += 1
        cur += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# 3. Serialization Helpers
# ---------------------------------------------------------------------------
def dt_to_ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def ms_to_dt(ms: int | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def serialize_employee(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    d["created_at"] = dt_to_ms(d.get("created_at"))
    return d


def serialize_history_entry(entry: dict) -> dict:
    h = dict(entry)
    h["at"] = dt_to_ms(h.get("at"))
    changes = {}
    for k, diff in h.get("changes", {}).items():
        diff_dict = dict(diff)
        if k in ("punch_in", "punch_out"):
            diff_dict["from"] = dt_to_ms(diff_dict.get("from"))
            diff_dict["to"] = dt_to_ms(diff_dict.get("to"))
        changes[k] = diff_dict
    h["changes"] = changes
    return h


def serialize_attendance(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    d["punch_in"] = dt_to_ms(d.get("punch_in"))
    d["punch_out"] = dt_to_ms(d.get("punch_out"))
    d.setdefault("late_minutes", 0)
    d.setdefault("overtime_minutes", 0)
    d.setdefault("half_day", False)
    d.setdefault("history", [])
    d["history"] = [serialize_history_entry(h) for h in d["history"]]
    return d


# ---------------------------------------------------------------------------
# 4. Pydantic Models & EpochMillis Validation
# ---------------------------------------------------------------------------
EpochMillis = Annotated[
    int,
    Field(
        ge=100_000_000_000,   # rejects seconds-looking numbers
        le=4_102_444_800_000,
    ),
]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def shift_times_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def valid_status(cls, v: str) -> str:
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        return v

    @model_validator(mode="after")
    def absent_leave_no_punches(self) -> "RegularizeRequest":
        if self.status in {"ABSENT", "LEAVE"}:
            if self.punch_in is not None or self.punch_out is not None:
                raise ValueError("Cannot supply punch_in or punch_out when status is ABSENT or LEAVE")
        return self


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes(get_db())
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="FastAPI + MongoDB Attendance and Analytics API following openapi.yaml",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 6. System Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", tags=["system"])
def health():
    try:
        get_db().command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


# ---------------------------------------------------------------------------
# 7. Employee Endpoints
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    db = get_db()
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=UTC)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    db = get_db()
    q: dict = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size))
    return {
        "items": [serialize_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# 8. Attendance Endpoints
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(
    body: PunchInRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    if idempotency_key:
        now_epoch = time.time()
        cached = _idempotency_cache.get(idempotency_key)
        if cached and (now_epoch - cached[0] < 30.0):
            return cached[1]

    db = get_db()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_ts = truncate_to_seconds(ms_to_dt(body.punched_at) if body.punched_at else datetime.now(tz=UTC))
    att_date = attendance_date(punch_ts, emp["shift_start"], emp["shift_end"])
    date_str = att_date.isoformat()

    derived = recompute_derived(punch_ts, None, emp["shift_start"], emp["shift_end"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Employee already has a record for this attendance date")

    response_doc = serialize_attendance(doc)
    if idempotency_key:
        _idempotency_cache[idempotency_key] = (time.time(), response_doc)

    return response_doc


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    db = get_db()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_ts = truncate_to_seconds(ms_to_dt(body.punched_at) if body.punched_at else datetime.now(tz=UTC))

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_ts}, "punch_out": None},
        sort=[("punch_in", -1)],
    )

    if record is None:
        existing = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_out": {"$ne": None}},
            sort=[("date", -1)],
        )
        if existing:
            raise HTTPException(status_code=409, detail="Most recent record is already punched out")
        raise HTTPException(status_code=404, detail="No open punch-in found for this employee")

    punch_in_ts: datetime = record["punch_in"]

    if punch_ts <= punch_in_ts:
        raise HTTPException(status_code=422, detail=[{"msg": "punched_at must be after punch_in"}])
    if punch_ts - punch_in_ts > timedelta(hours=24):
        raise HTTPException(status_code=422, detail=[{"msg": "punched_at must be within 24 hours of punch_in"}])

    att_date = DateType.fromisoformat(record["date"])
    derived = recompute_derived(punch_in_ts, punch_ts, emp["shift_start"], emp["shift_end"], att_date)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_ts,
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(status_code=409, detail="Record was concurrently modified")

    return serialize_attendance(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    db = get_db()
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail=[{"msg": "date_from must be <= date_to"}])

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": [serialize_attendance(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    db = get_db()
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    new_status = body.status if body.status is not None else record["status"]
    current_punch_in = record.get("punch_in")
    current_punch_out = record.get("punch_out")

    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = ms_to_dt(body.punch_in) if body.punch_in is not None else current_punch_in
        if new_punch_in is not None:
            new_punch_in = truncate_to_seconds(new_punch_in)
        new_punch_out = ms_to_dt(body.punch_out) if body.punch_out is not None else current_punch_out
        if new_punch_out is not None:
            new_punch_out = truncate_to_seconds(new_punch_out)

        if new_punch_in is None:
            raise HTTPException(status_code=422, detail=[{"msg": "Presence status requires punch_in"}])

        computed_date = attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date.isoformat() != date:
            raise HTTPException(status_code=422, detail=[{"msg": "punch_in must remain on the attendance date"}])

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(status_code=422, detail=[{"msg": "punch_out must be after punch_in"}])
            if new_punch_out - new_punch_in > timedelta(hours=24):
                raise HTTPException(status_code=422, detail=[{"msg": "punch_out must be within 24 hours"}])

    att_date = DateType.fromisoformat(date)
    derived = recompute_derived(new_punch_in, new_punch_out, emp["shift_start"], emp["shift_end"], att_date)
    if new_status in ("ABSENT", "LEAVE"):
        derived = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}

    def _dt_eq(a: datetime | None, b: datetime | None) -> bool:
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return int(a.timestamp()) == int(b.timestamp())

    changes: dict = {}
    if new_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": new_status}
    if not _dt_eq(new_punch_in, record.get("punch_in")):
        changes["punch_in"] = {"from": dt_to_ms(record.get("punch_in")), "to": dt_to_ms(new_punch_in)}
    if not _dt_eq(new_punch_out, record.get("punch_out")):
        changes["punch_out"] = {"from": dt_to_ms(record.get("punch_out")), "to": dt_to_ms(new_punch_out)}
    if derived["late_minutes"] != record.get("late_minutes", 0):
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": derived["late_minutes"]}
    if derived["work_hours"] != record.get("work_hours"):
        changes["work_hours"] = {"from": record.get("work_hours"), "to": derived["work_hours"]}
    if derived["overtime_minutes"] != record.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": derived["overtime_minutes"]}
    if derived["half_day"] != record.get("half_day", False):
        changes["half_day"] = {"from": record.get("half_day", False), "to": derived["half_day"]}

    if not changes:
        raise HTTPException(status_code=422, detail=[{"msg": "No changes detected"}])

    db_changes: dict = {}
    for f, diff in changes.items():
        if f in ("punch_in", "punch_out"):
            db_changes[f] = {
                "from": ms_to_dt(diff["from"]) if diff["from"] is not None else None,
                "to": ms_to_dt(diff["to"]) if diff["to"] is not None else None,
            }
        else:
            db_changes[f] = diff

    history_entry = {
        "at": datetime.now(tz=UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": db_changes,
    }

    current_history_len = len(record.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            f"history.{current_history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": derived["work_hours"],
                "late_minutes": derived["late_minutes"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(status_code=409, detail="Record was concurrently modified")

    return serialize_attendance(updated)


# ---------------------------------------------------------------------------
# 9. Analytics Endpoints (MongoDB Aggregations)
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    db = get_db()
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    date_from_str, date_to_str, date_from, date_to = month_bounds(month)
    joined = DateType.fromisoformat(emp["joined_on"])
    effective_start = max(joined, date_from)
    working_days = working_days_in_range(effective_start, date_to)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": date_from_str, "$lte": date_to_str}}},
        {
            "$addFields": {
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "is_presence": {"$in": ["$status", list(PRESENCE_STATUSES)]},
            }
        },
        {"$addFields": {"is_weekday": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]}}},
        {
            "$addFields": {
                "present_contribution": {
                    "$cond": [
                        {"$and": ["$is_presence", "$is_weekday"]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                        0.0,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": "$present_contribution"},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    r = results[0] if results else {
        "present_days": 0.0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }

    present_days = round_half_up(r["present_days"], 2)
    attendance_pct = round_half_up(present_days / working_days * 100, 2) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(r["leave_days"]),
        "late_count": int(r["late_count"]),
        "total_late_minutes": int(r["total_late_minutes"]),
        "total_overtime_minutes": int(r["total_overtime_minutes"]),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
):
    db = get_db()
    date_from_str, date_to_str, _, date_to = month_bounds(month)
    date_to_str_inc = date_to.isoformat()

    emp_match: dict = {"joined_on": {"$lte": date_to_str_inc}}
    if department:
        emp_match["department"] = department

    emp_pipeline = [
        {"$match": emp_match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
    ]
    dept_headcounts = {
        r["_id"]: {"headcount": r["headcount"], "emp_codes": r["emp_codes"]}
        for r in db.employees.aggregate(emp_pipeline)
    }

    if not dept_headcounts:
        return {"month": month, "items": []}

    all_emp_codes = [code for info in dept_headcounts.values() for code in info["emp_codes"]]
    logs_match = {
        "emp_code": {"$in": all_emp_codes},
        "date": {"$gte": date_from_str, "$lte": date_to_str_inc},
    }

    logs_pipeline = [
        {"$match": logs_match},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        {"$unwind": "$emp_info"},
        {
            "$addFields": {
                "department": "$emp_info.department",
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "is_presence": {"$in": ["$status", list(PRESENCE_STATUSES)]},
            }
        },
        {"$addFields": {"is_weekday": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]}}},
        {
            "$addFields": {
                "present_contribution": {
                    "$cond": [
                        {"$and": ["$is_presence", "$is_weekday"]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                        0.0,
                    ]
                },
                "has_work_hours": {
                    "$and": ["$is_presence", {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "present_days": {"$sum": "$present_contribution"},
                "work_hours_sum": {"$sum": {"$cond": ["$has_work_hours", "$work_hours", 0]}},
                "work_hours_count": {"$sum": {"$cond": ["$has_work_hours", 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
    ]

    logs_by_dept = {r["_id"]: r for r in db.attendance_logs.aggregate(logs_pipeline)}

    items = []
    for dept, hc_info in sorted(dept_headcounts.items()):
        if department and dept != department:
            continue
        headcount = hc_info["headcount"]
        if headcount == 0:
            continue
        lr = logs_by_dept.get(dept, {})
        present_days = round_half_up(lr.get("present_days", 0.0), 2)
        wh_count = lr.get("work_hours_count", 0)
        avg_wh = round_half_up(lr["work_hours_sum"] / wh_count, 2) if wh_count > 0 else None
        items.append({
            "department": dept,
            "headcount": headcount,
            "present_days": present_days,
            "avg_work_hours": avg_wh,
            "late_count": int(lr.get("late_count", 0)),
            "total_late_minutes": int(lr.get("total_late_minutes", 0)),
            "leave_count": int(lr.get("leave_count", 0)),
            "on_duty_count": int(lr.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(default=None),
):
    db = get_db()
    date_from_str, date_to_str, _, _ = month_bounds(month)

    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": date_from_str, "$lte": date_to_str}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp_info",
            }
        },
        {"$match": {"emp_info": {"$ne": []}}},
        {"$unwind": "$emp_info"},
    ]

    if department:
        pipeline.append({"$match": {"emp_info.department": department}})

    pipeline += [
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp_info.name",
                "department": "$emp_info.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]

    try:
        items = list(db.attendance_logs.aggregate(pipeline))
    except Exception:
        # Fallback for environments lacking $setWindowFields
        fallback_pipe = pipeline[:6]
        all_rows = list(db.attendance_logs.aggregate(fallback_pipe))
        all_rows.sort(key=lambda r: (-r["total_late_minutes"], r["emp_code"]))
        ranked = []
        rank = 0
        prev_min = None
        for i, row in enumerate(all_rows):
            if row["total_late_minutes"] != prev_min:
                rank = i + 1
                prev_min = row["total_late_minutes"]
            row["rank"] = rank
            if rank <= limit:
                ranked.append(row)
        items = ranked

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_: str = Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    db = get_db()
    if to < from_:
        raise HTTPException(status_code=422, detail=[{"msg": "to must be >= from"}])

    d_from = DateType.fromisoformat(from_)
    d_to = DateType.fromisoformat(to)
    if (d_to - d_from).days > 91:
        raise HTTPException(status_code=422, detail=[{"msg": "Date range must not exceed 92 days"}])

    dept_count = db.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    # Native gap-filled daily series using database stages
    emp_docs = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    emp_codes = [e["emp_code"] for e in emp_docs]

    logs = list(
        db.attendance_logs.find(
            {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_, "$lte": to}},
            {"date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "_id": 0},
        )
    )

    logs_by_date: dict[str, list] = {}
    for log in logs:
        logs_by_date.setdefault(log["date"], []).append(log)

    items = []
    cur = d_from
    prev_rates: list[float] = []

    while cur <= d_to:
        date_str = cur.isoformat()
        is_working_day = cur.weekday() < 5
        headcount = sum(1 for e in emp_docs if e["joined_on"] <= date_str)

        day_logs = logs_by_date.get(date_str, [])
        present_count = 0.0
        late_count = 0
        for log in day_logs:
            if log["status"] in PRESENCE_STATUSES:
                present_count += 0.5 if log.get("half_day", False) else 1.0
            if log.get("late_minutes", 0) > 0:
                late_count += 1

        if is_working_day and headcount > 0:
            att_rate = round_half_up(present_count / headcount, 4)
            prev_rates.append(att_rate)
        else:
            att_rate = None

        window = prev_rates[-7:]
        moving_avg = round_half_up(sum(window) / len(window), 4) if window else None

        items.append({
            "date": date_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": round_half_up(present_count, 2),
            "late_count": late_count,
            "attendance_rate": att_rate,
            "moving_avg_7d": moving_avg,
        })
        cur += timedelta(days=1)

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# 10. Admin Explain Endpoint
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    from_: Optional[str] = Query(alias="from", default=None),
    to: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    VALID_ENDPOINTS = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in VALID_ENDPOINTS:
        raise HTTPException(status_code=422, detail=[{"msg": f"endpoint must be one of {sorted(VALID_ENDPOINTS)}"}])

    db = get_db()

    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        explain_doc = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail=[{"msg": "emp_code and month required"}])
        date_from_str, date_to_str, _, _ = month_bounds(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": date_from_str, "$lte": date_to_str}}}]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail=[{"msg": "month required"}])
        date_from_str, date_to_str, _, date_to_obj = month_bounds(month)
        emp_match: dict = {"joined_on": {"$lte": date_to_obj.isoformat()}}
        if department:
            emp_match["department"] = department
        explain_doc = db.command(
            "explain",
            {"find": "employees", "filter": emp_match},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_doc}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail=[{"msg": "month required"}])
        date_from_str, date_to_str, _, _ = month_bounds(month)
        pipeline = [{"$match": {"date": {"$gte": date_from_str, "$lte": date_to_str}, "late_minutes": {"$gt": 0}}}]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail=[{"msg": "department, from, and to required"}])
        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
        pipeline = [{"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_, "$lte": to}}}]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}
