# Distribution targets — Digitorn launch

Ordered by **impact per hour of effort**. Hit Tier 1 in week 1, Tier 2 in
weeks 2-4, Tier 3 ongoing.

---

## Tier 1 — high impact, ship in week 1

### 1.1 Hacker News Show HN

- **Channel**: news.ycombinator.com/submit
- **Day/time**: Tuesday or Wednesday, 8-10 AM ET
- **Asset**: see `show-hn-draft.md`
- **Goal**: 100+ upvotes = front page = ~50k impressions, 3-5k clicks, 10-30 quality backlinks
- **Watch**: be on for the first 4 hours to reply to comments

### 1.2 Product Hunt launch

- **Channel**: producthunt.com/launches
- **Day**: Tuesday (best for dev tools, less crowded than Mon/Wed)
- **Time**: 12:01 AM PT (full 24h on the leaderboard)
- **Asset**: see `product-hunt-draft.md` (next file)
- **Hunter**: ask someone with 1k+ followers (look at past Vercel, Resend, Linear PH launches for hunter recs). Self-launch works but rallies less.
- **Goal**: top 5 of the day = ~30k impressions, 2-3k clicks, badge for the website

### 1.3 GitHub README + repo polish

- **Already done**: README rewritten as landing
- **Still to do**:
  - GitHub topics (settings → topics): `ai-agents`, `llm-framework`, `yaml`, `multi-agent`, `claude`, `openai`, `agent-framework`, `python`, `self-hosted`, `mcp`
  - Repo description (settings): "Build AI agent apps in YAML. Run them on a self-hosted runtime."
  - Pinned issue: "Roadmap & feedback welcome"
  - Discussions enabled (settings → features)
  - Social preview image (settings → general → social preview): 1280x640 PNG with logo + tagline
  - License badge in README (already there)
  - First release tagged on GitHub (`v1.0.0`)

### 1.4 Awesome lists — submit PRs

Each PR is one alphabetical insert + a one-line description. Cost: 30
min per submission. Several may be merged in 24h.

| List | URL | Section to target |
|------|-----|-------------------|
| awesome-llm-apps | github.com/Shubhamsaboo/awesome-llm-apps | "AI Agent Frameworks" |
| awesome-ai-agents | github.com/e2b-dev/awesome-ai-agents | "Frameworks" |
| awesome-llm | github.com/Hannibal046/Awesome-LLM | "LLM Tools" |
| awesome-langchain | github.com/kyrolabs/awesome-langchain | "Alternatives" |
| awesome-mcp-servers | github.com/punkpeye/awesome-mcp-servers | "MCP-compatible runtimes" |
| awesome-claude | github.com/promptslab/Awesome-Claude | "Tools using Claude" |
| awesome-self-hosted | github.com/awesome-selfhosted/awesome-selfhosted | "AI / LLMs" |
| awesome-yaml | github.com/dreikanter/awesome-yaml | "Tools" |
| awesome-python | github.com/vinta/awesome-python | "AI / Agent" |

PR template:

```markdown
- [Digitorn](https://github.com/digitorn/digitorn-bridge) - Declarative
  AI agent framework. Build multi-agent apps in YAML, run them on a
  self-hosted Python runtime. Built-in credentials vault, channels
  (Slack/email/webhook), MCP support, visual builder.
```

### 1.5 GitHub trending hack

After Show HN + PH bring stars, the project lands on github.com/trending
under `python`. To maximise the chance:

- Stars need to come **fast** in the first 24h after launch (trending uses velocity, not absolute count).
- Encourage stars in the Show HN body and PH page.
- Post to /r/programming, /r/Python with the launch.

---

## Tier 2 — high impact, weeks 2-4

### 2.1 Reddit

| Subreddit | Subscribers | Pitch angle | When |
|-----------|-------------|-------------|------|
| r/LocalLLaMA | 350k+ | Self-hosted multi-agent runtime | Tue/Wed AM ET |
| r/MachineLearning | 2.7M | Architecture deep-dive: behavior engine + hooks | Sun PM ET |
| r/Python | 1.3M | Python 3.12 framework, declarative DSL via YAML | Mon AM ET |
| r/programming | 5.8M | Self-hosted alternative to LangChain | Tue AM ET |
| r/devops | 320k | Cron + webhook + channel triggers | Wed AM ET |
| r/selfhosted | 380k | Open source, runs on your hardware | Thu AM ET |
| r/ChatGPTPro | 350k | Build your own custom GPT, self-hosted | Wed PM ET |
| r/AI_Agents | 50k | Multi-agent dispatch in YAML | Anytime |

