# /opening - Review opening choices in recent games

Steps :
1. Recall `chess.username`, fetch last 10 games (same Lichess endpoint).
2. Parse each game's `opening.name` and `opening.eco`.
3. Group by colour (white/black) and report the 3 most-played openings on each side.
4. For each, comment briefly: theory adherence, win rate, time spent in the opening.
5. Suggest **one concrete improvement** : drop a losing opening, deepen a strong one, or expand the repertoire by one new line.
