# Employee Attendance & Analytics API

A REST API built with **FastAPI** and **MongoDB** for employee management, attendance tracking, attendance regularization, and workforce analytics. Developed as part of the HROne Software Engineer hiring assignment.

## Features

- **Employee Management** — Create employee records and filter employees by department.
- **Attendance Tracking** — Record punch-in and punch-out events, including overnight shifts.
- **Attendance Queries** — Retrieve attendance records with filtering and pagination.
- **Attendance Regularization** — Update attendance records with reasons, audit information, and change history.
- **Employee Analytics** — Generate monthly attendance summaries, including working days, leave days, late arrivals, overtime, and attendance percentage.
- **Department Analytics** — Summarize department-level attendance and working-hour metrics.
- **Late-Arrival Leaderboard** — Rank employees by late arrivals with consistent handling of ties.
- **Attendance Trends** — Generate daily department attendance trends with moving averages.
- **Query Plan Inspection** — Inspect MongoDB query execution plans to verify index usage.

## Technology Stack

- **Language:** Python 3.11+
- **API Framework:** FastAPI
- **Database:** MongoDB 6.0+
- **API Server:** Uvicorn
- **Data Processing:** MongoDB aggregation pipelines

## Prerequisites

Before running the application, ensure you have:

- Python 3.11 or later
- A reachable MongoDB 6.0+ instance
- Git
- The dependencies listed in `requirements.txt`

## Installation and Setup

### 1. Clone the repository

```bash
git clone <YOUR_GITHUB_REPOSITORY_URL>
cd <YOUR_PROJECT_DIRECTORY>
```

Replace the placeholders with your own repository URL and project directory.

### 2. Create a virtual environment

```bash
python -m venv .venv
```

Activate it using the command appropriate for your operating system.

**Windows PowerShell:**

```powershell
.\.venv\Scripts\Activate.ps1
```

**macOS/Linux:**

```bash
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

The application requires the following environment variables:

| Variable | Description |
|---|---|
| `MONGO_URI` | MongoDB connection URI |
| `MONGO_DB` | MongoDB database name |

Set these variables in your local shell or configure them through your local development environment.

**Example for Windows PowerShell using a local MongoDB instance:**

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

These are example local-development settings, not production credentials. For a hosted database, use your own connection configuration and keep credentials private.

If you use a local `.env` file, ensure it is excluded from version control. Only use that approach if your application or development setup loads the file correctly.

### 5. Start the application

From the repository root, run:

```bash
uvicorn app.main:app --port 8000
```

The API will be available at:

`http://127.0.0.1:8000`

## API Documentation

Once the application is running, access the interactive API documentation through Swagger UI.

- **Swagger UI:** `http://127.0.0.1:8000/docs`
- **Health endpoint:** `http://127.0.0.1:8000/health`

Use Swagger UI to inspect the available endpoints, review request and response schemas, and test API operations.

## Sample Data

If the repository includes `sample_seed.py`, you can use it to load the provided sample dataset:

```bash
python sample_seed.py
```

**Important:** The seed script may clear and replace the sample collections. Review its implementation and ensure you are connected to a disposable development database before running it. Do not run it against production or any database containing data you need to preserve.

## Implementation Notes

- Attendance timestamps are stored as UTC BSON datetimes in MongoDB and exposed through the API as integer epoch milliseconds.
- Calendar dates and shift times are represented as strings.
- Attendance calculations support overnight shifts.
- Attendance regularization maintains an audit trail of changes.
- MongoDB indexes are initialized at application startup.
- Analytics are implemented using MongoDB aggregation pipelines rather than fetching records for Python-side aggregation.
- Query explain functionality helps inspect execution plans and verify index usage.

## Project Documentation

Additional documentation is provided in the repository:

- **`REVIEW.md`** — Documents the identified starter-code defects and review findings.
- **`DECISIONS.md`** — Explains key implementation decisions and their rationale.