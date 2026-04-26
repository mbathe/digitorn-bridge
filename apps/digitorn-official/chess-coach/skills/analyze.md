# /analyze — Fetch + analyse the user's last N games

Usage: `/analyze [<username>] [--n 10] [--lang fr|en]`

Steps :
1. If `<username>` is given, use it AND store via `memory.remember("chess.username", username)`.
   Otherwise, recall it from memory. If no username known, ask.
2. Fetch via the Lichess endpoint specified in the system prompt with `max=<n>` (default 10).
3. Parse NDJSON line by line.
4. Produce the coaching report exactly as specified.
5. Persist the top 1-3 recurring patterns under `chess.weaknesses`.
