"""distill.py - turn long-form Digitorn docs into atomic concept cards.

Reads one (or all) markdown files under ``docs/`` and asks Claude to
produce a set of short, RAG-ready concept cards in the strict template
defined in ``knowledge_base/README.md``.

Each card lands as ``knowledge_base/concepts/<id>.md``.

Usage::

    # One file (smoke test)
    python knowledge_base/distill.py docs/app-language/09-triggers.md

    # Several files
    python knowledge_base/distill.py docs/app-language/09-triggers.md docs/app-language/38-background-sessions.md

    # All app-language + module reference docs
    python knowledge_base/distill.py --all

    # Dry run (don't write, just print)
    python knowledge_base/distill.py --dry-run docs/app-language/09-triggers.md

The script uses Claude via the same OAuth path as the daemon
(``~/.claude/.credentials.json``), so it just works on a dev machine
that has Claude Code authenticated. No API key needed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONCEPTS_DIR = REPO_ROOT / "knowledge_base" / "concepts"
DOCS_DIR = REPO_ROOT / "docs"

# Default model - Sonnet 4.6 for the right speed/quality trade-off. The
# script may run on ~80 doc files per regeneration so latency matters,
# and the distillation task (template-following + summarisation) is
# squarely within Sonnet's strength. Override with --model if needed.
MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 16_000

DISTILL_SYSTEM_PROMPT = """You are a technical-documentation distiller for the Digitorn AI agent framework.

Your job: read a long-form documentation file and emit a SET of short,
atomic concept cards optimized for retrieval-augmented generation (RAG).

Each card describes EXACTLY ONE concept. If a doc file covers 8 concepts,
you emit 8 cards. Never bundle two concepts into the same card.

==================================================================
STRICT OUTPUT FORMAT
==================================================================

Output ONE OR MORE cards, each delimited by `---CARD---` / `---END---`
markers exactly as shown below. Use markdown inside each card. No
preamble before the first card, no commentary between cards, no text
after the last card.

---CARD---
id: kebab-case-unique-id
title: Human-readable Title
keywords: keyword1, keyword2, keyword3, keyword4, keyword5
related: other-card-id-1, other-card-id-2

# Human-readable Title

## What it is
ONE paragraph max. Direct, factual, no fluff.

## When to use
- concrete situation 1
- concrete situation 2
- concrete situation 3

## YAML
```yaml
# A minimal, correct snippet that compiles as-is
triggers:
  - id: hourly
    type: cron
    schedule: "0 * * * *"
```

## Gotchas
- real pitfall 1 - what bites you in production
- real pitfall 2
---END---

Each card has a header block (4 lines: id/title/keywords/related)
followed by an empty line followed by the markdown body. The markers
`---CARD---` and `---END---` must appear on lines by themselves.

==================================================================
RULES
==================================================================

1. ATOMICITY. One concept per card. If you find yourself writing
   "and also" in a card, split it.

2. THE id IS THE FILENAME. Kebab-case, max 60 chars, descriptive.
   Examples: trigger-cron, payload-schema, session-mode-mono,
   capabilities-grant, background-mode-overview.

3. KEYWORDS BOOST RECALL. Comma-separated synonyms a user might type.
   For "cron trigger" include: cron, schedule, hourly, daily, periodic,
   recurring, crontab. Aim for 5-10 keywords.

4. WHAT IT IS = ONE TIGHT PARAGRAPH (~40-80 words). Not bullet points,
   not three paragraphs. Direct, factual.

5. WHEN TO USE = CONCRETE SITUATIONS, not abstract advice.
   Bad:  "use when you need scheduling"
   Good: "use for a job scraper that polls a website every hour"

6. YAML EXAMPLES MUST BE CORRECT. They will be tested against the
   compiler. Use exactly the syntax the source doc shows. Never invent
   fields. Wrap in a ```yaml fenced block.

7. GOTCHAS = REAL PITFALLS. Things that bite people in production. If
   the source doc mentions none, omit the section entirely - do NOT
   invent gotchas to fill space.

8. RELATED = COMMA-SEPARATED kebab-case ids of other cards that the
   reader might want next. Can reference cards in this same response
   or cards you expect to exist later.

9. STAY GROUNDED. Do not invent features. Do not extrapolate. If the
   source is ambiguous, write the most conservative reading. Better to
   skip a card than to lie.

10. SPLIT GENEROUSLY. A 200-line doc usually distills into 5-15 cards,
    not 1-2. Each section heading is usually a card. Each independently
    useful concept is a card.

==================================================================
EXAMPLE - what good output looks like for a tiny input
==================================================================

---CARD---
id: trigger-cron
title: Cron Trigger
keywords: cron, schedule, hourly, daily, periodic, recurring, crontab
related: payload-schema, broadcast-routing, background-mode-overview

# Cron Trigger

## What it is
Cron triggers fire the agent on a recurring schedule using standard 5-field cron syntax (parsed by croniter). They are the simplest way to run an agent at fixed intervals - every hour, every morning, every Monday at 9am.

## When to use
- Job scrapers that poll websites every hour
- Daily summary emails sent at 8am
- Periodic monitoring of an API that has no webhook

## YAML
```yaml
triggers:
  - id: hourly
    type: cron
    schedule: "0 * * * *"
    routing: broadcast
    message: "Time to check the job board."
