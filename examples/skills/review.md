# Code Review

Follow these steps to review code changes:

1. Run `git.diff()` to see all current changes
2. For each changed file:
   a. Read the file to understand the full context
   b. Check for security issues (hardcoded secrets, injection, XSS)
   c. Check for error handling (missing try/catch, silent failures)
   d. Check for code quality (naming, duplication, complexity)
   e. Check if changes are covered by tests
3. Store each finding with `memory.add_fact()`
4. Produce a summary with:
   - Number of files reviewed
   - Issues found (critical, warning, info)
   - Specific recommendations with file and line references
   - Overall assessment (approve, request changes, needs discussion)
