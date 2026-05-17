# Skill: Answer Grounded Question

Default skill for ANY question that isn't a /briefing or /audio_overview etc.

## Steps

### 1. Discover what's available

Call `WsGlob("attachments/**")`. If empty, refuse:

`"No sources yet. Paste a URL or upload a file, then ask again."` (1 line)

### 2. Pick the relevant files

You have the file list with names. Decide which to read based on the question. Heuristics:

- File name semantically matches a question term → read it
- User mentioned a file by name → only read that one
- Question is broad ("summarise everything") → read all (up to ~5 files in one turn; if more, use briefing skill instead)

Be CONSERVATIVE. Don't read every file every turn — it bloats the context.

### 3. Read the file(s)

`WsRead(path="<path>")` — for text/md, returns line-numbered output `   1\t<line>`. For PDFs, returns page-marked text.

### 4. Compose the answer

- 2-6 sentences, every claim followed by `[^n]`.
- Footnote block at the end:

  ```text
  [^1]: attachments/foo.md:L42-L46 — "verbatim quote"
  [^2]: attachments/report.pdf:p.14 — "verbatim quote"
  ```

### 5. Strict rules

- VERBATIM quote in footnote. Not paraphrased.
- If a quote isn't in the file, do not invent it. Refuse the citation: `[^?]` with a note "I couldn't find a direct passage; this answer is synthesised".
- If multiple files contribute, cite each with its own footnote.
- Don't dump full file contents. Cite the smallest passage that supports the claim.

## When the user wants longer

"more detail" / "expand" → re-read the same files but produce 2-3 paragraphs, same citation rules.

## When the user goes off-corpus

"give me your opinion" / "speculate" → prefix `[off-corpus]`, mark speculative sentences. Resume grounded on next turn.

## Cross-file synthesis

If files disagree, dramatise the disagreement, cite both:

`Anthropic emphasizes X [^1], while OpenAI takes the opposite stance [^2].`
