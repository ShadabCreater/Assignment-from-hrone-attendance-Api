# Employee Attendance & Analytics API

FastAPI and MongoDB backend for employee attendance, punch management, manual corrections with audit trails, and aggregation-driven analytics pipelines.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (MongoDB 7.0/8.0/9.0 supported)

## Installation & Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Configure environment variables (optional; defaults to `mongodb://localhost:27017` and `attendance_db`):
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```
*(On Windows PowerShell: `$env:MONGO_URI="mongodb://localhost:27017"`; `$env:MONGO_DB="attendance_db"`)*

3. Seed sample data (optional):
```bash
python sample_seed.py
```

4. Run the application:
```bash
uvicorn app.main:app --port 8000
```

All MongoDB indexes are created idempotently at application startup via FastAPI lifespan.

## Testing
Run the automated test suite:
```bash
python test_suite.py
python test_edge_cases.py
```

## Repository Structure
- `app/main.py`: Complete API application and business logic.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Technical code review of starter defects, failing symptoms, and fixes.
- `DECISIONS.md`: Architectural decisions regarding indexes, concurrency, ranking, headcount, and scale.
- `README.md`: Setup and run instructions.
