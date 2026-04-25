# Simplify

Review changed code for reuse, quality, and efficiency, then fix any issues found.

1. Run `shell.bash("git diff")` to see all current changes
2. For each changed file, read the full file to understand context
3. Look for:
   - Duplicated code that could be extracted into a function
   - Over-engineered abstractions that add complexity without value
   - Unnecessary error handling for impossible scenarios
   - Variables or imports that are declared but never used
   - Functions that are too long (> 50 lines) and should be split
   - Magic numbers or strings that should be constants
4. For each issue found:
   - Fix it directly with a surgical edit
   - Keep the fix minimal -- don't refactor surrounding code
5. After all fixes, run `shell.bash("git diff")` to verify the changes look clean
6. Report what you simplified and why
