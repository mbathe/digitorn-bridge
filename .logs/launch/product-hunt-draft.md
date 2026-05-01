# Product Hunt launch — Digitorn

## Tactics that work on PH in 2026

- **Tuesday is the best day** for dev tools (Mon and Wed are crowded with consumer apps).
- **Launch at 12:01 AM Pacific Time** to get the full 24-hour window on the leaderboard.
- **Maker comment within 5 minutes** of going live. Hunters check this.
- **Reply to every comment in the first 12 hours**.
- **Have a hunter** if possible (1k+ followers): ask in the PH Discord, or look at past hunters of similar dev tools.
- **Email subscribers + Twitter the morning of**, not before. Premature notification eats into launch day votes.
- **No "vote for us" begging**. PH downranks campaigns that look coordinated.
- **5 images required**. First image = hero, set on autoplay if it's a GIF.

## Listing fields

### Tagline (60 char max, the most important field)

Pick one:

1. **Build AI agent apps in YAML. Run them on your machine.** (49 chars)
2. **Self-hosted AI agents in YAML, with a visual builder.** (53 chars)
3. **The declarative AI agent framework. YAML in, agent out.** (55 chars)
4. **AI agents in YAML. No glue code. Apache 2.0.** (45 chars)

Recommendation: option 1 (clearest, leads with action verb, names the differentiator).

### Description (260 char max)

```
Digitorn turns one YAML file into a multi-agent app with credentials,
channels, hooks, and triggers. The runtime is self-hosted and Apache
2.0. The conversational + visual builder lets you ship in five
minutes without reading the schema.
```

Length: 254 chars. Has the three pillars (YAML, self-hosted, builder) and a concrete number.

### First comment by maker

```
Hey Hunters,

I'm Paul, the maker of Digitorn.

Short version: a Python runtime that runs AI agent apps written as YAML.
Long version: I got tired of writing 300 lines of LangChain wrappers for
every new agent. Digitorn collapses agent + tools + memory + channels +
triggers into one config file and serves them from a self-hosted daemon.

Three things I'm proud of:

1. The Builder — describe an agent in plain language, watch it generate
   the YAML, edit it on a live canvas with 5 view lenses (architecture,
   security, performance, runtime, sequence). Two-way binding: edit YAML,
   canvas updates; drag a node, YAML rewrites.

2. Credentials vault — 4 scopes (system, per-app, per-user, per-app-
   per-user), envelope encryption, hash-chained audit trail. No more
   `api_key: "{{env.X}}"` sprinkled through configs.

3. Multi-agent done right — one Agent(...) tool with 8 modes
   (background, blocking, batch wait, cancel, reassign, etc.). Parallel
   by default, abort actually kills child shell processes.

Apache 2.0, Python 3.12+, runs on macOS / Linux / Windows. Single command
install:

    curl -sSL https://digitorn.ai/install | sh

Honest comparison: LangChain still wins for Python-native data prep in
notebooks. CrewAI wins on tutorials. OpenAI Assistants wins on hosted
infra. Digitorn wins on self-hosted, multi-agent, channels, audit, and
the sheer size of the YAML diff vs the equivalent code.

Try the Builder: https://digitorn.ai/builder
Repo: https://github.com/digitorn/digitorn-bridge

Roast it. What's missing? What's overkill? What pattern would you want
documented next?
```

### Topics (pick 3, max 4)

Required tags for the listing:

- AI Tools
- Developer Tools
- Open Source
- Productivity (or replace with API)

### Gallery (5 images)

1. **Hero image** (1270x760) — the canvas screenshot with a YAML pane on the left, graph on the right, all 5 lens tabs visible. Caption overlay: "Two ways in. One YAML. One click out."

2. **Builder conversational view** — chat panel with the conversational interview in progress, generated YAML visible on the side. Caption: "Describe your agent in plain language."

3. **Multi-agent canvas** — the canvas showing a coordinator + 3 specialists in parallel, edges animated. Caption: "Multi-agent dispatch is one tool call."

4. **Credentials vault** — screenshot of the credential picker showing the 4 scopes. Caption: "Credentials with audit trail. Not env vars."

5. **The before/after diff** — left pane: 380 lines of LangChain. Right pane: 45 lines of Digitorn YAML. Caption: "Same agent. 88% less code."

### Video (optional but doubles engagement)

60-second screen recording, no audio (PH plays muted by default):

- 0:00 Empty homepage
- 0:05 Click "Builder", builder opens
- 0:10 Type "I want a Slack helper that searches the web"
- 0:18 Builder generates YAML, canvas appears
- 0:25 Drag a new node onto canvas, YAML updates
- 0:35 Click "Deploy", success toast
- 0:40 Slack opens, user mentions @helper, agent answers
- 0:55 Cut to logo + URL

## Pre-launch checklist

- [ ] Maker profile complete (photo, bio, Twitter)
- [ ] Hunter confirmed (or accept self-launch)
- [ ] All 5 images uploaded, hero is autoplay GIF if possible
- [ ] Video uploaded
- [ ] Tagline + description finalised
- [ ] Topics selected
- [ ] First maker comment drafted in a doc, ready to paste
- [ ] Email subscribers list ready (do NOT send before 9 AM PT)
- [ ] Twitter post drafted with PH link
- [ ] Discord announcement drafted
- [ ] LinkedIn post drafted

## Launch day timeline (PT)

- **12:01 AM** — Listing goes live
- **12:05 AM** — Maker comment posted
- **12:10 AM** — Twitter post: "Just launched on Product Hunt: [link]"
- **6:00 AM** — Wake-up + reply to all overnight comments
- **8:00 AM** — Email subscribers (subject: "Launching Digitorn on Product Hunt today")
- **9:00 AM** — Cross-post to LinkedIn + relevant Slack/Discord communities
- **10:00 AM** — Twitter thread #2 with first 24h metrics
- **2:00 PM** — Twitter thread #3 with the "what people built" angle
- **6:00 PM** — Final Twitter post, screenshot of leaderboard position
- **9:00 PM PT (midnight ET)** — Last comment replies, thank-you Twitter post

## Success benchmarks

- **#1 of the day** = 100k+ impressions, 8-15k clicks, ~1500 stars on GitHub
- **Top 5** = 30-50k impressions, 3-5k clicks, ~500 stars
- **Top 10** = 10-20k impressions, 1-2k clicks, ~200 stars

Realistic target for a first launch with no built-in audience: top 10.
With Show HN front page first + PH next day, top 5 is achievable.

## Aftermath

- **Day 2-3**: write a "Product Hunt launch retrospective" blog post.
  Numbers, lessons, what surprised you. PH community loves this.
- **Day 7**: PH "Week's #1" badge if you got it. Add to website footer.
- **Day 30**: PH "Month's #1" badge if you got it. Same.

## Don'ts

- Don't ask friends to upvote without commenting (PH algorithm penalises).
- Don't run paid Twitter ads on launch day (looks desperate, bad signal).
- Don't link to a paywalled landing.
- Don't list a price (Digitorn is free; "free / open source" is in the description).
- Don't use emojis in the tagline.
- Don't relaunch within 6 months. PH allows it but consumes goodwill.
