# Systematic Debugging

Follow this process to debug an issue:

1. **Reproduce** — get a clear error message or failing test
   - If the user describes a bug, ask for the exact error or steps to reproduce
   - If there's a stack trace, read every file and line mentioned in it
   - If there's a failing test, run it: `shell.run("pytest path/to/test.py::test_name -v")`
2. **Isolate** — find the root cause
   - Use `grep` to find the function or class mentioned in the error
   - Read the surrounding code (50 lines of context)
   - Check recent changes: `shell.bash("git log --oneline -10")` and `shell.bash("git diff")`
   - Trace the data flow: where does the bad input come from?
3. **Hypothesize** — state what you think is wrong and why
   - Be specific: "Line 42 passes None to foo() which expects a string"
   - If unsure, add a diagnostic `shell.run()` to test your hypothesis
4. **Fix** — make the minimal change
   - Use `edit()` for surgical fixes
   - Don't fix unrelated issues you happen to notice
   - Don't refactor surrounding code
5. **Verify** — confirm the fix works
   - Run the failing test or reproduce the scenario
   - Run the full test suite if the change could have side effects
   - If the fix doesn't work, go back to step 2 with new information
6. **Clean up** — leave things tidy
   - `shell.bash("git status")` to check what changed
   - Remove any debug artifacts (print statements, test files)
   - Verify no unintended changes were made
