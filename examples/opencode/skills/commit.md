# Smart Commit

Follow these steps to create a well-formatted git commit:

1. Run `git.status()` to see all changes (staged, unstaged, untracked)
2. Run `git.diff()` to understand what changed in detail
3. Run `git.log(limit=5, oneline=true)` to match the repository's commit style
4. Analyze the changes:
   - If changes address multiple concerns (bug fix + feature + refactor), group them into separate commits
   - Each commit should be atomic - one logical change per commit
5. Draft the commit message:
   - Focus on WHY the change was made, not WHAT changed
   - Keep the first line under 72 characters
   - Use imperative mood ("Add feature" not "Added feature")
   - If the change is complex, add a blank line then a detailed body
6. Stage relevant files with `git.add(files=[...])`
   - Never stage .env, credentials, node_modules, or large binaries
   - Stage specific files, not everything
   - If splitting into multiple commits, stage one group at a time
7. Run `git.commit(message="...")` with the drafted message
8. If there are remaining changes for a second commit, repeat from step 5
9. Run `git.status()` to verify the commit(s) were successful