Rules: no link to your own product if the subreddit forbids it. Always
read sticky rules first. Engage on others' posts for 2 weeks before
posting yours.

### 2.2 Dev.to + Medium + Hashnode

Cross-post from your blog (already SEO-clean). Adapt the title for
each platform:

- **dev.to**: "I rebuilt LangChain in YAML. Here's what I learned."
- **Medium**: same
- **Hashnode**: "Why your AI agent framework should be a config file"

Each republished post gets a canonical link back to digitorn.ai/blog/X
so the SEO juice stays with the source.

Articles to cross-post first (highest engagement potential):

1. "10 Apps You Can Build in YAML" → Show, don't tell.
2. "Multi-agent systems in 50 lines of YAML"
3. "Credentials vault for AI agents"
4. The migration guides (LangChain, CrewAI, OpenAI Assistants)

### 2.3 Twitter / X

Daily thread for 2 weeks during launch:

- Day 1: Show HN + PH launch announcement.
- Day 2: "Migrating a 380-line LangChain app to 45 lines of YAML" thread with screenshots.
- Day 3: "5 view lenses on the same agent graph" with canvas screenshots.
- Day 4: "8 production patterns every agent app needs" with the /patterns links.
- Day 5: Builder demo video (60 seconds, screen recording, no audio).
- Day 6: Architecture deep-dive on the behavior engine.
- Day 7: User testimonials / "what people built this week".
- Day 8-14: One concrete tip per day from the docs.

Tag relevant accounts in replies, not in main posts: @LangChainAI, @AnthropicAI (for Claude support), @OpenAIDevs, @code (Cursor), @swyx (writes about agents), @karpathy (occasionally amplifies dev tools).

### 2.4 YouTube

A 5-7 minute walkthrough video on the official channel:

- 0:00-0:30 Hook: "I built an AI agent in 12 lines of YAML. Watch."
- 0:30-2:00 Live demo: paste YAML, hit deploy, agent answers in Slack.
- 2:00-4:00 Multi-agent example: research crew with the canvas open.
- 4:00-5:00 Builder demo: describe an agent, watch it generate.
- 5:00-6:00 Comparison: same app in LangChain (here's the diff).
- 6:00-7:00 Install + try it.

Submit to:
- YouTube directly
- Embedded on the homepage hero
- Linked from Show HN / PH / Twitter

### 2.5 Podcasts (request slot, takes 4-8 weeks to land)

| Podcast | Audience | How to pitch |
|---------|----------|--------------|
| Latent Space (swyx) | AI engineers | Email with "declarative agents" angle |
| The Pragmatic Engineer | Senior engineers | Pitch the "self-hosted alternative" angle |
| Practical AI | Broad ML | Multi-agent + production patterns |
| MLOps Community Podcast | MLOps practitioners | Audit + cost ceilings angle |
| Software Engineering Daily | General SWE | Builder + visual canvas angle |
| AI Engineer Podcast | AI engineers | Hooks + behavior engine technical depth |

Pitch template:

```
Subject: AI agent framework as YAML config — guest pitch?

Hi [name],

I built Digitorn (Apache 2.0, github.com/digitorn/digitorn-bridge):
a Python runtime that turns one YAML file into a multi-agent app
with channels, hooks, credentials, and a visual builder.

Story angles I think your audience would find useful:
1. What changes when the agent runtime owns the cron/webhook layer
   instead of the agent owning a framework.
2. Why I rebuilt LangChain in YAML and what surprised me along the way.
3. The credentials vault pattern (4 scopes, hash-chained audit) that
   most agent stacks are missing.

Happy to do 30 min recording, weekday afternoon ET. Any of these resonate?

Paul (mbathepaul@gmail.com)
```

---

## Tier 3 — ongoing flywheel

### 3.1 SEO-grade comparison & "alternative" articles on third-party sites

These already exist on /vs and /alternatives-to but the third-party
versions matter for backlink diversity:

- AlternativeTo.net (submit Digitorn)
- StackShare (submit Digitorn, link tools, link integrations)
- G2 / Capterra (only when you have user reviews)
- Slant.co
- TecMint, Linode, DigitalOcean tutorials (write a "deploy Digitorn on a $5 droplet" post and pitch it to them)

### 3.2 Guest posts (one per month)

Pitch the editor of:

- InfoQ
- The New Stack
- Towards Data Science (Medium)
- AnthropicAI's blog (they sometimes feature ecosystem tools)
- Vercel's blog (they've featured smaller dev tools)
- The Pragmatic Engineer newsletter (sponsored slot)
- Bytes / TLDR newsletter (free if newsworthy, paid otherwise)

