"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
from decimal import Decimal, ROUND_HALF_UP
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from contextlib import asynccontextmanager
from pymongo.errors import DuplicateKeyError

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, StrictInt
from pymongo import MongoClient

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000


def parse_epoch_ms(value: Optional[int]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)

    if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(
            status_code=422,
            detail="Timestamp must be valid epoch milliseconds",
        )

    try:
        return datetime.fromtimestamp(
            value // 1000,
            tz=timezone.utc,
        )
    except (ValueError, OverflowError, OSError):
        raise HTTPException(
            status_code=422,
            detail="Invalid timestamp",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employees_emp_code"
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_date"
    )

    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="idx_attendance_date_emp"
    )

    yield

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    punch_in = punch_in.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    candidates = [
        punch_in.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=d)
        for d in (-1, 0)
    ]
    start = min(candidates, key=lambda s: abs(punch_in - s))
    minutes = int((punch_in - start).total_seconds() // 60)
    return minutes if minutes > 10 else 0

def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime
) -> float:
    seconds = (punch_out - punch_in).total_seconds()

    hours = Decimal(str(seconds)) / Decimal("3600")

    rounded_hours = hours.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )

    return float(rounded_hours)



def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    sh, sm= map(int, shift_start.split(":"))
    eh, em=map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=eh, minute=em, tzinfo=IST)
    if(eh,em)<= (sh,sm):
        end+=timedelta(days=1)
    punch_out=punch_out.astimezone(IST)    
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def to_epoch_ms(value):
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return int(value.timestamp()) * 1000


def attendance_response(record):
    if record is None:
        return None

    result = {
        "emp_code": record["emp_code"],
        "date": record["date"],
        "status": record["status"],
        "punch_in": to_epoch_ms(record.get("punch_in")),
        "punch_out": to_epoch_ms(record.get("punch_out")),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
        "history": []
    }

    for entry in record.get("history", []):
        changes = {}

        for field, change in entry.get("changes", {}).items():
            old_value = change.get("from")
            new_value = change.get("to")

            if field in ("punch_in", "punch_out"):
                old_value = to_epoch_ms(old_value)
                new_value = to_epoch_ms(new_value)

            changes[field] = {
                "from": old_value,
                "to": new_value
            }

        result["history"].append({
            "at": to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": changes
        })

    return result


def employee_response(record):
    return {
        "emp_code": record["emp_code"],
        "name": record["name"],
        "email": record["email"],
        "department": record["department"],
        "shift_start": record["shift_start"],
        "shift_end": record["shift_end"],
        "joined_on": record["joined_on"],
        "created_at": to_epoch_ms(record["created_at"])
    }

   

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return doc


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = parse_epoch_ms(body.punched_at).astimezone(IST)

    d = ts.date().isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        res = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    saved = db.attendance_logs.find_one({"_id": res.inserted_id})
    return attendance_response(saved)

HALF_DAY_HOURS = 4.50   # TODO: set from the spec

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = parse_epoch_ms(body.punched_at).astimezone(IST)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_out": None},
        sort=[("date", -1)],
    )
    if rec is None:
        raise HTTPException(409, "no open punch-in for this employee")

    punch_in_ts = rec["punch_in"]
    if punch_in_ts.tzinfo is None:                 # safety net
        punch_in_ts = punch_in_ts.replace(tzinfo=timezone.utc)

    if ts <= punch_in_ts:
        raise HTTPException(422, "punch_out must be after punch_in")
    if ts - punch_in_ts > timedelta(hours=24):
        raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_ts, ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], rec["date"])

    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},     # guard against a double punch-out race
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": work_hours < HALF_DAY_HOURS,
        }},
    )
    if res.modified_count == 0:
        raise HTTPException(409, "already punched out")

    rec = db.attendance_logs.find_one({"_id": rec["_id"]})
    return attendance_response(rec)
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
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
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = []
    for d in cursor:
     items.append(attendance_response(d))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
