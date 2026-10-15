"""Employee Attendance & Analytics API (HROne engineering assignment)."""
from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # Real environment variables override values from .env.
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000, tz_aware=False)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
EPOCH = datetime(1970, 1, 1)
EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------- validation ---------------------------------

def fail422(message):
    raise HTTPException(status_code=422, detail=message)


def parse_date(value, field="date"):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        fail422(f"{field} must be YYYY-MM-DD")

    try:
        return date.fromisoformat(value)
    except ValueError:
        fail422(f"{field} must be a valid calendar date")


def parse_month(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        fail422("month must be YYYY-MM")

    start = date.fromisoformat(value + "-01")

    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)

    return start, end


def parse_hhmm(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        fail422("time must be HH:MM in 24-hour format")

    return value


def round_half_up(value, places=2):
    if places == 0:
        quant = Decimal("1")
    else:
        quant = Decimal("1").scaleb(-places)

    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def epoch_ms_to_dt(value):
    return EPOCH + timedelta(milliseconds=value)


def now_utc_seconds():
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def dt_to_epoch_ms(value):
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return int(value.timestamp() * 1000)


def to_ist(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return value.astimezone(IST).replace(microsecond=0)


def shift_minutes(value):
    h, m = map(int, value.split(":"))
    return h * 60 + m



def attendance_date(punch_in, emp):
    local = to_ist(punch_in)

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    result = local.date()

    if (
        shift_minutes(shift_end) <= shift_minutes(shift_start)
        and shift_minutes(shift_end) > local.hour * 60 + local.minute
    ):
        result -= timedelta(days=1)

    return result


def compute_late_minutes(punch_in, shift_start, attendance_day):
    local = to_ist(punch_in)
    day = date.fromisoformat(attendance_day)

    start_h, start_m = map(int, shift_start.split(":"))

    shift_start_dt = datetime.combine(
        day,
        time(start_h, start_m),
        tzinfo=IST
    )

    elapsed = (local - shift_start_dt).total_seconds()

    if elapsed > 600:
        return int(elapsed // 60)

    return 0


def shift_end_datetime(shift_end, shift_start, attendance_day):
    h, m = map(int, shift_end.split(":"))
    start_day = date.fromisoformat(attendance_day)

    if shift_minutes(shift_end) <= shift_minutes(shift_start):
        end_day = start_day + timedelta(days=1)
    else:
        end_day = start_day

    return datetime.combine(
        end_day,
        time(h, m),
        tzinfo=IST
    )


def derived_fields(punch_in, punch_out, emp, day):
    if punch_in:
        late = compute_late_minutes(
            punch_in,
            emp["shift_start"],
            day
        )
    else:
        late = 0

    if punch_in is None or punch_out is None:
        return {
            "late_minutes": late,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False
        }

    seconds = (punch_out - punch_in).total_seconds()
    hours = round_half_up(seconds / 3600, 2)

    end = shift_end_datetime(
        emp["shift_end"],
        emp["shift_start"],
        day
    )

    overtime_delta = (to_ist(punch_out) - end).total_seconds()

    if overtime_delta >= 1800:
        overtime = int(overtime_delta // 60)
    else:
        overtime = 0

    return {
        "late_minutes": late,
        "work_hours": hours,
        "overtime_minutes": overtime,
        "half_day": hours < 4.50
    }


def serialize_history(history):
    out = []

    for entry in history or []:
        changes = {}

        for key, pair in (entry.get("changes") or {}).items():

            if key in ("punch_in", "punch_out"):
                from_value = dt_to_epoch_ms(pair.get("from"))
                to_value = dt_to_epoch_ms(pair.get("to"))
            else:
                from_value = pair.get("from")
                to_value = pair.get("to")

            changes[key] = {
                "from": from_value,
                "to": to_value
            }

        out.append({
            "at": dt_to_epoch_ms(entry.get("at")),
            "by": entry.get("by", ""),
            "reason": entry.get("reason", ""),
            "changes": changes
        })

    return out


def serialize_attendance(doc):
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": serialize_history(doc.get("history", []))
    }


def serialize_employee(doc):
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc["created_at"])
    }


def validate_epoch(value, field="timestamp"):
    if type(value) is not int or value < EPOCH_MIN or value > EPOCH_MAX:
        fail422(
            f"{field} must be an integer epoch timestamp in milliseconds"
        )



def validate_punch_order(punch_in, punch_out):
    if punch_in and punch_out:
        delta = (punch_out - punch_in).total_seconds()

        if delta <= 0 or delta > 24 * 3600:
            fail422("punch_out must be after punch_in and within 24 hours")



# models 

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift(cls, v):
        return parse_hhmm(v)

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        parse_date(v, "joined_on")
        return v

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            fail422("shift_start must differ from shift_end")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, v):
        if v is not None: validate_epoch(v, "punched_at")
        return v

    @model_validator(mode="after")
    def reject_explicit_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            fail422("punched_at cannot be null")
        return self


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, v):
        if v is not None: validate_epoch(v, "punched_at")
        return v

    @model_validator(mode="after")
    def reject_explicit_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            fail422("punched_at cannot be null")
        return self


