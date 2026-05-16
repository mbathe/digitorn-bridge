# Skill: Save a Source

Triggered when the user:
- Pastes a URL ("Add this: https://...")
- Says "save this", "import", "ingest"
- Sends raw text as a source ("save this text as a source")

Attachments uploaded via the composer DO NOT need this skill — they land automatically under `attachments/` and you read them directly with `WsRead`. Only use this skill for URLs and pasted text.

## Steps

### URL

1. `web.fetch(url, format="markdown")` — returns `{title, content, length}`.
2. Build a slug from the title: lowercase, non-alphanumerics → `-`, max 60 chars.
3. Build the markdown source file:

   ```markdown
   ---
   url: <original_url>
   title: <title>
   added_at: <iso-now>
   ---

   # <title>

   <content>
   ```

4. `WsWrite(path="sources/<slug>.md", content=<above>)`.
5. Reply ONE line: `Saved: <title> (sources/<slug>.md, ~<wordcount> words).`

### Pasted text

1. Ask for a label if not obvious (1 line, max).
2. Build a slug from the label.
3. `WsWrite(path="sources/<slug>.md", content=<text with a minimal frontmatter title>)`.

### Multiple URLs at once

Parallel `web.fetch` + `WsWrite`, then ONE summary line: `Saved 3 sources: <slug1.md>, <slug2.md>, <slug3.md>`.

## Don'ts

- Don't summarise on ingest. That's for `briefing`. Ingest = save.
- Don't re-save the same URL twice. Use `WsGlob("sources/**")` to check.
- If a fetch returns >100k chars, save the first ~80k and tell the user (1 line).
- If a URL is paywalled / 403, save the title + URL + error message and continue.

## Auto-suggest after save

After 1 source: "Try /briefing or just ask a question."
After 3+: "/mindmap reveals their relationships."
After 5+: "/timeline if events have dates, or /study_guide for revision."
