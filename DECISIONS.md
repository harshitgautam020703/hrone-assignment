# DECISIONS.md

1. **Indexes.** I created unique `employees(emp_code)` and `attendance_logs(emp_code, date)` to enforce uniqueness at the DB layer, `attendance_logs(date DESC, emp_code ASC)` for index-covered sorted listing, and `(late_minutes)` for leaderboard scans. I rejected a standalone date index since compound indexes already prefix-cover date ranges.

2. **Punch-in race.** All instants are normalized to UTC at the API boundary. When concurrent identical requests arrive, both compute identical records. MongoDB's unique `(emp_code, date)` index permits the first insert and raises `DuplicateKeyError` on the second, returning HTTP 409 atomically without application-level locks.

3. **Ties.** I apply standard competition ranking (`$setWindowFields` with `$rank`). Tied employees share ranks while skipping subsequent ones (1, 2, 2, 4). When tied at the cutoff (`limit=2`), both are returned because `rank <= limit`, intentionally exceeding limit.

4. **Headcount.** Headcount aggregates `employees` where `joined_on <= month_end` first. Metrics from `attendance_logs` are then mapped, ensuring zero-log employees are accurately counted.

5. **100x Scale.** Audit trails are embedded in `history` for atomic, join-free reads. At 100x scale, I would decouple audit logs into a referenced collection and convert `attendance_logs` to a MongoDB Time-Series collection partitioned monthly.
