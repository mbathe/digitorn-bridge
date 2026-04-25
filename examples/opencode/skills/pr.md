# Create Pull Request

Follow these steps to create a well-documented pull request:

1. Run `git.status()` to check the current branch and changes
2. Run `git.log(limit=10, oneline=true)` to see all commits on this branch
3. Run `git.diff(target="main")` to see the full diff against the base branch
4. Analyze all commits and changes to understand the scope
5. Draft the PR:
   - Title: short, under 70 characters, describes the change
   - Body: summary (what and why), test plan, breaking changes if any
6. If there are uncommitted changes, ask the user before proceeding
7. Run `git.push(set_upstream=true)` to push the branch
8. Run `git.pr_create(title="...", body="...", base="main")` to create the PR
9. Report the PR URL to the user