class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_times(cls, v):
        if v is not None: validate_epoch(v)
        return v

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                fail422(f"{field} cannot be null")
        return self


# aggregation

def mongo_round_half_up(expression, places):

    scale = 10 ** places

    return {
        "$divide": [
            {
                "$floor": {
                    "$add": [
                        {"$multiply": [expression, scale]},
                        0.5
                    ]
                }
            },
            scale
        ]
    }


def month_bounds(month):
    start, next_month = parse_month(month)

    return start, next_month, next_month - timedelta(days=1)


def attendance_filter(emp_code=None, date_from=None, date_to=None, status=None):
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

    return q




def attendance_list_pipeline(q, page, page_size):
    return [
        {"$match": q},
        {"$sort": {"date": -1, "emp_code": 1}},
        {
            "$facet": {
                "items": [
                    {"$skip": (page - 1) * page_size},
                    {"$limit": page_size},
                    {"$project": {"_id": 0}}
                ],
                "count": [
                    {"$count": "total"}
                ]
            }
        }
    ]


def monthly_pipeline(emp_code, month):
    start, next_month, last_day = month_bounds(month)

    first_expr = {
        "$dateFromString": {
            "dateString": "$joined_on"
        }
    }

    month_start = datetime.combine(start, time.min)
    month_end = datetime.combine(next_month, time.min)

    return [
        {
            "$match": {
                "emp_code": emp_code
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start.isoformat()]},
                                    {"$lt": ["$date", next_month.isoformat()]}
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                            "overtime_minutes": 1
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$set": {
                "working_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$range": [
                                    0,
                                    {
                                        "$dateDiff": {
                                            "startDate": month_start,
                                            "endDate": month_end,
                                            "unit": "day"
                                        }
                                    }
                                ]
                            },
                            "as": "offset",
                            "in": {
                                "$let": {
                                    "vars": {
                                        "d": {
                                            "$dateAdd": {
                                                "startDate": month_start,
                                                "unit": "day",
                                                "amount": "$$offset"
                                            }
                                        }
                                    },
                                    "in": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$gte": ["$$d", first_expr]},
                                                    {
                                                        "$in": [
                                                            {"$dayOfWeek": "$$d"},
                                                            [2, 3, 4, 5, 6]
                                                        ]
                                                    }
                                                ]
                                            },
                                            1,
                                            0
                                        ]
                                    }
                                }
                            }
                        }
                    }
                },
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$l.status",
                                                    list(PRESENCE)
                                                ]
                                            },
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$l.date"
                                                            }
                                                        }
                                                    },
                                                    [2, 3, 4, 5, 6]
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$$l.half_day", True]},
                                            0.5,
                                            1
                                        ]
                                    },
                                    0
                                ]
                            }
                        }
                    }
                },
                "leave_days": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {
                                "$eq": ["$$l.status", "LEAVE"]
                            }
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$l.late_minutes", 0]},
                                    0
                                ]
                            }
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$ifNull": ["$$l.late_minutes", 0]
                            }
                        }
                    }
                },
                "total_overtime_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$ifNull": ["$$l.overtime_minutes", 0]
                            }
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": 1,
                "present_days": mongo_round_half_up("$present_days", 2),
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$working_days", 0]},
                        mongo_round_half_up(
                            {
                                "$multiply": [
                                    {
                                        "$divide": [
                                            "$present_days",
                                            "$working_days"
                                        ]
                                    },
                                    100
                                ]
                            },
                            4
                        ),
                        None
                    ]
                }
            }
        }
    ]


