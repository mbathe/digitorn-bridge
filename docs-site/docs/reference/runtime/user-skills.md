---
id: user-skills
title: User Skills & `/use_skill`
sidebar_label: User Skills
sidebar_position: 9
description: User-authored skills, the `/use_skill` composer command, and the turn-scoped system-prompt injection that backs them.
---

# User Skills & `/use_skill`

App-declared skills (the YAML `dev.skills`) are **agent-facing**:
the LLM decides to call `use_skill(command="/commit")` and gets the
markdown body as a tool result. That works for "self-help" recipes
the agent reaches for on its own, but it can't be **forced** —
the LLM may ignore the skill the user wanted applied.

User skills close that gap. They are **user-facing**:

- The end user picks a skill from the composer palette.
- The composer inserts `/use_skill <name> ` in the textarea.
- On send, the daemon parses the prefix, looks up the skill, and
  injects its body as a **turn-scoped `role: system` directive**
  so the agent **must** follow it for that turn.
- The chat history stores the user's actual prompt (without the
  dispatch prefix); the system directive lives only on the per-turn
  context and never leaks to follow-up turns.

This page describes the runtime mechanics. For the surrounding
bundle / `dev.skills` model see [Skills](../../language/21-skills.md).

## TL;DR

- Source of truth (daemon):
  - Flag: `dev.allow_user_skills: bool` in
    `packages/digitorn/core/app/schema.py`
    ([`DevBlock`](https://github.com/digitorn/digitorn-bridge/blob/main/packages/digitorn/core/app/schema.py))
  - Table: `user_skills` (migration
    `packages/digitorn/core/migrations/versions/0010_user_skills.py`)
  - ORM: `UserSkill` in `packages/digitorn/core/models.py`
  - CRUD router:
    `packages/digitorn/core/api/apps_v2/skills.py`
  - Parser + injection:
    `packages/digitorn/core/api/apps_v2/messages.py`
    (search `/use_skill`)
- Injection reuses the **same slot** as `template_id`:
  `ctx.template_system_prompt`. The agent loop prepends it on every
  LLM round-trip inside the turn
  (`runtime/agent_loop.py::_chat_messages_for_llm`).
- Two sources, one lookup order: user table first, app-declared
  fallback. A user skill named `commit` shadows an app skill `/commit`.

## YAML opt-in

User skills are **disabled by default**. The app author opts in:

```yaml
dev:
  allow_user_skills: true
  skills:                       # app-declared still work as before
    - command: /commit
      description: "Stage + commit + push the current diff"
      path: skills/commit.md
```

| Flag | Default | Effect |
|---|---|---|
| `dev.allow_user_skills` | `false` | `false` → POST/PATCH/DELETE on `/api/apps/{app_id}/skills` return 403. Web composer hides the "+ New skill" affordance. GET still works and returns app-declared skills only. |

## The `user_skills` table

```sql
CREATE TABLE user_skills (
  id            VARCHAR(64)  PRIMARY KEY,
  user_id       VARCHAR(64)  NOT NULL,
  app_id        VARCHAR(128) NOT NULL,
  name          VARCHAR(128) NOT NULL,
  description   VARCHAR(300) NOT NULL DEFAULT '',
  instructions  TEXT         NOT NULL,
  created_at    TIMESTAMPTZ  NOT NULL,
  updated_at    TIMESTAMPTZ  NOT NULL
);
CREATE INDEX        ix_user_skills_user_app       ON user_skills(user_id, app_id);
CREATE UNIQUE INDEX ux_user_skills_user_app_name  ON user_skills(user_id, app_id, name);
```

Scope is strict on `(user_id, app_id)`:

- A user never sees another user's skills.
- A skill authored while chatting with `digitorn-code` is invisible
  inside `digitorn-chat`.
- The unique index on `(user_id, app_id, name)` keeps the
  `/use_skill <name>` lookup unambiguous; two users can share the
  name `commit`, the same user can have `commit` in two different
  apps, but a single (user, app) pair cannot.

The `name` column is a slug (`^[a-z0-9][a-z0-9-]{0,63}$`): lowercase
letters, digits, hyphens; must start with a letter or digit; max 64
chars. The composer validates the slug client-side; the API rejects
non-conforming names with `422`.

## CRUD endpoints

All endpoints live under `/api/apps/{app_id}/skills` and require an
authenticated user (`request.state.user_id`).

### `GET /api/apps/{app_id}/skills`

Always allowed. Returns three things in one payload:

```json
{
  "success": true,
  "data": {
    "app_skills": [
      { "command": "/commit", "description": "..." }
    ],
    "user_skills": [
      {
        "id": "uuid",
        "app_id": "digitorn-code",
        "name": "tone-witty",
        "description": "Write with wit and personality",
        "instructions": "# Tone\n\nUse short sentences...",
        "created_at": "2026-05-16T...",
        "updated_at": "2026-05-16T..."
      }
    ],
    "allow_user_skills": true
  }
}
```

- `app_skills` ships the **label + description only**. The .md body
  stays daemon-internal; the picker never needs it.
- `user_skills` ships the full row including `instructions` so the
  editor can show the body when the user clicks "Edit".
- `allow_user_skills` mirrors the YAML flag — used by the web client
  to toggle the "+ New skill" UI.

### `POST /api/apps/{app_id}/skills`

```json
{
  "name": "tone-witty",
  "description": "Write with wit and personality",
  "instructions": "# Tone\n\nUse short sentences..."
}
```

| Status | Reason |
|---|---|
| 201 / 200 with `{success: true}` | Created |
| 401 | No authenticated user |
| 403 | `dev.allow_user_skills` is `false` |
| 404 | App not deployed |
| 409 | A skill with this `name` already exists for this (user, app) |
| 422 | Slug shape invalid |

### `PATCH /api/apps/{app_id}/skills/{id}`

Partial update. Pydantic's `model_fields_set` distinguishes
"omitted" from "explicitly null" — fields not in the request body
are left untouched. Renames trigger a `409` if the new name
collides.

| Status | Reason |
|---|---|
| 200 | Updated |
| 401 / 403 / 404 / 409 / 422 | Same as POST |

`404` on a row owned by another user (no "exists but forbidden" leak).

### `DELETE /api/apps/{app_id}/skills/{id}`

Hard delete. Same authentication / 403 / 404 rules.

## The `/use_skill` parser

Detection lives in `session_send_message` in
`packages/digitorn/core/api/apps_v2/messages.py`, immediately after
the `template_id` block. The regex (case-insensitive):

```python
^\s*/use_skill\s+/?([a-zA-Z0-9_-]+)(?:\s+([\s\S]*))?$
```

- Group 1 — the skill `name`. The leading slash is optional, so
  both `/use_skill commit` and `/use_skill /commit` work.
- Group 2 — the user's actual prompt, may be multi-line, may be
  empty (the skill alone can answer for itself).

Lookup order:

1. **`user_skills` table** when the app has `allow_user_skills: true`
   and a real `user_id` is present.
2. **`compiled.skills`** (app-declared, `.md`-backed) as fallback.
   Match on `command == "/<name>"` OR `command.lstrip("/") == name`.

Resolution outcomes:

| Result | HTTP |
|---|---|
| Found in either source | 200 — dispatch continues normally |
| Not found | 404 `skill not found` |
| Pre-flight error (app missing, …) | 404 / 503 — see standard helpers |

On a hit the daemon:

1. **Strips the dispatch prefix** from `body.message`. The chat
   history stores only the user's real prompt — never the dispatch
   command — so a follow-up message reading the past turns won't
   show a leaked `/use_skill commit` line.
2. **Wraps the skill body** in a mandatory framing line so the LLM
   treats it as authoritative:

   ```text
   # MANDATORY DIRECTIVE — Skill: /<name>

   Follow the instructions below to handle the user's next message.
   They take precedence over your default behaviour for this turn only.

   ---

   <skill instructions verbatim>
   ```
3. **Concats into `_template_system_prompt`**. If the request also
   carries a `template_id`, the template directive comes first, the
   skill directive second, separated by `---`. Otherwise the skill
   directive stands alone.
4. **Continues the normal dispatch** — the queue, the dispatcher,
   the manager all receive the slot via the existing
   `template_system_prompt` plumbing
   (`message_queue.py`, `_dispatch.py`, `manager_v2/_chat.py`).

## Turn-scoped injection

`ctx.template_system_prompt` is set on the **per-turn** context copy.
`runtime/agent_loop.py::_chat_messages_for_llm` reads it on every
LLM round-trip inside that single turn:

```python
def _chat_messages_for_llm(ctx, messages):
    pruned = _strip_transient_from_past_messages(messages)
    out = to_chat_messages(pruned)
    sys_prompt = getattr(ctx, "template_system_prompt", "") or ""
    if sys_prompt:
        return [{"role": "system", "content": sys_prompt}, *out]
    return out
```

Result:

- The system directive applies through the initial call **and**
  every tool-loop iteration of the same turn.
- The very next user message gets a fresh `ctx` without the
  addendum. No "leak" cleanup is required.
- An `/use_skill` on turn N doesn't affect turn N+1.

If the user wants the directive to persist they re-send
`/use_skill <name> <new prompt>` on the next turn.

## Web composer integration

UI lives in `digitorn_web/src/components/chat/premium-composer.tsx`:

- **Palette** — a new `Use skill` entry in the `+` menu (icon
  `Sparkles`). Click opens `SkillsMenu`.
- **`SkillsMenu`** — two sections:
  - **App skills**: read-only rows, pulled from
    `manifest.appSkills` / the daemon GET response.
  - **My skills**: CRUD rows, edit + delete icons on hover. Only
    rendered when the GET response says
    `allow_user_skills: true`.
- **Pick** — inserts `/use_skill <name> ` into the textarea and
  parks the caret at the end. The user types their prompt and hits
  Enter. POST `/messages` carries the message verbatim; the daemon
  parses.
- **Editor** — name (live-slugified), description, markdown
  instructions textarea, plus a **"📄 Pick .md file"** button. The
  button opens a hidden `<input type="file" accept=".md,.markdown">`;
  the file is read client-side via `FileReader.text()` and dropped
  into the instructions field. If the name input is empty, the
  filename (minus extension, slugified) is used as the default.

The hooks (`useSkills`, `useCreateSkill`, `useUpdateSkill`,
`useDeleteSkill`) live in
`digitorn_web/src/hooks/use-skills.ts`. They use TanStack Query;
mutations invalidate the `["skills", appId]` key so the menu always
reflects the latest server state without polling.

## Manifest surface

The daemon's `summary()`
(`packages/digitorn/core/app/manager_v2/_models.py`) exposes the two
fields the web client needs:

```json
{
  "skills": [
    { "command": "/commit", "description": "..." }
  ],
  "allow_user_skills": true
}
```

The web manifest parser
(`digitorn_web/src/models/app-manifest.ts`) maps them to
`AppManifest.appSkills` and `AppManifest.allowUserSkills`.

## Worked example

App YAML (`my-bot/app.yaml`):

```yaml
app:
  id: my-bot
  name: My Bot

dev:
  allow_user_skills: true
  skills:
    - command: /commit
      description: "Stage + commit + push the current diff"
      path: skills/commit.md
```

The user creates a personal skill via the composer:

- name: `tone-witty`
- description: `Write with wit and personality`
- instructions (from a `.md` file):

  ```markdown
  # Tone

  - Use short sentences.
  - One concrete example per claim.
  - Land the joke before the explanation.
  - No filler ("In conclusion", "Furthermore", "Indeed").
  ```

The user sends:

```text
/use_skill tone-witty rewrite the changelog so my mum would laugh at it
```

Server-side, the daemon:

1. Parses → `name=tone-witty`, `prompt=rewrite the changelog so my mum would laugh at it`.
2. Looks up `tone-witty` in `user_skills` for `(user, my-bot)` — hit.
3. Wraps the body in the MANDATORY DIRECTIVE frame.
4. Sets `_template_system_prompt` to the wrapped body.
5. Rewrites `body.message` to just
   `rewrite the changelog so my mum would laugh at it`.
6. Dispatches as normal.

The agent receives a `role: system` message at the head of the turn
forcing the tone, then sees the user's request. The chat history
stores the user message without the dispatch prefix.

## Comparison with sibling primitives

| Primitive | Authored by | Triggered by | Where the body lives | Persistence |
|---|---|---|---|---|
| `dev.skills` (`use_skill` tool) | App author | LLM (tool call) | `.md` files in bundle, loaded at compile time | Per-call tool result |
| `user_skills` (`/use_skill`) | End user | User (composer command) | `user_skills.instructions` (DB) | Turn-scoped `role: system` |
| `templates` (`template_id`) | App author | User (gallery pick) | `TemplateBlock.system_prompt` + `seed_dir` | Turn-scoped `role: system` (same slot) |
| `behavior.profile` | App author | Always-on | `behavior/X.yaml` | Whole-session rule engine |

The three turn-scoped mechanisms (`template_id`, `/use_skill`, future
similar) all share the **same injection slot**
(`ctx.template_system_prompt`). When more than one fires on the same
turn they concat with `---` separators, template first, skill
second.

## Gotchas

- **Daemon restart required after the migration**. Migration `0010`
  creates the table; without it the CRUD endpoints will fail at
  query time. Bootstrap runs migrations on startup.
- **Slug case**. The parser lowercases the captured name before
  lookup. Two skills `Commit` and `commit` cannot coexist for the
  same (user, app) — the unique index is case-sensitive but the
  lookup is not, so the second insert would still succeed and the
  first one would shadow it. The slug regex prevents uppercase at
  the API boundary anyway.
- **Empty prompt**. `/use_skill foo` with no trailing text is
  accepted. The skill body alone drives the turn; sometimes that
  is what you want ("apply the audit checklist to the current
  workspace").
- **Combining with `template_id`**. Both can fire on the same
  message. Order: template directive first, skill directive second.
  Use sparingly — a 5 KB combined system prompt eats tokens.
- **Daemon-internal `use_skill` tool is unchanged**. The agent can
  still call `use_skill(command="/foo")` to load an app skill on
  its own initiative. The user-facing `/use_skill` and the
  agent-facing `use_skill` are different code paths that happen to
  share a vocabulary.

## See also

- [Skills](../../language/21-skills.md) — the app-author side
  (`dev.skills`, .md bundle, `use_skill` tool).
- [Workdir Sandbox](./workdir-sandbox.md) — the other "single
  primitive, many call sites" pattern.
- [Configuration](./configuration.md) — runtime knobs.
