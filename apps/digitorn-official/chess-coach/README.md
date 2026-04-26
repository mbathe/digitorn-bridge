# Chess Coach

> Pulls your last games from Lichess, identifies recurring tactical mistakes, and drills you on the right patterns. Conversational, multilingual (FR/EN).

## What it does

You give it your Lichess username. It fetches your last 10 rated games via the public Lichess API, parses the PGN with engine evaluations, identifies your recurring weaknesses (opening drift, middlegame blunders, endgame leaks), and produces a focused coaching report with concrete drill suggestions.

It remembers your username + your patterns across sessions, so over time it tracks whether you're actually improving on the things it flagged.

## Quick start

```
/analyze <your_lichess_username>          # 1st time
/analyze                                  # subsequent (uses stored username)
/weaknesses                               # focused drill on your worst pattern
/opening                                  # opening repertoire review
```

Or just talk:

> "Analyse mes 10 dernières parties (username: xxxx)"
> "Quelle est ma faiblesse récurrente ?"
> "Give me a tactical drill for tonight"

## What it can NOT do

- It does not play chess against you (use Lichess for that).
- It does not analyse arbitrary PGN you paste (yet) — Lichess username only for now.
- It does not connect to Chess.com — Lichess only.

## Architecture

- **One agent**, conversational mode.
- **Brain**: DeepSeek `deepseek-chat` by default. Anthropic Haiku and OpenAI gpt-4o-mini are also recommended.
- **Modules**: `http` (Lichess API), `memory` (username + weakness patterns), `context_builder`.
- **Permissions**: `risk_level: low`, network only (Lichess), no filesystem, no shell.

## Provenance

Built and maintained by the Digitorn team. Source: `apps/digitorn-official/chess-coach/` in the [digitorn-bridge repo](https://github.com/digitorn/digitorn-bridge).