def department_summary_pipeline(month, department):
    start, next_month, last_day = month_bounds(month)

    employee_match = {
        "joined_on": {
            "$lte": last_day.isoformat()
        }
    }

    if department is not None:
        employee_match["department"] = department

    return [
        {
            "$match": employee_match
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start.isoformat()]},
                                    {"$lt": ["$date", next_month.isoformat()]}
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "date": 1,
                            "status": 1,
                            "half_day": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                            "overtime_minutes": 1
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$set": {
                "_weekday": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": {
                                        "$ifNull": [
                                            "$logs.date",
                                            start.isoformat()
                                        ]
                                    }
                                }
                            }
                        },
                        [2, 3, 4, 5, 6]
                    ]
                },
                "_present": {
                    "$in": [
                        {"$ifNull": ["$logs.status", ""]},
                        list(PRESENCE)
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code"
                },
                "department": {
                    "$first": "$department"
                },
                "headcount": {
                    "$first": 1
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_weekday",
                                    "$_present"
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$ifNull": ["$logs.late_minutes", 0]},
                                    0
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": ["$logs.late_minutes", 0]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0
                        ]
                    }
                },
                "work_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_present",
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            0
                        ]
                    }
                },
                "work_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_present",
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": "$headcount"},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "work_sum": {"$sum": "$work_sum"},
                "work_count": {"$sum": "$work_count"}
            }
        },
        {
            "$match": {
                "headcount": {"$gt": 0}
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": mongo_round_half_up(
                    "$present_days",
                    2
                ),
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_count", 0]},
                        mongo_round_half_up(
                            {
                                "$divide": [
                                    "$work_sum",
                                    "$work_count"
                                ]
                            },
                            2
                        ),
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {
            "$sort": {
                "department": 1
            }
        }
    ]



def leaderboard_pipeline(month, limit, department):
    start, next_month, _ = month_bounds(month)

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": start.isoformat(),
                    "$lt": next_month.isoformat()
                },
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"}
    ]

    if department is not None:
        pipeline.append({
            "$match": {
                "employee.department": department
            }
        })

    pipeline += [
        {
            "$set": {
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department"
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
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ]

    return pipeline


def trend_pipeline(department, start_s, end_s):
    start, end = parse_date(start_s), parse_date(end_s)

    start_dt = datetime.combine(start, time.min)
    days = (end - start).days + 1

    return [
        {
            "$match": {
                "department": department
            }
        },
        {
            "$set": {
                "_joined_dt": {
                    "$dateFromString": {
                        "dateString": "$joined_on"
                    }
                },
                "_days": {
                    "$map": {
                        "input": {"$range": [0, days]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": start_dt,
                                "unit": "day",
                                "amount": "$$offset"
                            }
                        }
                    }
                }
            }
        },
        {"$unwind": "$_days"},
        {
            "$set": {
                "_day_s": {
                    "$dateToString": {
                        "date": "$_days",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC"
                    }
                },
                "_eligible": {
                    "$gte": ["$_days", "$_joined_dt"]
                },
                "_weekday": {
                    "$in": [
                        {"$dayOfWeek": "$_days"},
                        [2, 3, 4, 5, 6]
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "code": "$emp_code",
                    "day": "$_day_s"
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$eq": ["$date", "$$day"]}
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1
                        }
                    }
                ],
                "as": "_logs"
            }
        },
        {
            "$unwind": {
                "path": "$_logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$group": {
                "_id": "$_day_s",
                "_date": {"$first": "$_days"},
                "is_working_day": {"$first": "$_weekday"},
                "headcount": {
                    "$sum": {
                        "$cond": ["$_eligible", 1, 0]
                    }
                },
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_eligible",
                                    {
                                        "$in": [
                                            {
                                                "$ifNull": [
                                                    "$_logs.status",
                                                    ""
                                                ]
                                            },
                                            list(PRESENCE)
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$_logs.half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_eligible",
                                    {
                                        "$gt": [
                                            {
                                                "$ifNull": [
                                                    "$_logs.late_minutes",
                                                    0
                                                ]
                                            },
                                            0
                                        ]
                                    }
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        mongo_round_half_up(
                            {"$divide": ["$present_count", "$headcount"]},
                            4
                        ),
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]}
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": mongo_round_half_up("$present_count", 2),
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": mongo_round_half_up(
                    "$moving_avg_7d",
                    4
                )
            }
        },
        {"$sort": {"date": 1}}
    ]

