# Skill: Study Guide

Triggered by "study guide", "revision", "quiz me", "test myself", "key concepts".

## Steps

1. `WsGlob` + `WsRead` the corpus.
2. Extract:
   - **Key concepts** (terms + 1-line definitions) — aim for 5-15.
   - **FAQ** — 5-10 likely user questions + answers.
   - **Quiz** — 10 questions of mixed difficulty.
3. Write `study_guide.md`:

   ````markdown
   # Study Guide · <main subject>

   ## Key concepts

   | Term | Definition | Source |
   |---|---|---|
   | <term> | <1-line def> | [^1] |
   | ... |

   ## Frequently asked questions

   **Q1: <natural question>**
   A: <answer> [^1][^2]

   **Q2: ...**

   ## Quick quiz (10 questions)

   1. <question> *(answer at bottom)*
   2. ...

   ---

   ### Quiz answers

   1. <short answer> [^1]
   2. ...

   ## Sources

   [^1]: attachments/foo.md:L42-L42 — "verbatim quote"
   ````

4. Reply ONE line: `Study guide written to study_guide.md (<N> concepts, <M> FAQ, 10 quiz questions).`

## Rules

- Every term, FAQ answer, quiz answer cites a source via `[^n]`.
- Quiz questions are mixed: 4 recall (easy), 4 understanding (medium), 2 application (hard).
- Don't make trick questions. Clear = better study.
- If corpus too thin (<5 chunks of useful content across <2 files), refuse with 1 line.
