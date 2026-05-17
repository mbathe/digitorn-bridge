# Skill: Chronological Timeline

Triggered by "timeline", "chronology", "what happened when", "order of events".

## Steps

1. `WsGlob("attachments/**")` to discover the corpus.
2. `WsRead` each file.
3. Extract every `(date, event)` pair. Be conservative — only events with explicit dates (year minimum).
4. Sort ascending.
5. Write `timeline.md`:

   ```markdown
   # Timeline

   | Date | Event | Source |
   |---|---|---|
   | 2023-04-12 | <event> | [^1] |
   | 2023-09 | <event> | [^2] |
   | 2024 | <event> | [^1][^3] |
   | 2020s | <event> | [^2] |

   ## Sources

   [^1]: attachments/foo.md:L42-L42 — "verbatim quote that mentions the date"
   [^2]: attachments/report.pdf:p.7 — "..."
   ```

6. Reply ONE line: `Timeline written to timeline.md (<N> events from <M> sources).`

## Date format rules

- Full: `2023-04-12`
- Month: `2023-04`
- Year-only: `2023`
- Quarter: `2023-Q2` (only if the source says so)
- Decade: `2020s` (only if no narrower date is given)

NEVER guess. If the source says "a few years ago", SKIP the event.

## Verbatim citation

The quote in the footnote MUST contain the date phrase from the source. That's how the user verifies you didn't invent the date.

## When no dated events

If <3 dated events emerge, decline:

`"No clear chronology in this corpus. Add sources that mention specific dates."` (1 line)