```

## Gotchas
- Without payload_schema every user gets the SAME generic message - combine with payload_schema for per-user personalization
- routing: broadcast fires for ALL active sessions, watch out for token cost at scale
- Use max_concurrent_activations to throttle large broadcasts
---END---

NOW DO THE SAME FOR THE DOC THAT FOLLOWS. Output cards only - no
preamble, no commentary, no closing text.
"""


# ────────────────────────────────────────────────────────────────────
# OAuth - same path the daemon uses
# ────────────────────────────────────────────────────────────────────


def load_claude_oauth_token() -> str:
    """Return the Claude Code OAuth access token from ``~/.claude/.credentials.json``.

    Mirrors ``packages/digitorn/modules/llm_provider/providers/anthropic.py``
    so this script behaves identically to the daemon - no API key needed
    on a dev machine that has Claude Code installed.
    """
    candidates = [
        Path.home() / ".claude" / ".credentials.json",
        Path.home() / ".claude" / "credentials.json",
    ]
    for cred_path in candidates:
        if not cred_path.is_file():
            continue
        try:
            data = json.loads(cred_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[distill] cannot read {cred_path}: {exc}", file=sys.stderr)
            continue

        oauth = data.get("claudeAiOauth", {})
        token = oauth.get("accessToken")
        if not token:
            continue

        expires_at = oauth.get("expiresAt", 0)
        if isinstance(expires_at, (int, float)) and expires_at > 0:
            if time.time() > expires_at / 1000.0:
                print(
                    "[distill] Claude Code OAuth token expired. "
                    "Run 'claude' to refresh.",
                    file=sys.stderr,
                )
                continue
        return token

    raise RuntimeError(
        "No Claude Code OAuth token found. Either:\n"
        "  - run `claude` to authenticate, OR\n"
        "  - set the ANTHROPIC_API_KEY environment variable"
    )


# ────────────────────────────────────────────────────────────────────
# LLM call
# ────────────────────────────────────────────────────────────────────


def call_claude(source_path: Path, source_text: str) -> list[dict]:
    """Send the source doc to Claude and return the parsed list of cards."""
    try:
        from anthropic import Anthropic
    except ImportError:
        raise SystemExit(
            "anthropic SDK not installed. Run: pip install anthropic"
        )

    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    using_oauth = False
    if not api_key:
        api_key = load_claude_oauth_token()
        using_oauth = True

    client_kwargs: dict = {"api_key": api_key, "max_retries": 10}
    if using_oauth:
        # Mimic Claude Code's headers so the OAuth token is accepted
        client_kwargs["default_headers"] = {
            "x-app": "cli",
            "User-Agent": "claude-cli/1.0.34 (external, cli)",
            "anthropic-beta": "oauth-2025-04-20,claude-code-20250219",
        }
    client = Anthropic(**client_kwargs)

    user_message = (
        f"Source file: {source_path.relative_to(REPO_ROOT)}\n"
        f"========\n\n"
        f"{source_text}\n\n"
        f"========\n"
        f"Distill the above into atomic RAG concept cards. JSON array only."
    )

    print(
        f"[distill] calling Claude ({MODEL}, {len(source_text)} chars input)...",
        file=sys.stderr,
    )
    t0 = time.time()
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=DISTILL_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    elapsed = time.time() - t0
    print(
        f"[distill] response in {elapsed:.1f}s "
        f"(in={response.usage.input_tokens}, out={response.usage.output_tokens})",
        file=sys.stderr,
    )

    raw = "".join(b.text for b in response.content if hasattr(b, "text"))
    return parse_cards(raw)


_CARD_BLOCK_RE = re.compile(
    r"---CARD---\s*\n(?P<body>.*?)\n---END---",
    re.DOTALL,
)


def parse_cards(raw: str) -> list[dict]:
    """Split Claude's response into card dicts.

    Each card is a block delimited by ``---CARD---`` / ``---END---``.
    The first 4 lines after the opening marker are the header
    (``id:``, ``title:``, ``keywords:``, ``related:``), then a blank
    line, then the markdown body that already follows our card
    template.

    We deliberately use a markdown-delimited format instead of JSON so
    that YAML examples with embedded newlines / quotes / backticks
    don't blow up the parser - Claude is markedly better at producing
    long fenced-code blocks than at hand-escaping JSON strings.
    """
    cards: list[dict] = []
    for match in _CARD_BLOCK_RE.finditer(raw):
        body = match.group("body").strip("\n")
        header, _, markdown = body.partition("\n\n")
        if not markdown:
            # No blank-line separator - try splitting after the 4 header lines.
            lines = body.split("\n")
            header = "\n".join(lines[:4])
            markdown = "\n".join(lines[4:]).lstrip("\n")

        meta = _parse_header(header)
        if not meta.get("id"):
            # Skip cards without an id rather than crashing - the LLM
            # occasionally emits a malformed first card while it
            # "warms up" to the format.
            continue
        meta["markdown"] = markdown.rstrip() + "\n"
        cards.append(meta)

    if not cards:
        raise RuntimeError(
            "No cards found in Claude's response. Expected at least one "
            "block delimited by ---CARD--- / ---END---.\n\n"
            f"--- raw (first 1500 chars) ---\n{raw[:1500]}"
        )
    return cards


def _parse_header(header: str) -> dict:
    """Parse the 4-line header block at the top of a card."""
    out: dict = {"id": "", "title": "", "keywords": [], "related": []}
    for line in header.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "id":
            out["id"] = value
        elif key == "title":
            out["title"] = value.strip('"').strip("'")
        elif key == "keywords":
            out["keywords"] = [k.strip() for k in value.split(",") if k.strip()]
        elif key == "related":
            out["related"] = [k.strip() for k in value.split(",") if k.strip()]
    return out


# ────────────────────────────────────────────────────────────────────
# Card → markdown file
# ────────────────────────────────────────────────────────────────────


def card_to_markdown(card: dict, source: str) -> str:
    """Wrap a parsed card with proper YAML frontmatter.

    The markdown body is already produced by Claude in our template
    format (``# Title`` / ``## What it is`` / etc.), so we just prepend
    the frontmatter block and return the whole thing.
    """
    cid = card.get("id", "").strip() or "untitled"
    title = card.get("title", "").strip() or cid
    keywords = card.get("keywords", []) or []
    related = card.get("related", []) or []
    body = card.get("markdown", "").rstrip()

    frontmatter = "\n".join([
        "---",
        f"id: {cid}",
        f'title: "{title}"',
        "type: concept",
        f"keywords: [{', '.join(keywords)}]",
        f"related: [{', '.join(related)}]",
        f"source: {source}",
        "---",
        "",
    ])
    return frontmatter + body + "\n"


def safe_id(raw_id: str) -> str:
    """Sanitize an id into a safe filename."""
    sid = re.sub(r"[^a-z0-9-]", "-", raw_id.lower())
    sid = re.sub(r"-+", "-", sid).strip("-")
    return sid or "untitled"


# ────────────────────────────────────────────────────────────────────
# Driver
# ────────────────────────────────────────────────────────────────────


def distill_file(source_path: Path, dry_run: bool) -> int:
    """Distill one doc file. Returns the number of cards written."""
    text = source_path.read_text(encoding="utf-8")
    if not text.strip():
        print(f"[distill] {source_path.name}: empty, skipping", file=sys.stderr)
        return 0

    cards = call_claude(source_path, text)
    print(f"[distill] {source_path.name}: {len(cards)} card(s)", file=sys.stderr)

    rel_source = str(source_path.relative_to(REPO_ROOT)).replace("\\", "/")
    written = 0
    CONCEPTS_DIR.mkdir(parents=True, exist_ok=True)

    for card in cards:
        cid = safe_id(card.get("id", ""))
        out_path = CONCEPTS_DIR / f"{cid}.md"
        markdown = card_to_markdown(card, rel_source)

        if dry_run:
            print(f"\n--- {out_path.relative_to(REPO_ROOT)} ---")
            print(markdown)
        else:
            out_path.write_text(markdown, encoding="utf-8")
            written += 1
            print(f"  ✓ {out_path.relative_to(REPO_ROOT)}", file=sys.stderr)

    return written if not dry_run else len(cards)


def discover_all_docs() -> list[Path]:
    """Return every doc file we want to distill, in order."""
    targets: list[Path] = []
    targets += sorted((DOCS_DIR / "app-language").glob("*.md"))
    targets += sorted((DOCS_DIR / "modules" / "reference").glob("*.md"))
    return [p for p in targets if p.is_file() and not p.name.startswith("_")]


def main() -> None:
    global MODEL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "files",
        nargs="*",
        help="Doc files to distill (relative to repo root or absolute)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Distill every file under docs/app-language/ and docs/modules/reference/",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the cards instead of writing them to disk",
    )
    parser.add_argument(
        "--model",
        default=MODEL,
        help=f"Override the Claude model (default: {MODEL})",
    )
    args = parser.parse_args()
    MODEL = args.model

    if args.all:
        targets = discover_all_docs()
    else:
        if not args.files:
            parser.error("provide at least one file or pass --all")
        targets = [Path(f) if Path(f).is_absolute() else (REPO_ROOT / f) for f in args.files]

    print(f"[distill] {len(targets)} file(s) to process", file=sys.stderr)

    total_cards = 0
    for source in targets:
        if not source.is_file():
            print(f"[distill] skip (not a file): {source}", file=sys.stderr)
            continue
        try:
            total_cards += distill_file(source, dry_run=args.dry_run)
        except Exception as exc:
            print(f"[distill] FAILED on {source.name}: {exc}", file=sys.stderr)

    verb = "would write" if args.dry_run else "wrote"
    print(f"\n[distill] {verb} {total_cards} card(s) total", file=sys.stderr)


if __name__ == "__main__":
    main()
