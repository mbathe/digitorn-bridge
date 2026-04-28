---
id: 33-rules
---

# Rules - Modular Project Instructions

Rules are `.md` files in `.digitorn/rules/` that provide **modular, scoped instructions** to the agent. Unlike `.digitorn.md` (one monolithic file), rules are organized by topic and can be conditionally loaded based on file paths.

## Structure

```
your-project/
  .digitorn/
    rules/
      python.md         # Always loaded - Python conventions
      testing.md         # Always loaded - Test guidelines
      api/
        security.md      # Loaded only for src/api/** files
        validation.md    # Loaded only for src/api/** files
```

## Rule Format

### Simple (always loaded)

```markdown
Always use ruff for linting. Never use flake8.
Use 4-space indentation. No tabs.
```

### Scoped (loaded conditionally)

```markdown
---
paths: ["src/api/**"]
---
All API routes must validate input with Pydantic models.
Never return raw database objects - always use response schemas.
```

The `paths:` frontmatter uses glob patterns. Rules without `paths:` are always loaded.

## How It Works

1. At bootstrap, `WorkspaceLayout.load_project_memory()` scans:
   - `.digitorn/rules/` (global rules)
   - `.digitorn/apps/{app_id}/rules/` (app-specific rules)
2. Each `.md` file is parsed for optional YAML frontmatter
3. Rules are concatenated to the project memory
4. The combined text is injected into the agent's system prompt

## Loading Priority

1. **Global rules** (`.digitorn/rules/`) loaded first
2. **App rules** (`.digitorn/apps/{app_id}/rules/`) loaded second (can override)
3. **`.digitorn.md`** or **`CLAUDE.md`** loaded as base project memory
4. All concatenated together

## Comparison with .digitorn.md

| Feature | `.digitorn.md` | `.digitorn/rules/` |
|---------|---------------|-------------------|
| Format | Single file | Multiple files |
| Organization | Monolithic | By topic/directory |
| Scoping | Always loaded | Optional `paths:` filtering |
| Maintenance | Gets unwieldy at scale | Clean separation of concerns |
| Use with | Both | Both |

**Use both together:** `.digitorn.md` for project overview, `rules/` for detailed topic-specific instructions.

## Examples

### `rules/git.md`
```markdown
Always create feature branches for new work.
Never push directly to main.
Commit messages: imperative mood, under 72 chars.
Always run tests before committing.
```

### `rules/api/auth.md`
```markdown
---
paths: ["src/api/**", "src/middleware/**"]
---
All API endpoints must check authentication via the auth middleware.
JWT tokens must be validated on every request.
Never store tokens in localStorage - use httpOnly cookies.
Rate limiting: 60 requests/minute per user.
```

### `rules/database.md`
```markdown
---
paths: ["src/models/**", "src/repositories/**"]
---
Always use parameterized queries - never string concatenation.
All tables must have created_at and updated_at timestamps.
Foreign keys must have ON DELETE CASCADE or SET NULL (never leave orphans).
Use database transactions for multi-table operations.
```
