# Safe Refactoring

Refactor code with test verification at every step.

1. **Scope** - identify exactly what to refactor and why
   - Read all affected files first
   - Count the files that will change
   - If the scope is large (10+ files), ask the user to confirm before starting
2. **Safety net** - run the test suite BEFORE starting
   - Use /test or run the project's test command
   - If tests already fail, tell the user before proceeding
   - Store the baseline result with `memory.add_fact("baseline tests: X passed, Y failed")`
3. **Execute** - make changes one file at a time
   - After each file: run tests to verify nothing broke
   - If tests fail after a change: `filesystem.undo(path)` to restore, then investigate
   - Common refactors:
     - **Rename**: grep for ALL usages first, then change all at once
     - **Extract function**: identify the block, create the function, replace the original call
     - **Move file**: update ALL imports after moving (grep for the old import path)
     - **Inline**: verify the function is only called in one place before inlining
4. **Final check** - run the full test suite
   - `git.diff()` to review all changes
   - Compare with baseline: same number of tests should pass
5. **Report** - summarize what was refactored
   - Files changed
   - Tests passed (before vs after)
   - What was refactored and why
