# Smart Commit

Follow these steps to create a well-formatted git commit:

1. Run `git.status()` to see all changes (staged, unstaged, untracked)
2. Run `git.diff()` to understand what changed in detail
3. Run `git.log(limit=5, oneline=true)` to match the repository's commit style
4. Analyze the changes and draft a commit message:
   - Focus on WHY the change was made, not WHAT changed
   - Keep the first line under 72 characters
   - Use imperative mood ("Add feature" not "Added feature")
   - If the change is complex, add a blank line then a detailed body
5. Run `git.add(files=[...])` for relevant files only
   - Never stage .env, credentials, node_modules, or large binaries
   - Prefer staging specific files over "all"
6. Run `git.commit(message="...")` with the drafted message
7. Run `git.status()` to verify the commit was successful
