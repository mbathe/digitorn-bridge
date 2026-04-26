# /feeds — Manage saved feed URLs

Sub-commands :

- `/feeds list` → recall and pretty-print the list stored under `rss.feeds`.
- `/feeds add <url> [<url> ...]` → recall the current list, append the new URL(s), dedupe, store back.
- `/feeds remove <url>` → recall, drop the URL, store back.
- `/feeds clear` → confirm with the user, then store an empty list.
- `/feeds preferences` → show current `rss.preferences` (max bullets, lang, etc.) and let the user update them.

Always report back the resulting list count: "✓ Saved 7 feeds. (was 5)".
