# Explore a codebase fast

Follow this procedure to understand a repo you don't know:

1. **Glob top-level**: `*` - see the root structure
2. **Read README-like files**: README.md, CONTRIBUTING.md, CHANGELOG.md
3. **Glob source files** by type: `**/*.py`, `**/*.js`, `**/*.ts`
4. **Grep entry points**: `main`, `if __name__`, `app =`, `def main`
5. **Read the entry file(s) and the config file(s)**: pyproject.toml, package.json, Cargo.toml
6. **Summarize** in 5 bullet points: language, framework, entry point, main modules, build/test commands

Never read more than 5 files before summarizing what you found.
