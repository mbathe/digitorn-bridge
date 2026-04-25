# Code Review

Follow these steps to review code changes:

1. Run `shell.bash("git diff")` to see all current changes
2. For each changed file:
   a. Read the file to understand the full context (not just the diff)
   b. Check for security issues:
      - Hardcoded secrets, API keys, tokens
      - SQL injection, command injection, XSS
      - Unsafe deserialization, path traversal
   c. Check for error handling:
      - Missing try/catch, silent failures
      - Bare except / catch(e) without handling
      - Error swallowing (logging but not re-raising when needed)
   d. Check for code quality:
      - Naming clarity, duplication, excessive complexity
      - Magic numbers or strings that should be constants
      - Functions longer than 50 lines
   e. Check for common anti-patterns:
      - Hardcoded values that should be configuration
      - N+1 query patterns (loop with individual DB/API calls)
      - Race conditions in async code (missing await, shared mutable state)
      - Resource leaks (unclosed files, connections, cursors)
   f. Check if changes are covered by tests
3. Store each finding with `memory.add_fact()` as you go
4. Produce a summary:
   - Number of files reviewed
   - Issues found by severity (critical, warning, info)
   - Specific recommendations with file:line references
   - Overall assessment: approve, request changes, or needs discussion
