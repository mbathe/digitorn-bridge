# Create Pull Request

Follow these steps to create a well-documented pull request:

1. Run `shell.bash("git status")` to check the current branch and changes
2. Run `shell.bash("git log --oneline -10")` to see all commits on this branch
3. Run `shell.bash("git diff main...HEAD")` to see the full diff against the base branch
4. Analyze all commits and changes to understand the scope
5. Draft the PR:
   - Title: short, under 70 characters, describes the change
   - Body: summary (what and why), test plan, breaking changes if any
6. If there are uncommitted changes, ask the user before proceeding
7. Run `shell.bash("git push -u origin HEAD")` to push the branch
8. Run `shell.bash("gh pr create --title '...' --body '...' --base main")` to create the PR (requires GitHub CLI)
   - If gh is not available, report the branch name and ask the user to create the PR manually
9. Report the PR URL to the user
