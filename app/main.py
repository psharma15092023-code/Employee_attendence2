"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from contextlib import asynccontextmanager
from pymongo.errors import DuplicateKeyError

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


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

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    sh, sm= map(int, shift_start.split(":"))
    eh, em=map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=eh, minute=em, tzinfo=IST)
    if(eh,em)<= (sh,sm):
        end+=timedelta(days=1)
    punch_out=punch_out.astimezone(IST)    
    return max(0, int((punch_out - end).total_seconds() // 60))


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
    punched_at: Optional[int] = None
    status: str = "PRESENT"
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees( department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=IST)
    else:
        ts = datetime.now(IST)
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
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc

HALF_DAY_HOURS = 4.0   # TODO: set from the spec

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=IST)
    else:
        ts = datetime.now(IST)

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
    rec["id"] = str(rec.pop("_id"))
    return rec
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
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
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


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