Each post embeds 1-2 backlinks to digitorn.ai with branded anchor text.

### 3.3 Conference talks

Proposed talks:

1. "From 380 lines of LangChain to 45 lines of YAML" — PyCon, Strange Loop, AI Engineer Summit
2. "Hooks for AI agents: 15 events that prevent runaway costs" — local Python meetups, MLOps World
3. "Multi-agent dispatch is just a tool call" — AI Engineer Summit, local AI meetups

Submit CFPs:
- AI Engineer Summit (sf.aiengineer.dev)
- Strange Loop (thestrangeloop.com)
- PyCon US + EU + local
- MLOps World (mlopsworld.com)
- The Linux Foundation AI conferences

### 3.4 Open source flywheel

- Pin a "good first issue" filter so newcomers can find something to do
- Publish a CONTRIBUTORS.md with all merged contributors named
- Quarterly "what shipped this quarter" blog post
- Roadmap publicly visible
- Discord (already linked) for community
- Monthly community call

### 3.5 Search Console + analytics

Day 1 actions:

- Add property to search.google.com/search-console
- Submit sitemap.xml
- Request indexing for: /, /builder, /hub, /templates, /patterns, /migrate-from/langchain, /migrate-from/crewai
- Plug Plausible or umami (privacy-friendly, lightweight) into the Next.js app
- Set up Google Search Console weekly digest emails

Day 7 review: which queries are surfacing? Adapt page titles if
something unexpected ranks.

---

## Anti-patterns (do not do)

- **Buying backlinks** — Google penalises quickly, irreversible damage.
- **Affiliate spam** — distrusted by HN audience, will get downvoted hard.
- **Link exchanges with unrelated sites** — same penalty risk.
- **Comment spam on Reddit / forums** — bans incoming.
- **Paid Twitter promo on launch day** — looks desperate, bad signal.
- **Generic "AI" hashtag spam** — ignored by serious devs.
- **Releasing before the docs are good** — wastes the launch window.
- **Multiple Show HN posts** — kills the algo for the project.

---

## Week-by-week launch calendar

### Week 0 (preparation)

- [ ] README polished, badges, social preview image
- [ ] Set up GitHub Discussions
- [ ] Tag v1.0.0 release with changelog
- [ ] Search Console set up + sitemap submitted
- [ ] Plausible/umami analytics installed
- [ ] YouTube demo recorded
- [ ] Twitter account warmed up (10 posts before launch)
- [ ] Discord set up with channels: #general, #help, #showcase, #contributors

### Week 1 (launch)

- [ ] Tuesday 8 AM ET: Show HN
- [ ] Wednesday 12:01 AM PT: Product Hunt launch
- [ ] Same day: announce on Twitter, tag PH
- [ ] Same day: post to /r/programming, /r/Python
- [ ] Same day: submit awesome-list PRs (9 lists)

### Week 2

- [ ] Cross-post 2 articles to dev.to / Hashnode
- [ ] Post to /r/LocalLLaMA, /r/MachineLearning
- [ ] Twitter daily threads continue
- [ ] Reach out to 5 podcasts

### Week 3

- [ ] Cross-post 2 more articles
- [ ] Pitch InfoQ or The New Stack guest post
- [ ] Reply to all GitHub issues / discussions from the launch
- [ ] First "what shipped" digest blog post

### Week 4

- [ ] Submit to AlternativeTo, StackShare, Slant
- [ ] Schedule first conference CFP submissions
- [ ] Email everyone who starred the repo asking what they'd build
  (use github.com API, not a scrape)
- [ ] Review GSC for ranking surprises, adapt page titles

---

## Success metrics

After 30 days:

- **GitHub stars**: target 1k (HN front page can deliver 500-2k for dev tools).
- **Domain referring backlinks**: target 30+ (Ahrefs / Moz free tier shows them).
- **Indexed pages on Google**: 100+ of the ~165 in sitemap.
- **Organic clicks**: 200/day baseline.
- **Discord members**: 200+.
- **Issues opened**: 30+ (proof people are using it).

After 90 days:

- **Stars**: 3-5k.
- **Backlinks**: 100+.
- **Indexed**: 100% of sitemap.
- **Organic clicks**: 1k/day.
- **Real production deploys**: 50+ teams (track via opt-in telemetry if shipping).

If after 90 days you're below 50% of these, the issue is not content,
it's discovery: do another launch wave with new angles.
