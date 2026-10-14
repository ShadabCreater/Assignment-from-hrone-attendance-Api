# DECISIONS.md

1. **Indexes.**
   On `employees`: unique `emp_code` (business key uniqueness), `(department, emp_code)` (filtering and sorting), and `(department, joined_on)` / `joined_on` (headcount range filtering).
   On `attendance_logs`: unique `(emp_code, date)` (guarantees one record per employee-day and concurrency safety), `(date, -1, emp_code, 1)` (attendance listing and pagination), `(emp_code, 1, punch_in, -1)` (punch-out lookup), and `(date, 1, late_minutes, 1, emp_code, 1)` (leaderboard IXSCAN).
   *Rejected:* `(emp_code, date, status, work_hours)` as an over-indexed covered index—rejected because high write volume on punches would suffer from excessive index maintenance overhead.

2. **Punch-in race.**
   Both requests validate payload and compute attendance date (R1). Both call `insert_one()`. Because `(emp_code, date)` has a unique index, MongoDB atomically accepts exactly one insert (returning 201). The second request triggers `DuplicateKeyError`, which our handler intercepts and returns HTTP 409 Conflict. No check-then-insert race window exists.

3. **Ties.**
   Using MongoDB's `$setWindowFields` with `$rank`, tied employees receive identical ranks and subsequent ranks are skipped (standard competition ranking: 1, 2, 2, 4). When employees tie at `rank <= limit`, all tied employees are returned (ordered by `emp_code` ascending). The response can exceed `limit` rows because filtering checks rank, not array slice.

4. **Headcount.**
   The summary aggregation starts from `employees` filtered by `joined_on <= end_of_month`. It groups by department to establish baseline headcount before performing a `$lookup` into `attendance_logs`. Employees with zero records contribute 1 to headcount and 0 to attendance metrics.

5. **One thing to change for 100x data.**
   Shard `attendance_logs` by hashed `emp_code`, and materialize monthly department summaries asynchronously via Change Streams rather than on-demand full month scans across 10 million records.
