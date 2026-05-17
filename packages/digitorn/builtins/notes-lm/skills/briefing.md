# Skill: Briefing Document

Triggered by "briefing", "summary", "summarize my sources", "executive summary".

## Steps

1. `WsGlob("attachments/**")` to discover the corpus.
2. `WsRead` each (up to ~10 files; if more, sample by recency or ask user to focus).
3. Compose a markdown document:

   ```markdown
   # Briefing · <iso date>

   ## In one paragraph
   <3-4 sentences synthesising the corpus, every fact cited>

   ## Key claims
   - claim 1 [^1][^3]
   - claim 2 [^2]
   - claim 3 [^1][^2]

   ## Tensions / disagreements
   - <where sources contradict each other> [^1][^2]

   ## Open questions
   - <what the corpus does NOT settle>

   ## Sources

   [^1]: attachments/foo.md:L42-L46 — "verbatim quote"
   [^2]: attachments/report.pdf:p.14 — "verbatim quote"
   ```

4. `WsWrite(path="briefing.md", content=<above>)`.
5. Reply ONE line: `Briefing written to briefing.md (<N> sources, ~<wordcount> words).`

## Rules

- Citations are MANDATORY, including in the "in one paragraph" section.
- "Tensions" and "Open questions" sections are MANDATORY — they make the briefing honest.
- If <2 sources, write what you can and tell the user (1 line) that more sources would deepen the briefing.
- If user asks a focused brief ("brief me on X aspect only"), only read files matching X.
- Never invent the dates / authors / stats. If not in the corpus, omit.

## Bullet density

Aim for 5-10 key claims. More than 15 = the briefing is too dense, splits into sections by theme.
