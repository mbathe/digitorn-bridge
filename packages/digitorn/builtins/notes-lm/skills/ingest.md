# Skill: Save a Source (FALLBACK only)

This skill is a FALLBACK. The default path for adding sources is the iframe's "+" button in the Sources sidebar (URL paste, file upload, text paste) and the chat composer's paperclip for file uploads (auto-mirrored under `attachments/`). The user normally manages their corpus without you.

Use this skill ONLY when the user pastes a URL or text in CHAT and explicitly asks you to save it ("save this URL", "ingest this article"). For ANY other request (paste text, upload file), redirect them to the iframe UI:

> "Use the **+** button in the Sources sidebar (left panel) to add this. The chat composer paperclip also works for files."

## URL ingest (the one legitimate agent-driven path)

Triggered when the user pastes a URL in chat and says "save", "add", "ingest", "import", or asks a question that requires reading a URL not yet in the corpus.

1. `web.fetch(url, format="markdown")` — returns `{title, content, length}`.
2. Build a slug from the title: lowercase, non-alphanumerics -> `-`, max 60 chars.
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

4. `WsWrite(path="attachments/<slug>.md", content=<above>)`.
5. Reply ONE line: `Saved: <title> (attachments/<slug>.md, ~<wordcount> words).`

Multiple URLs at once: parallel `web.fetch` + `WsWrite`, then ONE summary line: `Saved 3 sources: <slug1.md>, <slug2.md>, <slug3.md>`.

## Pasted text — REDIRECT to the iframe

If the user pastes a long text blob and asks you to save it, redirect:

> "Paste it into the **+ -> Text** tab in the Sources sidebar. That keeps your corpus management in the same place as everything else."

Don't save text yourself. The iframe handles it cleanly with a title + body + auto-slug.

## File uploads — REDIRECT to the composer

If the user describes a file they want to add, redirect:

> "Drag-drop it into the chat composer (paperclip icon) or use the **+ -> Upload** tab. PDFs/DOCX get extracted and land under `attachments/` automatically."

## Don'ts

- Don't summarise on ingest. That's for the briefing skill.
- Don't re-save the same URL twice. Use `WsGlob("attachments/**")` to check first.
- If a fetch returns >100k chars, save the first ~80k and tell the user.
- If a URL is paywalled / 403, save the title + URL + error message in the markdown and continue.

## Auto-suggest after a successful save

- After 1 source: "Try a briefing or just ask a question."
- After 3+: "A mind map will reveal how these sources relate."
- After 5+: "Try a timeline if events have dates, or a study guide for revision."
