# DECISIONS.md

1. **Indexes.** I use indexes based on the application's query patterns rather than indexing every field.
I use a unique compound index on (emp_code, date) to guarantee at most one attendance record per employee per calendar date. Department and joining-date indexes support employee filtering and eligible headcount queries. Attendance date/status indexes support filtered attendance lists and monthly analytics. Date/late-minutes indexes support late-arrival queries and leaderboard calculations. Employee/punch-in indexes support selecting the appropriate attendance record during punch-out. This indexes also enforce important data-integrity rules at the database level. Avoiding unnecessary indexes reduces storage overhead and the cost of maintaining indexes during writes.

2. **Punch-in race.** Two concurrent punch-in requests for the same employee and attendance date may both pass an initial existence check. An application-level check alone cannot prevent this race condition. The unique (emp_code, date) index is therefore the final safeguard. Only one insert can succeeds with 201 while MongoDB rejects the other insert, which the API converts to 409. This approach relies on a database-enforced constraint rather than assuming that requests execute sequentially.

3. **Ties.** The leaderboard uses MongoDB `$rank` over total late minutes, then filters `rank <= limit`. Equal totals share a rank and the next rank is skipped, so a tie at the cutoff returns every employee sharing that rank. Rows are then ordered by late minutes descending and employee code ascending.

4. **Headcount.** The department summary starts from employees eligible by the period end and looks up their attendance logs. Employees with no matching logs remain in the pipeline, so they count once toward headcount but contribute no attendance metrics.

5. **100x data.** If the dataset grows substantially, I would first measure query latency, execution plans, index usage. Based on those measurements, possible improvements include pre-aggregated daily summaries, retention or archival policies, and targeted index adjustments. The original attendance records should remain the authoritative source.
