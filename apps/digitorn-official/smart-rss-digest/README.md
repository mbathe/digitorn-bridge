# Smart RSS Digest

> Aggregates RSS/Atom feeds, clusters articles by topic with an LLM, and delivers a tight 5-minute brief in your language.

## What it does

You give it a list of feed URLs (or save them once for re-use). It fetches them, drops anything older than 36 h, clusters the rest into topical groups, and returns a markdown digest you can read in five minutes.

It's conversational: ask follow-ups ("more about X", "translate to French", "drop ycombinator from my list"), and it remembers your preferences across runs.

## Quick start

After installing on your daemon :

```
/digest                         # use your saved feeds
/feeds add https://...          # add a feed for next time
/dive <feed_url> <topic>        # expand on one topic
```

Or just talk to it :

> "Build me a brief from these AI feeds: anthropic.com/news/rss.xml, openai.com/blog/rss.xml. French please, max 6 bullets."

## What it can NOT do

- It does not crawl arbitrary websites - feeds only.
- It does not save digests to disk (output stays in chat). If you want disk output, install with `filesystem` granted.
- It does not send the brief by email/Slack - pair it with a channel module if you need that.

## Architecture

- **One agent**, conversational mode.
- **Brain**: DeepSeek `deepseek-chat` by default (fast, multilingual, cheap). Anthropic Haiku and OpenAI gpt-4o-mini are also recommended.
- **Modules**: `http` (feed fetching), `memory` (saved feeds + preferences), `context_builder`.
- **Permissions**: `risk_level: low`, network only, no filesystem, no shell.

## Provenance

Built and maintained by the Digitorn team. Source: `apps/digitorn-official/smart-rss-digest/` in the [digitorn-bridge repo](https://github.com/digitorn/digitorn-bridge).
