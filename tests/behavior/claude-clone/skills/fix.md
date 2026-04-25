# Fix a bug surgically

1. **Locate**: Grep the symptom (error message, function name) to find the source
2. **Read**: the full function + 10 lines of context
3. **Diagnose**: state the root cause in one sentence
4. **Fix**: Edit with minimal change — fix the bug, don't refactor unrelated code
5. **Verify**: Read the modified section to confirm the change
6. **Test**: run the relevant test (pytest tests/test_X.py, etc.)

Never apply a fix without reading the code that produces the bug.
