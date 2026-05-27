"""Complex document end-to-end test.

Asks scribe for an academic article that exercises:
- multi-section structure with abstract
- theorem environment + proof
- TikZ figure (state transition graph)
- comparative table
- cross-refs (\\ref, \\cite, \\label)
- biblatex bibliography with multiple entries
- French content
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


APP_ID = "digitorn-scribe"
TIMEOUT_S = 900


PROMPT = """\
Écris-moi un article LaTeX de qualité académique (3-5 pages) sur les
chaînes de Markov à temps discret et leur application à la simulation
Monte Carlo (algorithme de Metropolis-Hastings).

Le document doit contenir TOUT ce qui suit :

1. Préambule complet (article, biblatex authoryear, hyperref, cleveref,
   tikz, booktabs, amsmath/amsthm).
2. Titre, auteur "Paul Mbathe", date \\today, abstract de 80-120 mots.
3. Section 1 : Introduction (motivation, structure du papier).
4. Section 2 : Définitions formelles (chaîne de Markov, matrice de
   transition, mesure stationnaire, ergodicité). Au moins UN \\label{def:...}.
5. Section 3 : Théorème ergodique (énoncé + preuve esquissée).
   Utilise un environnement \\begin{theorem} ... \\end{theorem} et
   \\begin{proof} ... \\end{proof}. Réfère le théorème par \\cref ailleurs.
6. Section 4 : Application Metropolis-Hastings.
   - Inclus une FIGURE TikZ d'un graphe à 3 états avec arêtes étiquetées
     par les probabilités de transition (\\label{fig:graph} + \\caption).
   - Réfère la figure par \\cref dans le texte.
7. Section 5 : Comparaison numérique. Inclus un TABLEAU booktabs avec
   3 colonnes : Méthode, Convergence (itérations), Erreur L2.
   Au moins 4 méthodes comparées (\\label{tab:cmp} + \\caption).
8. Section 6 : Conclusion (3-4 phrases).
9. Bibliographie biblatex avec au moins 5 références
   réelles (Norris 1997, Robert & Casella 2004, Metropolis 1953,
   Hastings 1970, Meyn & Tweedie 2009). Cite chacune via \\cite{} dans le corps.

Contraintes de compile : main.pdf doit sortir avec 0 erreur tectonic.
Si tu touches au préambule, recompile pour vérifier que tout passe.
"""


def _token() -> str:
    return json.loads(
        (Path.home() / ".digitorn" / "credentials.json").read_text("utf-8")
    )["access_token"]


def _summary_disk(ws: Path) -> list[tuple[str, int]]:
    out = []
    for p in sorted(ws.iterdir()):
        if p.is_file():
            try:
                out.append((p.name, p.stat().st_size))
            except OSError:
                pass
    return out


def main() -> int:
    client = DevClient(token=_token())
    sid = f"complex-{uuid.uuid4().hex[:8]}"
    ws = Path.home() / ".digitorn" / "workspaces" / APP_ID / sid
    ws.mkdir(parents=True, exist_ok=True)
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace=str(ws),
    )
    print(f"daemon   = {client.daemon_url}")
    print(f"session  = {sid}")
    print(f"workspace= {ws}")
    print(f"prompt   = {len(PROMPT)} chars\n")

    t0 = time.monotonic()
    stream = client.send_live(session, PROMPT, total_timeout=TIMEOUT_S)
    elapsed = time.monotonic() - t0
    events = assertions.sort_by_seq(stream.events())
    stream.stop(timeout=2.0)

    tool_calls = []
    type_hist: dict[str, int] = {}
    for ev in events:
        t = str(ev.get("type", ""))
        type_hist[t] = type_hist.get(t, 0) + 1
        if t in ("tool_call", "tool_call_streaming"):
            p = ev.get("payload") or {}
            name = p.get("name") or p.get("tool_name") or ""
            if name:
                tool_calls.append(name)

    disk = _summary_disk(ws)
    main_tex = ws / "main.tex"
    main_pdf = ws / "main.pdf"
    main_log = ws / "main.log"
    bib = next((p for p in ws.iterdir() if p.suffix == ".bib"), None)

    print(f"elapsed     = {elapsed:.1f}s")
    print(f"events      = {len(events)}")
    print(f"tool_calls  = {len(tool_calls)}")
    print(f"type_hist   = {dict(sorted(type_hist.items(), key=lambda x: -x[1])[:8])}")

    print(f"\ndisk after the turn:")
    for name, size in disk:
        print(f"  {size:>10,d} B  {name}")

    print(f"\n----- artefacts -----")
    print(f"  main.tex : {'exists' if main_tex.exists() else 'MISSING'} "
          f"({main_tex.stat().st_size if main_tex.exists() else 0:,} bytes)")
    print(f"  main.pdf : {'exists' if main_pdf.exists() else 'MISSING'} "
          f"({main_pdf.stat().st_size if main_pdf.exists() else 0:,} bytes)")
    if bib:
        print(f"  {bib.name:9s}: {bib.stat().st_size:,} bytes")

    # Heuristic structure checks on main.tex
    if main_tex.exists():
        text = main_tex.read_text("utf-8", errors="replace")
        markers = {
            "\\section": text.count("\\section{"),
            "\\theorem-env": "begin{theorem}" in text,
            "\\proof-env":   "begin{proof}" in text,
            "tikz figure":  "begin{tikzpicture}" in text,
            "booktabs table": "\\toprule" in text and "\\bottomrule" in text,
            "\\cite":      text.count("\\cite{"),
            "\\cref":      text.count("\\cref{"),
            "\\label":     text.count("\\label{"),
            "biblatex":    "\\usepackage[" in text and "biblatex" in text,
        }
        print(f"\n----- structure markers in main.tex -----")
        for k, v in markers.items():
            print(f"  {k:18s} {v}")

    print(f"\n=== TO INSPECT ===")
    print(f"  cd \"{ws}\"")
    if main_pdf.exists():
        print(f"  start main.pdf")
    print(f"  notepad main.tex")
    return 0 if main_pdf.exists() and main_pdf.stat().st_size > 5000 else 1


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main())
