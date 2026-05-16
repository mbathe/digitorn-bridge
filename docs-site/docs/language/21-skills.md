---
id: skills
---

# Skills

Skills are **reusable workflow commands** packaged as markdown
files. The agent loads a skill on demand via the `use_skill` tool;
the file's content is returned and the agent follows the
instructions inside. Skills also surface in the client as `/command`
entries in the slash palette.

Every behaviour and field on this page maps to real code; entries
are cited with file + line.

## Anatomy

```text
my-app/
├── app.yaml
└── skills/
    ├── commit.md          # skill body (markdown)
    ├── review.md
    └── security-audit.md
```

Declare the skills under `dev.skills` :

```yaml
dev:
  skills:
    - command: /commit
      description: "Stage + commit + push the current diff"
      path: skills/commit.md

    - command: /review
      description: "Adversarial code review with focus on security"
      path: skills/review.md

    - command: /security-audit
      description: "Run the standard 6-step security audit"
      path: skills/security-audit.md
```

## `SkillEntry` reference

 `SkillEntry`
(`extra: forbid`).

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `command` | string (min 1) | yes | Slash command id (e.g. `/commit`). |
| `description` | string | no (default `""`) | One-line description shown in the slash palette and the `use_skill` catalog. |
| `path` | string (min 1) | yes | Path to the `.md` file relative to the bundle directory. |

The compiler reads each file at compile time and embeds its content
in the deployed bundle so skills work the same in dev, daemon, and
hub deployments.

## How an agent uses a skill

`use_skill` (`context_builder/actions_meta.py`) is one of the
always-available primitives - see
[Built-in Tools → use_skill](04b-builtin-tools.md#always-available-primitives-context_builder).

When the LLM calls `use_skill(command='/commit')`, the runtime
looks up the matching `SkillEntry`, returns the file content as
the action result, and prefixes it with a "Follow these
instructions" note.

```jsonc
// LLM call
{"name": "use_skill", "arguments": {"command": "/commit"}}

// Result (returned to the LLM)
{
  "success": true,
  "data": {
    "command": "/commit",
    "description": "Stage + commit + push the current diff",
    "content": "# Commit workflow\n\n1. Run `git status`...\n",
    "note": "Follow these instructions to complete the task."
  }
}
```

The leading `/` is optional in the `command` parameter - the
runtime adds it automatically (`actions_meta.py`).

## Two complementary surfaces

Don't confuse `dev.skills` with `ui.slash_commands`
(`typed_models.py`):

| Block | Purpose | Loaded by | Reach |
|-------|---------|-----------|-------|
| `dev.skills` | Reusable workflow MD files the **agent** loads via `use_skill`. | Compiler embeds the file. The agent calls `use_skill('/cmd')`. | Server-side. The agent reads the markdown and follows the steps. |
| `ui.slash_commands` | Pure client-side `/` palette entries. | Flutter / web client renders the palette; the chosen command's `template:` becomes the message sent to the agent. | Client-side. The agent never knows the slash palette existed - it just sees a normal user message. |

A typical app declares both: the slash command exposes a typed form
to the user; the resulting message tells the agent to invoke
`use_skill` with the right command.

## Auto-loading per agent

`schema.py` `AgentDefinition.capabilities`. Lists skill names
to **auto-load into the agent's system prompt**. The compiler reads
`skills/<name>.md` for each entry and appends the content under an
`## Available capabilities` section. Skills loaded this way don't
need to be invoked explicitly - they're already in the agent's
context.

```yaml
agents:
  - id: reviewer
    role: specialist
    capabilities:
      - git_review        # loads skills/git_review.md into the prompt
      - security_audit    # loads skills/security_audit.md into the prompt
```

Compare with the `instructions:` block (`typed_models.py`,
Phase-9 grouped form), which can also point at a single
`file:` and a list of `capabilities:`:

```yaml
agents:
  - id: reviewer
    role: specialist
    instructions:
      file: ./instructions/review.md   # appended to system_prompt
      capabilities: [git_review]       # auto-loaded from skills/
      specialty: "Adversarial code review"
```

The grouped form aliases back to the legacy fields at compile
time, so picking either shape works; downstream code (canvas,
inspector) reads both.

## Skill file format

Skills are **plain markdown**. The compiler doesn't parse them - it
inlines the raw text. Convention:

```markdown
# /commit - Stage, commit, push

## Goal
Produce a clean conventional-commits message and push to the
current branch.

## Steps
1. Run `git status` to see the working tree.
2. Run `git diff --staged` for any already-staged changes;
   `git diff` for unstaged.
3. Group changes by intent. One commit per intent.
4. For each commit:
   - Pick a type (feat / fix / refactor / docs / test / chore).
   - Stage the relevant files (`git add <paths>`).
   - Commit with a clear subject + body explaining the WHY.
5. Push the branch (`git push`).

## Anti-patterns
- DO NOT mix unrelated changes in one commit.
- DO NOT use vague subjects like "WIP" or "fix stuff".
```

The agent reads this verbatim and follows it. You can also use
the `{{prompt.X}}` and `{{include:path}}` namespaces inside the
markdown - they're resolved at compile time by `resolve_variables`.

## Bundle layout

The compiler expects skills in `<bundle>/skills/<name>.md` by
default - this is the convention `syncer.py` references when it
mirrors a deployed app. Other locations work as long as `path:`
points to an existing file relative to the bundle root.

For very large apps with many skills, consider grouping them in
sub-folders (`skills/git/commit.md`, `skills/security/audit.md`)
and listing each entry's full path.

## Compile-time validation

Per the Pydantic schema (`SkillEntry` `extra: forbid`):

- Every entry must declare `command` and `path` (both non-empty).
- The compiler reads each `path` at compile time. A missing file
  raises a clear error pointing at the bad entry.
- Duplicate `command` values raise an error (skill resolution would
  be ambiguous at runtime).

## User skills (`/use_skill`)

Skills on this page are **agent-facing**: the LLM calls `use_skill`
and the body comes back as a tool result. The runtime also supports
**user-facing** skills the end user picks from the composer and
that get injected as a **forced** `role: system` directive on the
matching turn. Authored per-user, stored in the daemon database,
gated by a single YAML flag:

```yaml
dev:
  allow_user_skills: true     # default false
```

Full mechanics, CRUD endpoints, the `/use_skill <name> <prompt>`
parser, the turn-scoped injection slot, and the web composer flow
(including the `.md` file picker) are documented in:

[Reference → Runtime → User Skills & `/use_skill`](../reference/runtime/user-skills.md)

## Cross-references

- The `use_skill` tool (always-available primitive):
  [Built-in Tools → use_skill](04b-builtin-tools.md#always-available-primitives-context_builder)
- App-level block reference for `dev.skills`:
  [App Configuration → dev](02-app-config.md#dev---developer-affordances)
- The `dev.include` fragmentation block (separate from skills):
  [App Configuration → dev.include](02-app-config.md#devinclude---fragmentation)
- Pure client-side slash palette (`ui.slash_commands`):
  [Client Manifest](44-client-manifest.md)
- Bundle namespace deep dive (`{{prompt.X}}`, `{{skill.X}}`,
  `{{include:}}`): [Bundle namespaces](38-bundle-namespaces.md)
- End-user-authored skills + `/use_skill` command:
  [Runtime → User Skills](../reference/runtime/user-skills.md)
