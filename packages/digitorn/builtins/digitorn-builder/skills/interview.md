---
version: 1
description: How to interview users to understand what they want to build
---

## Interview skill

### Goal
Extract enough information to generate a working Digitorn app YAML.

### Step 1 - Discovery (one question)

Ask: "What should your app do, in one sentence?"
Then call `memory.set_goal(goal=<their answer>)`.

### Step 2 - Template match

Query RAG for matching templates:
```
rag.query(knowledge_base="digitorn_examples", query=<user goal>, top_k=3)
```

If a template matches > 70%, propose it:
```
ask_user(
  question="I found a template that fits: **<name>**. Use it?",
  choices=["yes - adapt it", "no - build from scratch"]
)
```

### Step 3 - Structured interview

Use **ask_user** with choices for EVERY constrained question:

**Trigger type:**
```
ask_user(
  question="What fires this app?",
  choices=["cron schedule", "file watcher", "Telegram bot",
           "Slack bot", "HTTP webhook", "manual / on-demand"]
)
```

**Session mode:**
```
ask_user(
  question="Does each user need their own session?",
  choices=["yes - multi-user", "no - single shared session"]
)
```

**Multi-field form** (when collecting several related fields):
```
ask_user(
  question="Configure the app",
  form=[
    {"type": "text", "name": "app_name", "label": "App name"},
    {"type": "select", "name": "frequency", "label": "How often?",
     "options": ["every hour", "daily", "weekly"]},
    {"type": "text", "name": "city", "label": "City"},
    {"type": "number", "name": "min_salary", "label": "Min salary", "default": 60000},
    {"type": "toggle", "name": "remote_only", "label": "Remote only?", "default": true}
  ]
)
```

### What you need to know before generating

a. **Trigger type** - cron / channel / webhook / manual / file_watcher
b. **Modules needed** - web? memory? database? filesystem? shell? rag?
c. **Multi-user or single** - mono / multi session
d. **Payload schema** - does the user need to configure per-session params?
e. **One example** of input → output
f. **Output channels** - Slack? email? just chat?

### Rules

- ONE question per ask_user call
- Adapt next question to previous answer
- Stop interviewing once you have enough info (a–f above)
- Each answer → `memory.remember(key=..., value=...)`
- Before generating, recap your understanding to the user
