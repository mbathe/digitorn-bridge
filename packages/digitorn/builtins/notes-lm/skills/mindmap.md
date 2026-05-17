# Skill: Mind Map

Triggered by "mind map", "graph", "visualise structure", "relationships".

## Steps

1. `WsGlob("attachments/**")` to find the corpus.
2. `WsRead` each file (sample if >10).
3. Identify 3-6 top-level themes that cut across the corpus.
4. For each theme, pick 2-5 sub-concepts. Every leaf cites a source.
5. Write `mindmap.md`:

   ````markdown
   # Mind Map

   ```mermaid
   mindmap
     root((<main subject>))
       Theme A
         Concept A.1 [^1]
         Concept A.2 [^2]
       Theme B
         Concept B.1 [^3]
         Concept B.2 [^1][^3]
       Theme C
         ...
   ```

   ## Sources

   [^1]: attachments/foo.md:L42-L42 — "..."
   [^2]: attachments/report.pdf:p.7 — "..."
   ````

6. Reply ONE line: `Mind map written to mindmap.md (<N> themes, <M> leaves).`

## Mermaid gotchas

- Indentation is significant. 2 spaces per level.
- No emojis or special chars in node labels (Mermaid breaks). Alphanumerics + spaces + parens only.
- Citations as `[^1]` appended to the label after a space.
- `root((label))` uses double parens for a cloud-shape root.

## When the corpus is too sparse

If <3 files or <5 distinct themes emerge, decline:

`"The corpus is too thin for a useful mind map. Add 2-3 more sources covering different angles."` (1 line)
