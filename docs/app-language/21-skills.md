---
id: skills
---

# Skills System

Skills are reusable workflow commands that agents can load on demand. They provide step-by-step methodology for specific tasks without hardcoding instructions in the system prompt.

Skills are NOT tools. They are **prompt injections** -- the agent gets instructions then uses real tools to execute them.

## How It Works

```
1. Developer creates skill files (.md)
2. YAML declares available skills with commands
3. Compiler loads files at startup
4. Context builder shows the skill list to the agent
5. Agent calls use_skill("/commit") when it wants to use one
6. Skill content is returned as ActionResult
7. Agent follows the instructions using real tools (git, filesystem, etc.)
```

## Configuration

```yaml
skills:
  - command: "/commit"
    description: "Smart git commit workflow"
    path: "./skills/commit.md"
  - command: "/review"
    description: "Code review checklist"
    path: "./skills/review.md"
  - command: "/security"
    description: "Security audit methodology"
    path: "./skills/security.md"
```
Paths are resolved relative to the YAML file location.

## Skill File Format

A skill file is a markdown file with optional frontmatter:

```markdown
---
name: Smart Commit
description: Analyze changes and create a well-formatted git commit
---

# Smart Commit Workflow

1. Run `git.status()` to see all changes
2. Run `git.diff()` to understand what changed
3. Run `git.log(limit=5)` to match the repository's commit style
4. Analyze the changes and draft a commit message:
   - Focus on WHY, not WHAT
   - Keep the first line under 72 characters
   - Use imperative mood ("Add feature" not "Added feature")
5. Run `git.add(files=[...])` for relevant files only
   - Never stage .env, credentials, or large binaries
6. Run `git.commit(message="...")` with the drafted message
7. Verify with `git.status()` that the working tree is clean
```

## What the Agent Sees

When skills are configured, the context builder automatically injects:

```
# Available Skills

You have reusable workflows (skills) available.
Call use_skill(command="/name") to load detailed instructions.

  - /commit: Smart git commit workflow
  - /review: Code review checklist
  - /security: Security audit methodology
```

The agent decides when to use a skill. The system never forces skill usage.

## use_skill Action

```
use_skill(command="/commit")
```

Returns:
```json
{
  "command": "/commit",
  "description": "Smart git commit workflow",
  "content": "# Smart Commit Workflow\n\n1. Run git.status()...",
  "note": "Follow these instructions to complete the task."
}
```

## Example Skills

### /commit
```markdown
# Smart Commit

1. git.status() -- see what changed
2. git.diff() -- understand the changes
3. git.log(limit=5, oneline=true) -- match commit style
4. Draft commit message (why > what, imperative mood)
5. git.add(files=[specific files]) -- never use git add .
6. git.commit(message="...")
7. git.status() -- verify clean
```

### /review
```markdown
# Code Review

For each changed file:
1. Read the file
2. Check for:
   - Security issues (hardcoded secrets, SQL injection, XSS)
   - Error handling (missing try/catch, silent failures)
   - Code quality (naming, duplication, complexity)
   - Tests (are changes covered by tests?)
3. Store findings as facts
4. Produce a summary with severity ratings
```

### /security
```markdown
# Security Audit

For each file:
1. Check for hardcoded credentials (API keys, passwords, tokens)
2. Check for injection vulnerabilities (SQL, command, path traversal)
3. Check for insecure deserialization (pickle, yaml.load)
4. Check environment variable handling
5. Check authentication/authorization logic
6. Check input validation and sanitization
7. Rate each finding: critical / high / medium / low
```

## Integration with Multi-Agent

Skills work with the multi-agent system. A specialist agent can have its own skills:

```yaml
agents:
  - id: security_analyst
    role: specialist
    skills: "./skills/security_methodology.md"   # agent-level skill
    system_prompt: "You are a security expert."

skills:                                           # app-level skills
  - command: "/security"
    description: "Run a security audit"
    path: "./skills/security.md"
```
Agent-level skills (on specialists) are always loaded in the system prompt. App-level skills are loaded on demand via `use_skill`.
