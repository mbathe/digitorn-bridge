# /weaknesses - Recall stored weaknesses + suggest a focused drill

Steps :
1. `memory.recall("chess.weaknesses")`. If empty, suggest running `/analyze` first.
2. Pick the **single most actionable weakness** (the one most likely to improve rating fastest).
3. Suggest a concrete drill : a Lichess puzzle theme URL, a number of repetitions, and what success looks like.
4. End with a short pep-talk line.
