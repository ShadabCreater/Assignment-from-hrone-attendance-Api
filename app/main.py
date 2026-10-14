"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# Real environment variables take precedence over .env
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at startup."""
    # employees collection
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int) -> Optional[float]:
    """Round a float to N decimals using half-up rounding (R8)."""
    if val is None:
        return None
    d = Decimal(str(val))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a UTC or timezone-aware datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ms_to_utc_dt(ms: Optional[int]) -> Optional[datetime]:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds (R1)."""
    if ms is None:
        return None
    # Truncate to whole seconds
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def parse_hh_mm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(hour=h, minute=m, second=0)


def get_attendance_date(punch_in_ms: int, shift_start_str: str, shift_end_str: str) -> str:
    """
    R1: Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Overnight shifts (shift_end <= shift_start): a punch-in earlier than shift_end
    belongs to the shift that started the previous day.
    """
    dt_ist = datetime.fromtimestamp(punch_in_ms / 1000, tz=IST)
    start_t = parse_hh_mm(shift_start_str)
    end_t = parse_hh_mm(shift_end_str)

    if end_t <= start_t:
        # Overnight shift
        if dt_ist.time() < end_t:
            return (dt_ist.date() - timedelta(days=1)).isoformat()

    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_ms: int, attendance_date_str: str, shift_start_str: str) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start.
    """
    dt_ist = datetime.fromtimestamp((punch_in_ms // 1000), tz=IST)
    start_t = parse_hh_mm(shift_start_str)
    d = date.fromisoformat(attendance_date_str)
    shift_start_dt = datetime.combine(d, start_t, tzinfo=IST)

    diff_sec = (dt_ist - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = (punch_out_ms // 1000) - (punch_in_ms // 1000)
    hours = Decimal(str(diff_sec)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day is true when rounded work_hours < 4.50.
    """
    return work_hours is not None and work_hours < 4.50


