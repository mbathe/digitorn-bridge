# Audit procedure

1. Run `Glob` with pattern `**/*.{py,js,ts,go,rb}` to list all source files.
2. For each file type, run `Grep` for high-risk patterns:
   - `password`, `api_key`, `secret`, `token` (hardcoded secrets)
   - `eval(`, `exec(`, `pickle.loads` (unsafe deserialization)
   - `SELECT.*\+`, `execute.*%s` (SQL injection risk)
   - `subprocess.*shell=True`, `os.system` (command injection)
3. Read the top 5 most suspicious files.
4. Produce a report with findings grouped by severity.
