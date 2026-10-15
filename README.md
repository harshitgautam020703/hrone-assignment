# Employee Attendance & Analytics API

A production-grade FastAPI + MongoDB service for employee attendance tracking, manual regularization with an audit trail, and analytics aggregation pipelines.

## Architecture

```
HTTP Clients (Mobile / Web / Bio-device)
                │
                ▼ [FastAPI REST Boundary - Instants normalized to UTC]
┌─────────────────────────────────────────────────────────────┐
│                       app/main.py                           │
│  ┌──────────┐  ┌───────────┐  ┌───────────┐  ┌──────────┐   │
│  │employees │  │attendance │  │ analytics │  │  admin   │   │
│  │ (CRUD)   │  │(R1-R5 FSM)│  │ (Pipelines│  │ (Explain)│   │
│  └────┬─────┘  └─────┬─────┘  └─────┬─────┘  └────┬─────┘   │
│       │              │              │             │         │
│       ▼              ▼              ▼             ▼         │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  PyMongo ODM/Driver (tz_aware=True, Idempotency TTL) │   │
│  └──────────────────────────┬───────────────────────────┘   │
└─────────────────────────────┼───────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    MongoDB 6.0+ Database                    │
│   • employees         (Unique emp_code, department index)   │
│   • attendance_logs   (Compound natural key, IXSCAN plans)  │
└─────────────────────────────────────────────────────────────┘
```

## Requirements
- Python 3.11+
- MongoDB 6.0+

## Setup & Running

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment (optional):**
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Seed database:**
   ```bash
   python sample_seed.py           # Loads clean + realistic messy edge cases
   python sample_seed.py --clean   # Loads only standard sample data
   ```

4. **Run the API:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   Interactive Swagger docs are available at `http://localhost:8000/docs`.

5. **Run tests:**
   ```bash
   pytest tests/test_business_rules.py -v
   ```

## Architectural Highlights
- **Single-File Architecture:** All application logic is self-contained in `app/main.py`.
- **Atomic Concurrency:** Unique compound index on `(emp_code, date)` prevents race conditions at the database layer without locks.
- **Pure Aggregation Pipelines:** Analytics endpoints leverage MongoDB aggregation (`$setWindowFields` with `$rank` for competition ranking, cross-collection joins for headcount).
- **Timezone Boundary:** Instants are converted to UTC at the API boundary; IST (+05:30) is applied purely for shift/date calculations (R1).
- **Audit Trail:** Manual corrections append immutable diffs to `history[]` with optimistic locking.
- **Architectural Trade-offs:** Detailed in `DECISIONS.md`.
- **Starter Code Review:** Detailed in `REVIEW.md`.