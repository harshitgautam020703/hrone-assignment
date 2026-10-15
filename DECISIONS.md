# DECISIONS.md

1. **Indexes.** I created unique `employees(emp_code)` to prevent duplicates, unique `attendance_logs(emp_code, date)` to atomically stop duplicate punch-ins, `attendance_logs(date DESC, emp_code ASC)` for fast sorted listing, and `(late_minutes)` for leaderboard scans. I rejected a standalone `attendance_logs(date)` index since existing compound indexes already cover date lookups.

2. **Punch-in race.** When identical punch-in requests arrive simultaneously, both compute the record. Upon `insert_one()`, MongoDB’s unique `(emp_code, date)` index admits the first insert and rejects the second with `DuplicateKeyError`. The exception handler catches it and returns HTTP 409 without application locks.

3. **Ties.** I apply standard competition ranking (`$setWindowFields` with `$rank`). Tied employees share ranks while skipping subsequent ones (1, 2, 2, 4). When tied at the cutoff (`limit=2`), both are included because the query filters `rank <= limit`, deliberately returning more rows than limit.

4. **Headcount.** Headcount aggregates `employees` where `joined_on <= month_end` first. Summaries map log metrics onto these departments, ensuring zero-log employees are counted.

5. **100x Scale.** I would convert `attendance_logs` to a MongoDB Time-Series collection partitioned monthly to compress storage and speed range scans.