def compute_overtime(punch_out_ms: int, attendance_date_str: str, shift_start_str: str, shift_end_str: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    Overnight shifts: shift_end falls on the next calendar day.
    """
    dt_ist = datetime.fromtimestamp((punch_out_ms // 1000), tz=IST)
    start_t = parse_hh_mm(shift_start_str)
    end_t = parse_hh_mm(shift_end_str)
    d = date.fromisoformat(attendance_date_str)

    if end_t <= start_t:
        # Overnight shift ends on date + 1 day
        end_d = d + timedelta(days=1)
    else:
        end_d = d

    shift_end_dt = datetime.combine(end_d, end_t, tzinfo=IST)
    diff_sec = (dt_ist - shift_end_dt).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format a MongoDB attendance document to match AttendanceRecord schema."""
    history = []
    for h in doc.get("history", []):
        changes = {}
        for k, v in h.get("changes", {}).items():
            from_val = v.get("from")
            to_val = v.get("to")
            if isinstance(from_val, datetime):
                from_val = to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = to_epoch_ms(to_val)
            changes[k] = {"from": from_val, "to": to_val}

        history.append({
            "at": to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes,
        })

    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format a MongoDB employee document to match Employee schema."""
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


def make_json_serializable(obj: Any) -> Any:
    """Recursively convert BSON/MongoDB objects into JSON serializable structures."""
    if isinstance(obj, dict):
        return {k: make_json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [make_json_serializable(x) for x in obj]
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return str(obj)


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: PresenceStatusType = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe: 200 only when MongoDB answers; else 503."""
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create an employee."""
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Record a punch-in."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    punched_at_ms = body.punched_at if body.punched_at is not None else now_ms
    # Truncate to whole seconds
    punched_at_ms = (punched_at_ms // 1000) * 1000

    attendance_date = get_attendance_date(punched_at_ms, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punched_at_ms, attendance_date, emp["shift_start"])
    punch_in_dt = ms_to_utc_dt(punched_at_ms)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """Record a punch-out."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    punched_at_ms = body.punched_at if body.punched_at is not None else now_ms
    punched_at_ms = (punched_at_ms // 1000) * 1000
    punch_out_dt = ms_to_utc_dt(punched_at_ms)

    # Closes the employee's most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="That record is already punched out")

    punch_in_ms = to_epoch_ms(record["punch_in"])
    if punch_in_ms is None:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if punched_at_ms <= punch_in_ms:
        raise HTTPException(status_code=422, detail="punched_at must be strictly greater than punch_in")

    if (punched_at_ms - punch_in_ms) > (24 * 3600 * 1000):
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_ms, punched_at_ms)
    half_day = compute_half_day(work_hours)
    overtime_minutes = compute_overtime(punched_at_ms, record["date"], emp["shift_start"], emp["shift_end"])

    # Atomic update to guarantee race-safety under concurrency
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="That record is already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusType] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date descending, then emp_code ascending."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q: Dict[str, Any] = {}
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
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """Regularize (manually correct) one day's record."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    target_status = body.status if body.status is not None else record["status"]

    # Rule: setting status ABSENT or LEAVE clears both punch times,
    # so supplying punch_in or punch_out with it is 422
    if body.status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch times when setting status to ABSENT or LEAVE",
            )

    if target_status in ["ABSENT", "LEAVE"]:
        new_punch_in_ms = None
        new_punch_out_ms = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # A presence status requires a punch_in (supplied or already stored)
        if body.punch_in is not None:
            new_punch_in_ms = (body.punch_in // 1000) * 1000
        else:
            new_punch_in_ms = to_epoch_ms(record.get("punch_in"))

        if new_punch_in_ms is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        # punch_in must stay on the record's attendance date (R1)
        calc_date = get_attendance_date(new_punch_in_ms, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        # punch_out
        if body.punch_out is not None:
            new_punch_out_ms = (body.punch_out // 1000) * 1000
        else:
            new_punch_out_ms = to_epoch_ms(record.get("punch_out"))

        # If punch_out exists, validate times
        if new_punch_out_ms is not None:
            if new_punch_out_ms <= new_punch_in_ms:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if (new_punch_out_ms - new_punch_in_ms) > (24 * 3600 * 1000):
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

            new_work_hours = compute_work_hours(new_punch_in_ms, new_punch_out_ms)
            new_half_day = compute_half_day(new_work_hours)
            new_overtime_minutes = compute_overtime(
                new_punch_out_ms, date, emp["shift_start"], emp["shift_end"]
            )
        else:
            new_work_hours = None
            new_half_day = False
            new_overtime_minutes = 0

        new_late_minutes = compute_late_minutes(new_punch_in_ms, date, emp["shift_start"])

    new_punch_in_dt = ms_to_utc_dt(new_punch_in_ms)
    new_punch_out_dt = ms_to_utc_dt(new_punch_out_ms)

    # Detect changes compared to existing record
    changes_mongo: Dict[str, Dict[str, Any]] = {}
    if target_status != record["status"]:
        changes_mongo["status"] = {"from": record["status"], "to": target_status}
    if new_punch_in_dt != record.get("punch_in"):
        changes_mongo["punch_in"] = {"from": record.get("punch_in"), "to": new_punch_in_dt}
    if new_punch_out_dt != record.get("punch_out"):
        changes_mongo["punch_out"] = {"from": record.get("punch_out"), "to": new_punch_out_dt}
    if new_work_hours != record.get("work_hours"):
        changes_mongo["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
    if new_late_minutes != record.get("late_minutes", 0):
        changes_mongo["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_minutes}
    if new_overtime_minutes != record.get("overtime_minutes", 0):
        changes_mongo["overtime_minutes"] = {
            "from": record.get("overtime_minutes", 0),
            "to": new_overtime_minutes,
        }
    if new_half_day != record.get("half_day", False):
        changes_mongo["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}

    if not changes_mongo:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_mongo,
    }

    # Optimistic locking: check existing history array so concurrent edits don't overwrite each other
    existing_history = record.get("history", [])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": existing_history},
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in_dt,
                "punch_out": new_punch_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent edit conflict")

    return format_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    joined_on = emp["joined_on"]

    # R7: Working days Mon-Fri, counted from joined_on for mid-month joiners
    working_days = 0
    for day_num in range(1, num_days + 1):
        day_str = f"{month}-{day_num:02d}"
        if day_str >= joined_on:
            d_obj = date(year, m, day_num)
            if d_obj.weekday() < 5:  # Monday to Friday
                working_days += 1

    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dayOfWeek": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                    }
                },
            }
        },
        {
            "$project": {
                "is_weekday": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                "is_present_status": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", "$is_present_status"]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        data = res[0]
        present_days = float(data.get("present_days", 0.0))
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late_minutes = int(data.get("total_late_minutes", 0))
        total_overtime_minutes = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        pct = (present_days / working_days) * 100
        attendance_pct = round_half_up(pct, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    """Department summary for a month via MongoDB aggregation across both collections."""
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emps": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$in": ["$emp_code", "$$emps"]},
                            "date": {"$gte": start_date, "$lte": end_date},
                        }
                    },
                    {
                        "$project": {
                            "emp_code": 1,
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "dayOfWeek": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date",
                                        "format": "%Y-%m-%d",
                                    }
                                }
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$log.dayOfWeek", [2, 3, 4, 5, 6]]},
                                            {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": ["$$log.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "work_hours_list": {
                    "$filter": {
                        "input": {
                            "$map": {
                                "input": "$logs",
                                "as": "log",
                                "in": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$$log.work_hours", None]},
                                            ]
                                        },
                                        "$$log.work_hours",
                                        None,
                                    ]
                                },
                            }
                        },
                        "as": "wh",
                        "cond": {"$ne": ["$$wh", None]},
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$avg": "$work_hours_list"},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        avg_wh = doc.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(float(avg_wh), 2)
        items.append({
            "department": doc["department"],
            "headcount": int(doc["headcount"]),
            "present_days": float(doc["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(doc["late_count"]),
            "total_late_minutes": int(doc["total_late_minutes"]),
            "leave_count": int(doc["leave_count"]),
            "on_duty_count": int(doc["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """Late-comers leaderboard for a month using MongoDB standard competition ranking ($rank)."""
    start_date = f"{month}-01"
    end_date = f"{month}-31"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
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
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    items = []
    for doc in db.attendance_logs.aggregate(pipeline):
        items.append({
            "rank": int(doc["rank"]),
            "emp_code": doc["emp_code"],
            "name": doc["name"],
            "department": doc["department"],
            "total_late_minutes": int(doc["total_late_minutes"]),
            "late_count": int(doc["late_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend for a department with gap-filling ($densify) and 7-day moving average."""
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must be >= from")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # 404 for unknown department (no employees)
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    dt_from = datetime.combine(d_from, time.min)
    dt_to_bound = datetime.combine(d_to + timedelta(days=1), time.min)

    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [dt_from, dt_to_bound],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "joined_on": 1}},
                ],
                "as": "dept_emps",
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "present_count": 1,
                "late_count": 1,
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = []
    for doc in db.attendance_logs.aggregate(pipeline):
        att_rate = doc.get("attendance_rate")
        if att_rate is not None:
            att_rate = round_half_up(float(att_rate), 4)

        mov_avg = doc.get("moving_avg_7d_raw")
        if mov_avg is not None:
            mov_avg = round_half_up(float(mov_avg), 4)

        items.append({
            "date": doc["date"],
            "is_working_day": bool(doc["is_working_day"]),
            "headcount": int(doc["headcount"]),
            "present_count": float(doc["present_count"]),
            "late_count": int(doc["late_count"]),
            "attendance_rate": att_rate,
            "moving_avg_7d": mov_avg,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(
        ...,
        pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$",
    ),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusType] = Query(None),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """MongoDB explain with executionStats verbosity for an endpoint's main query or aggregation."""
    try:
        if endpoint == "attendance_list":
            q: Dict[str, Any] = {}
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

            find_cmd = {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            }
            explain_doc = db.command("explain", find_cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": make_json_serializable(explain_doc),
            }

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(status_code=422, detail="emp_code and month are required")

            pipeline = [
                {
                    "$match": {
                        "emp_code": emp_code,
                        "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
                    }
                }
            ]
            explain_doc = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": make_json_serializable(explain_doc),
            }

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(status_code=422, detail="month is required")

            year, m = map(int, month.split("-"))
            num_days = calendar.monthrange(year, m)[1]
            end_date = f"{month}-{num_days:02d}"

            match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
            if department:
                match_stage["department"] = department

            pipeline = [
                {"$match": match_stage},
                {
                    "$group": {
                        "_id": "$department",
                        "headcount": {"$sum": 1},
                        "emp_codes": {"$push": "$emp_code"},
                    }
                },
            ]
            explain_doc = db.command(
                "explain",
                {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
            return {
                "endpoint": endpoint,
                "collection": "employees",
                "explain": make_json_serializable(explain_doc),
            }

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(status_code=422, detail="month is required")

            pipeline = [
                {
                    "$match": {
                        "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
                        "late_minutes": {"$gt": 0},
                    }
                }
            ]
            explain_doc = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": make_json_serializable(explain_doc),
            }

        elif endpoint == "department_trend":
            if not department or not from_date or not to_date:
                raise HTTPException(status_code=422, detail="department, from, and to are required")

            pipeline = [
                {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "emp_code",
                        "foreignField": "emp_code",
                        "as": "emp",
                    }
                },
                {"$unwind": "$emp"},
                {"$match": {"emp.department": department}},
            ]
            explain_doc = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": make_json_serializable(explain_doc),
            }

        else:
            raise HTTPException(status_code=422, detail=f"Unknown endpoint '{endpoint}'")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
