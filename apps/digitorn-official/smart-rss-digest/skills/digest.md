# /digest — Build today's brief from saved feeds

Fetch every URL stored under `memory.recall("rss.feeds")`, build the topic-clustered digest as defined in your system prompt, and return it.

If the user has no saved feeds, ask them to provide URLs or use one of the quick prompts.

If the user added arguments after `/digest` (e.g. `/digest --topic AI`), interpret them:
- `--topic <kw>` → keep only items matching the keyword in the headline or summary
- `--lang fr|en` → force the digest language
- `--max <N>` → cap total bullets at N (default 10)
- `--since <hours>` → time window in hours (default 36)

Always end with the TL;DR line.