# --------------------------------- indexes ----------------------------------

def ensure_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employees_emp_code"
    )

    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employees_department_joined"
    )

    db.employees.create_index(
        [("joined_on", ASCENDING)],
        name="ix_employees_joined"
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_emp_date"
    )

    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_emp"
    )

    db.attendance_logs.create_index(
        [
            ("status", ASCENDING),
            ("date", DESCENDING),
            ("emp_code", ASCENDING)
        ],
        name="ix_attendance_status_date_emp"
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_emp_punchin"
    )

    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)],
        name="ix_attendance_date_late"
    )


@app.on_event("startup")
def startup():
    # Startup should fail loudly if MongoDB is unreachable;
    # /health also checks readiness.
    client.admin.command("ping")
    ensure_indexes()


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}

    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_seconds()

    try:
        db.employees.insert_one(doc)

    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if department is not None:
        q = {"department": department}
    else:
        q = {}

    total = db.employees.count_documents(q)

    docs = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_employee(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(404, "unknown employee")

    if body.punched_at is not None:
        ts = epoch_ms_to_dt(body.punched_at)
    else:
        ts = now_utc_seconds()

    day = attendance_date(ts, emp).isoformat()

    derived = derived_fields(ts, None, emp, day)

    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        db.attendance_logs.insert_one(doc)

    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_attendance(doc)



@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(404, "unknown employee")

    if body.punched_at is not None:
        ts = epoch_ms_to_dt(body.punched_at)
    else:
        ts = now_utc_seconds()

    doc = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {
                "$ne": None,
                "$lte": ts
            }
        },
        sort=[("punch_in", DESCENDING)]
    )

    if not doc:
        raise HTTPException(404, "no punch-in found")

    if doc.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")

    validate_punch_order(doc.get("punch_in"), ts)

    derived = derived_fields(
        doc["punch_in"],
        ts,
        emp,
        doc["date"]
    )

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": doc["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": ts,
                **derived
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if updated is None:
        raise HTTPException(409, "record already punched out")

    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if date_from:
        parse_date(date_from, "date_from")

    if date_to:
        parse_date(date_to, "date_to")

    if date_from and date_to and date_from > date_to:
        fail422("date_from must be on or before date_to")

    if status is not None and status not in STATUSES:
        fail422("invalid status")

    q = attendance_filter(
        emp_code,
        date_from,
        date_to,
        status
    )

    pipeline = attendance_list_pipeline(
        q,
        page,
        page_size
    )

    result = list(db.attendance_logs.aggregate(pipeline))[0]

    return {
        "items": [
            serialize_attendance(d)
            for d in result["items"]
        ],
        "total": (
            result["count"][0]["total"]
            if result["count"]
            else 0
        ),
        "page": page,
        "page_size": page_size
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code, date, body: RegularizeIn):
    parse_date(date)

    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp:
        raise HTTPException(404, "unknown employee")

    current = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })

    if not current:
        raise HTTPException(404, "attendance record not found")

    if "status" in body.model_fields_set:
        new_status = body.status
    else:
        new_status = current["status"]

    if "punch_in" in body.model_fields_set:
        new_in = epoch_ms_to_dt(body.punch_in)
    else:
        new_in = current.get("punch_in")

    if "punch_out" in body.model_fields_set:
        new_out = epoch_ms_to_dt(body.punch_out)
    else:
        new_out = current.get("punch_out")

    if new_status in ("ABSENT", "LEAVE"):
        if (
            "punch_in" in body.model_fields_set
            or "punch_out" in body.model_fields_set
        ):
            fail422("punch times cannot be supplied with ABSENT or LEAVE")

        new_in = None
        new_out = None

    elif new_in is None:
        fail422("presence status requires punch_in")

    if new_in is not None:
        if attendance_date(new_in, emp).isoformat() != date:
            fail422("punch_in must belong to the record attendance date")

    validate_punch_order(new_in, new_out)

    if new_status in ("ABSENT", "LEAVE"):
        new_derived = {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False
        }
    else:
        new_derived = derived_fields(
            new_in,
            new_out,
            emp,
            date
        )

    before = {
        "status": current.get("status"),
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out"),
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False)
    }

    after = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **new_derived
    }

    changes = {}

    for key, value in after.items():
        old = before.get(key)

        if key in ("late_minutes", "overtime_minutes"):
            old = int(old or 0)

        if key == "half_day":
            old = bool(old)

        if old != value:
            changes[key] = {
                "from": old,
                "to": value
            }

    if not changes:
        fail422("request does not change the record")

    history_entry = {
        "at": now_utc_seconds(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    # Optimistic concurrency: only update if the fields we read have not changed.
    # A losing edit gets 409.
    compare = {
        "emp_code": emp_code,
        "date": date,
        "status": current.get("status"),
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out")
    }

    updated = db.attendance_logs.find_one_and_update(
        compare,
        {
            "$set": after,
            "$push": {
                "history": history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if updated is None:
        raise HTTPException(
            409,
            "record changed concurrently; retry regularization"
        )

    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code,
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    )
):
    parse_month(month)

    if not db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 1}
    ):
        raise HTTPException(404, "unknown employee")

    docs = list(
        db.employees.aggregate(
            monthly_pipeline(emp_code, month)
        )
    )

    if not docs:
        raise HTTPException(404, "unknown employee")

    return docs[0]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    ),
    department: Optional[str] = None
):
    parse_month(month)

    items = list(
        db.employees.aggregate(
            department_summary_pipeline(month, department)
        )
    )

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    ),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    parse_month(month)

    items = list(
        db.attendance_logs.aggregate(
            leaderboard_pipeline(month, limit, department)
        )
    )

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")

    if end < start:
        fail422("to must be on or after from")

    if (end - start).days + 1 > 92:
        fail422("date range cannot exceed 92 days")

    if not db.employees.find_one(
        {"department": department},
        {"_id": 1}
    ):
        raise HTTPException(404, "unknown department")

    items = list(
        db.employees.aggregate(
            trend_pipeline(department, from_date, to_date)
        )
    )

    return {
        "department": department,
        "items": items
    }


