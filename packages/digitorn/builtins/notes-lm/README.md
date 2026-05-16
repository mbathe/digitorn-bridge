# Notes LM (v2 · read-direct)

The NotebookLM-equivalent on Digitorn. Upload sources, get grounded answers with **verbatim citations pointing to exact line ranges**, briefings, mind maps, timelines, study guides, and a podcast-style audio overview.

## v2 rewrite : read-direct

Dropped RAG. Sources are markdown files the agent reads on demand via `WsRead`. Citations point to `path:Lstart-Lend` with verbatim quotes, not opaque chunk IDs. Lighter, faster, more honest.

## What it does

- **Source-grounded chat** : every answer cites the exact line range with verbatim quote
- **Briefing** : auto-generated executive summary
- **Mind map** : mermaid graph of themes + relationships
- **Timeline** : chronological extraction of dated events
- **Study guide** : key concepts + FAQ + 10-question quiz
- **Audio overview** : 2-host podcast script + OpenAI TTS synthesis (~5-7 min)

## Modules

- `web` — fetch + extract pages when you paste URLs
- `http` — POST to OpenAI TTS for the audio feature
- `workspace` — store sources + generated artefacts (Lovable-style file panel)
- `memory` — preferences across sessions

## Setup

1. Install: `hub://mbathe/notes-lm`
2. For audio overview, add an OpenAI API key in your app secrets:
   - Settings → Secrets → `OPENAI_API_KEY = sk-...`
   - Without it, the other 5 features work fine.

## Sources layout

| Path | Comes from |
|---|---|
| `sources/*.md` | URLs / pasted text you sent the agent |
| `attachments/*` | Files you uploaded via the chat composer |
| `briefing.md`, `mindmap.md`, `timeline.md`, `study_guide.md`, `audio_overview.md` | Generated artefacts |

## Quick start

1. Paste a URL or upload a PDF → agent saves it under `sources/` or `attachments/`
2. Ask any question → agent reads the relevant file and cites `path:Lstart-Lend` with verbatim quote
3. Try the quick prompts on the empty state for one-click briefings