# ----------------------------- explain endpoint -----------------------------

def run_explain(collection, pipeline):
    return db.command({
        "explain": {
            "aggregate": collection.name,
            "pipeline": pipeline,
            "cursor": {}
        },
        "verbosity": "executionStats"
    })


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend"
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to")
):
    if endpoint == "attendance_list":
        if date_from:
            parse_date(date_from, "date_from")

        if date_to:
            parse_date(date_to, "date_to")

        if date_from and date_to and date_from > date_to:
            fail422("date_from must be on or before date_to")

        if status is not None and status not in STATUSES:
            fail422("invalid status")

        q = attendance_filter(
            emp_code,
            date_from,
            date_to,
            status
        )

        pipeline = attendance_list_pipeline(
            q,
            page,
            page_size
        )

        coll = db.attendance_logs

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            fail422("emp_code and month are required")

        parse_month(month)

        pipeline = monthly_pipeline(emp_code, month)
        coll = db.employees

    elif endpoint == "department_summary":
        if not month:
            fail422("month is required")

        parse_month(month)

        pipeline = department_summary_pipeline(
            month,
            department
        )

        coll = db.employees

    elif endpoint == "late_leaderboard":
        if not month:
            fail422("month is required")

        parse_month(month)

        pipeline = leaderboard_pipeline(
            month,
            limit,
            department
        )

        coll = db.attendance_logs

    else:
        if not department or not from_date or not to_date:
            fail422("department, from and to are required")

        start = parse_date(from_date, "from")
        end = parse_date(to_date, "to")

        if end < start or (end - start).days + 1 > 92:
            fail422("invalid trend date range")

        pipeline = trend_pipeline(
            department,
            from_date,
            to_date
        )

        coll = db.employees

    try:
        result = run_explain(coll, pipeline)

    except PyMongoError as exc:
        raise HTTPException(
            500,
            f"explain failed: {exc.__class__.__name__}"
        )

    # Extended BSON values in explain output (e.g. ObjectIds)
    # must be JSON serializable.
    import json
    from bson import json_util

    return {
        "endpoint": endpoint,
        "collection": coll.name,
        "explain": json.loads(json_util.dumps(result))
    }