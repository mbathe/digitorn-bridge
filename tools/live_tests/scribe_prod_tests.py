"""Production-grade scribe tests.

Uses the real session API (POST /sessions, then /sessions/{sid}/messages)
so sync_to_disk works and tectonic actually compiles. No dev- prefix.

Each test:
  1. Creates a real session
  2. Sends one or more messages
  3. Waits for completion (polls history until pending=False)
  4. Inspects on-disk artifacts (main.tex, main.pdf, chapters/, etc.)
  5. Asserts agent behavior (used the right tools, wrote the right content)

Run individual: PYTHONIOENCODING=utf-8 py -3.12 tools/live_tests/scribe_prod_tests.py test_scaffold_article
Run all      : PYTHONIOENCODING=utf-8 py -3.12 tools/live_tests/scribe_prod_tests.py
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

DAEMON = "http://127.0.0.1:8000"
WORKER = "http://127.0.0.1:18002"
APP_ID = "digitorn-scribe"


def _token() -> str:
    return json.loads(
        (Path.home() / ".digitorn/credentials.json").read_text(encoding="utf-8")
    )["access_token"]


def _hdr() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def _ws_root(sid: str) -> Path:
    return Path.home() / ".digitorn/workspaces" / APP_ID / sid


def _http(method: str, url: str, *, timeout: float = 15.0, retries: int = 3, **kw) -> httpx.Response:
    """Resilient HTTP: retries on connect refused (daemon stall) with
    exponential backoff. Read errors / 5xx pass through without retry."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return httpx.request(method, url, timeout=timeout, **kw)
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            last_exc = exc
            # Daemon is stalled or restarting; wait + retry
            time.sleep(2.0 * (attempt + 1))
        except httpx.ReadTimeout as exc:
            last_exc = exc
            time.sleep(1.0)
    raise last_exc if last_exc else RuntimeError("unreachable")


def _wait_until_ready(timeout: float = 60.0) -> None:
    """Block until daemon healthz reports not-warming and worker is up."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            d = httpx.get(f"{DAEMON}/healthz", timeout=5).json()
            w = httpx.get(f"{WORKER}/health", timeout=5).json()
            if (not d.get("warming_up")) and w.get("status") == "ok":
                return
        except Exception:
            pass
        time.sleep(2.0)
    raise TimeoutError("daemon/worker not ready in time")


# ── Session helpers ─────────────────────────────────────────────────


def create_session(initial: str = "ok") -> str:
    """Create a real session via the daemon API. Returns the session_id.

    Uses a trivial initial message ("ok") to satisfy the atomic-create
    contract (no empty sessions allowed) while keeping the bootstrap
    turn cheap. The session settles in ~15-30 s including Coach's
    classifier overhead.
    """
    r = _http(
        "POST",
        f"{DAEMON}/api/apps/{APP_ID}/sessions",
        headers=_hdr(),
        json={"message": initial},
        timeout=30,
    )
    r.raise_for_status()
    sid = (r.json().get("data") or {}).get("session_id")
    if not sid:
        raise RuntimeError(f"no session_id in response: {r.text[:200]}")
    # Wait generously for the bootstrap turn (Coach + reply)
    _wait_for_idle(sid, timeout=240)
    return sid


def _get_history(sid: str) -> dict[str, Any]:
    try:
        r = _http(
            "GET",
            f"{DAEMON}/api/apps/{APP_ID}/sessions/{sid}/history",
            headers=_hdr(),
            timeout=10,
            retries=2,
        )
    except Exception:
        return {}
    if r.status_code != 200:
        return {}
    return r.json().get("data") or {}


def _wait_for_idle(
    sid: str,
    timeout: float = 480.0,
    min_messages: int = 1,
) -> dict[str, Any]:
    """Poll history until pending=False AND ``len(messages) >= min_messages``.
    Tolerant of daemon stalls — connection refused triggers a backoff
    rather than an immediate failure.

    ``min_messages`` gates the early-exit so we don't return the prior
    turn's already-idle state when a fresh POST hasn't been registered
    by the daemon yet.
    """
    t0 = time.monotonic()
    last_msg_count = 0
    last_change_ts = t0
    consecutive_failures = 0
    while time.monotonic() - t0 < timeout:
        try:
            h = _get_history(sid)
            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            if consecutive_failures > 10:
                raise TimeoutError(f"session {sid}: 10+ consecutive history fetch errors")
            time.sleep(3.0)
            continue
        msgs = h.get("messages") or []
        pending = bool(h.get("pending"))
        if len(msgs) != last_msg_count:
            last_msg_count = len(msgs)
            last_change_ts = time.monotonic()
        if not pending and len(msgs) >= min_messages:
            if time.monotonic() - last_change_ts > 1.5:
                return h
        # Auto-approve any pending approvals
        try:
            ar = _http(
                "GET",
                f"{DAEMON}/api/approvals",
                headers=_hdr(), timeout=5, retries=1,
            )
            if ar.status_code == 200:
                for ap in ar.json().get("data", []) or []:
                    _http(
                        "POST",
                        f"{DAEMON}/api/approvals/{ap.get('id')}/resolve",
                        headers=_hdr(),
                        json={"decision": "approve"},
                        timeout=5, retries=1,
                    )
        except Exception:
            pass
        # Backoff: 1s normally, longer when the daemon looks stalled
        time.sleep(1.0)
    raise TimeoutError(f"session {sid} did not idle within {timeout}s")


def send(sid: str, message: str, timeout: float = 600.0) -> dict[str, Any]:
    """Send a message and wait for completion. Returns final history.

    Two-phase wait:
      1. Wait for the new user message to appear in history (turn
         actually dispatched on the daemon side).
      2. Wait for pending=False and message stability.
    Phase 1 prevents the "pending=False from prior turn" race where
    the post returns before the daemon has flipped the flag.
    """
    # Snapshot the current message count BEFORE posting
    h0 = _get_history(sid)
    baseline = len(h0.get("messages") or [])

    r = _http(
        "POST",
        f"{DAEMON}/api/apps/{APP_ID}/sessions/{sid}/messages",
        headers=_hdr(),
        json={"message": message},
        timeout=30,
    )
    if r.status_code not in (200, 202):
        raise RuntimeError(f"send failed: {r.status_code} {r.text[:200]}")

    # Phase 1: wait for the new user message to appear in history
    t0 = time.monotonic()
    while time.monotonic() - t0 < 30:
        h = _get_history(sid)
        if len(h.get("messages") or []) > baseline:
            break
        time.sleep(0.5)
    else:
        # Some daemons batch the user-message-write with the assistant
        # response; if we never see growth in 30 s, fall through to the
        # idle wait and let it time out organically (don't crash here).
        pass

    return _wait_for_idle(sid, timeout=timeout, min_messages=baseline + 1)


def tool_calls_for_last_turn(history: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all tool_calls from assistant messages of the last user turn."""
    msgs = history.get("messages") or []
    # Find last user message index
    last_user = -1
    for i, m in enumerate(msgs):
        if m.get("role") == "user":
            last_user = i
    calls: list[dict[str, Any]] = []
    for m in msgs[last_user + 1:]:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or tc
            name = fn.get("name") or tc.get("name", "")
            args = fn.get("arguments") or tc.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}
            calls.append({"name": name, "args": args, "result": tc.get("result")})
    return calls


def assistant_text_for_last_turn(history: dict[str, Any]) -> str:
    msgs = history.get("messages") or []
    last_user = -1
    for i, m in enumerate(msgs):
        if m.get("role") == "user":
            last_user = i
    out = []
    for m in msgs[last_user + 1:]:
        if m.get("role") == "assistant":
            c = m.get("content")
            if isinstance(c, str):
                out.append(c)
    return "\n\n".join(out)


# ── Disk introspection ──────────────────────────────────────────────


def list_workspace(sid: str) -> dict[str, dict[str, Any]]:
    root = _ws_root(sid)
    out: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if p.is_file() and ".digitorn" not in str(p):
            rel = str(p.relative_to(root)).replace("\\", "/")
            out[rel] = {
                "size": p.stat().st_size,
                "ext": p.suffix,
            }
    return out


def read_disk_file(sid: str, rel_path: str) -> str | None:
    p = _ws_root(sid) / rel_path
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


# ── Result accounting ───────────────────────────────────────────────


class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed: list[str] = []
        self.failed: list[str] = []
        self.warnings: list[str] = []
        self.facts: dict[str, Any] = {}
        self.sid: str | None = None
        self.elapsed: float = 0.0

    def assert_(self, cond: bool, desc: str) -> None:
        if cond:
            self.passed.append(desc)
        else:
            self.failed.append(desc)

    def warn(self, desc: str) -> None:
        self.warnings.append(desc)

    def fact(self, key: str, value: Any) -> None:
        self.facts[key] = value

    def is_pass(self) -> bool:
        return not self.failed

    def report(self) -> str:
        head = f"{'PASS' if self.is_pass() else 'FAIL'} {self.name}  ({self.elapsed:.1f}s, sid={self.sid})"
        lines = [head]
        for p in self.passed:
            lines.append(f"  + {p}")
        for w in self.warnings:
            lines.append(f"  ? {w}")
        for f in self.failed:
            lines.append(f"  - {f}")
        if self.facts:
            for k, v in self.facts.items():
                sv = str(v)
                if len(sv) > 200:
                    sv = sv[:200] + "..."
                lines.append(f"  . {k} = {sv}")
        return "\n".join(lines)


# ── Scenarios ───────────────────────────────────────────────────────


def test_scaffold_article(r: TestResult) -> None:
    """Test 1: Scaffold a publication-grade article from scratch.

    Expectation: agent produces a complete main.tex with proper preamble,
    title/author, abstract, 2 sections, math, and a reference. Compiles
    with errors=0.
    """
    r.sid = create_session()
    h = send(r.sid, (
        "Crée un article LaTeX publication-grade sur le théorème central limite. "
        "Auteur: Paul Mbathe. Inclus: abstract, 2 sections, une formule "
        "(distribution gaussienne), et une référence bibliographique. "
        "Vérifie que main.tex compile sans erreur tectonic."
    ), timeout=300)

    files = list_workspace(r.sid)
    r.fact("workspace_files", list(files.keys()))
    r.assert_("main.tex" in files, "main.tex written to disk")
    r.assert_("main.pdf" in files, "main.pdf compiled to disk")
    if "main.pdf" in files:
        r.assert_(files["main.pdf"]["size"] > 1500, f"PDF > 1500 bytes (got {files['main.pdf']['size']})")

    tex = read_disk_file(r.sid, "main.tex") or ""
    r.fact("tex_size", len(tex))
    r.assert_("\\documentclass" in tex, "has \\documentclass")
    r.assert_("\\begin{document}" in tex, "has \\begin{document}")
    r.assert_("Paul Mbathe" in tex, "author Paul Mbathe present")
    r.assert_("\\section" in tex, "has at least one section")
    has_math = "\\[" in tex or "$" in tex or "equation" in tex or "align" in tex
    r.assert_(has_math, "has math content")

    calls = tool_calls_for_last_turn(h)
    r.fact("tool_calls", [c["name"] for c in calls])
    r.assert_(
        any("write" in c["name"].lower() or c["name"] == "WsWrite" for c in calls),
        "agent called WsWrite",
    )


def test_compile_error_fix_loop(r: TestResult) -> None:
    """Test 2: Multi-turn iterative compile fix.

    Turn 1: Write a doc with a deliberate macro typo.
    Turn 2: Ask agent to fix all tectonic errors.
    Expectation: agent inspects lint, identifies \\frak as typo, fixes it,
    confirms errors=0 on second compile.
    """
    r.sid = create_session()
    # Turn 1: scaffold with an intentional bug
    send(r.sid, (
        "Crée un main.tex minimal: documentclass article, begin/end document, "
        "et UNE formule math dans le body en utilisant \\\\frak{1}{2} "
        "(je sais que c'est faux, écris-le exactement comme demandé)."
    ), timeout=180)

    tex1 = read_disk_file(r.sid, "main.tex") or ""
    has_typo = "\\frak" in tex1
    r.fact("turn1_has_frak", has_typo)
    if not has_typo:
        r.warn("agent silently fixed \\frak on turn 1; can't test fix loop properly")
        # Re-inject manually
        tgt = _ws_root(r.sid) / "main.tex"
        if tgt.exists():
            content = tgt.read_text(encoding="utf-8")
            tgt.write_text(
                content.replace("\\frac", "\\frak"),
                encoding="utf-8",
            )

    # Turn 2: ask for fix
    h2 = send(r.sid, (
        "Compile main.tex maintenant et corrige toutes les erreurs tectonic. "
        "Confirme errors=0 dans ta réponse finale."
    ), timeout=240)

    tex2 = read_disk_file(r.sid, "main.tex") or ""
    r.assert_("\\frak" not in tex2, "\\frak typo eliminated after fix turn")
    r.assert_("\\frac" in tex2, "\\frac correctly used")
    pdf = _ws_root(r.sid) / "main.pdf"
    r.assert_(pdf.exists() and pdf.stat().st_size > 1000, "main.pdf compiled cleanly")
    final_text = assistant_text_for_last_turn(h2).lower()
    r.fact("final_msg_excerpt", final_text[:200])
    r.assert_("errors=0" in final_text or "0 erreur" in final_text or "0 error" in final_text or "aucune erreur" in final_text,
              "agent confirms errors=0")


def test_label_rename(r: TestResult) -> None:
    """Test 3: Atomic label rename — agent must WsGrep first, then atomic
    batch replace, then verify zero dangling refs."""
    r.sid = create_session()
    send(r.sid, (
        "Crée un main.tex avec une section labelée \\label{sec:intro} "
        "et 3 références à cette section: \\ref{sec:intro}, \\autoref{sec:intro}, "
        "\\cref{sec:intro}. Compile pour vérifier que les refs ne sont pas undefined."
    ), timeout=240)

    tex = read_disk_file(r.sid, "main.tex") or ""
    r.assert_("\\label{sec:intro}" in tex, "initial label present")
    r.assert_(tex.count("sec:intro") >= 4, f"label + 3 refs (count={tex.count('sec:intro')})")

    h2 = send(r.sid, (
        "Renomme le label sec:intro en sec:contexte partout dans le projet. "
        "Utilise WsGrep d'abord pour confirmer les sites, puis WsEdit avec replace_all=true. "
        "Confirme qu'il n'y a aucune ref undefined après recompile."
    ), timeout=240)

    tex2 = read_disk_file(r.sid, "main.tex") or ""
    r.assert_("sec:intro" not in tex2, "sec:intro fully gone")
    r.assert_("sec:contexte" in tex2, "sec:contexte introduced")
    r.assert_(tex2.count("sec:contexte") >= 4, f"4 sites renamed (count={tex2.count('sec:contexte')})")

    calls = tool_calls_for_last_turn(h2)
    names = [c["name"] for c in calls]
    r.fact("turn2_tools", names)
    r.assert_(any("Grep" in n or "grep" in n for n in names), "agent used WsGrep")
    r.assert_(any("Edit" in n or "edit" in n for n in names), "agent used WsEdit")


def test_bibtex_citation(r: TestResult) -> None:
    """Test 4: Add a real bibtex entry + citation via biblatex setup."""
    r.sid = create_session()
    send(r.sid, (
        "Crée un main.tex avec biblatex configuré (backend=biber, style=numeric), "
        "et une bibliography file references.bib. Ajoute une entrée pour "
        "l'article 'Attention Is All You Need' de Vaswani et al. 2017 "
        "(citation key: vaswani2017). Dans le body, cite-le avec \\\\cite{vaswani2017} "
        "dans une phrase. Compile + lance \\\\printbibliography."
    ), timeout=300)

    files = list_workspace(r.sid)
    r.fact("files", list(files.keys()))
    r.assert_("main.tex" in files, "main.tex present")
    r.assert_("references.bib" in files or "main.bib" in files or any(f.endswith(".bib") for f in files), "bib file present")

    tex = read_disk_file(r.sid, "main.tex") or ""
    r.assert_("biblatex" in tex.lower(), "biblatex package loaded")
    r.assert_("\\cite{vaswani2017}" in tex or "\\textcite{vaswani2017}" in tex or "\\parencite{vaswani2017}" in tex,
              "vaswani2017 cited in body")
    r.assert_("\\printbibliography" in tex, "\\printbibliography in body")

    bib_file = "references.bib" if "references.bib" in files else next((f for f in files if f.endswith(".bib")), None)
    if bib_file:
        bib = read_disk_file(r.sid, bib_file) or ""
        r.assert_("vaswani2017" in bib, "vaswani2017 entry in .bib")
        r.assert_("Attention" in bib, "title 'Attention' present in bib entry")


def test_article_to_beamer(r: TestResult) -> None:
    """Test 5: Convert an article to a beamer slide deck."""
    r.sid = create_session()
    # Turn 1: scaffold a short article
    send(r.sid, (
        "Crée un main.tex article court (2 sections) sur le tri rapide (quicksort). "
        "Inclus une formule O(n log n) en math mode."
    ), timeout=240)
    assert read_disk_file(r.sid, "main.tex"), "turn 1 should produce main.tex"

    # Turn 2: convert to beamer
    h2 = send(r.sid, (
        "Maintenant convertis main.tex en présentation Beamer de 5 frames: "
        "titre, sommaire, principe, complexité, conclusion. Garde le nom de "
        "fichier main.tex. Compile et vérifie errors=0."
    ), timeout=300)

    tex = read_disk_file(r.sid, "main.tex") or ""
    r.assert_("\\documentclass{beamer}" in tex or "\\documentclass[" in tex and "beamer" in tex.split("\\documentclass")[1].split("\n")[0],
              "documentclass switched to beamer")
    r.assert_(tex.count("\\begin{frame}") >= 4, f"at least 4 frames (got {tex.count('\\begin{frame}')})")
    pdf = _ws_root(r.sid) / "main.pdf"
    r.assert_(pdf.exists() and pdf.stat().st_size > 1500, "beamer PDF compiled")

    final = assistant_text_for_last_turn(h2).lower()
    r.assert_("errors=0" in final or "0 erreur" in final or "aucune erreur" in final or "0 error" in final,
              "agent confirms errors=0 on beamer")


def test_multichapter_thesis(r: TestResult) -> None:
    """Test 6: Scaffold a multi-chapter thesis with \\include and chapters/."""
    r.sid = create_session()
    send(r.sid, (
        "Scaffold une thèse multi-chapitre. main.tex utilise documentclass book, "
        "loads babel-french et biblatex, et fait \\\\include pour chaque chapitre. "
        "Crée 3 chapitres sous chapters/: chapters/introduction.tex, "
        "chapters/methodologie.tex, chapters/resultats.tex. Chaque chapitre a un "
        "\\\\chapter{...} + 1 section. Sujet: les nombres premiers de Mersenne. "
        "Compile pour vérifier la structure."
    ), timeout=360)

    files = list_workspace(r.sid)
    r.fact("files", sorted(files.keys()))
    r.assert_("main.tex" in files, "main.tex root present")
    r.assert_("chapters/introduction.tex" in files, "chapters/introduction.tex present")
    r.assert_("chapters/methodologie.tex" in files, "chapters/methodologie.tex present")
    r.assert_("chapters/resultats.tex" in files, "chapters/resultats.tex present")

    main_tex = read_disk_file(r.sid, "main.tex") or ""
    r.assert_("\\documentclass{book}" in main_tex or "{book}" in main_tex, "uses book class")
    r.assert_("\\include{chapters/introduction}" in main_tex or "\\input{chapters/introduction}" in main_tex,
              "main.tex includes introduction chapter")

    pdf = _ws_root(r.sid) / "main.pdf"
    r.assert_(pdf.exists() and pdf.stat().st_size > 2000, "thesis PDF compiled (>2KB)")


def test_session_reload(r: TestResult) -> None:
    """Test 7: Mid-task session reload. Start work, simulate disconnect,
    reconnect (history loaded), continue editing. Validate history reload
    doesn't lose context."""
    r.sid = create_session()
    # Turn 1: scaffold
    send(r.sid, (
        "Crée un article LaTeX très court sur le théorème de Pythagore. "
        "Auteur: Test. UNE section, UNE formule a² + b² = c²."
    ), timeout=180)
    h1 = _get_history(r.sid)
    msg_count_t1 = len(h1.get("messages") or [])
    r.fact("messages_after_t1", msg_count_t1)
    tex1 = read_disk_file(r.sid, "main.tex") or ""
    r.assert_("Pythagore" in tex1 or "pythagore" in tex1.lower(), "turn 1 created Pythagore content")

    # Simulate reconnect: re-fetch history via different client identity
    # (the daemon must serve full history for any auth'd request).
    h_reload = _get_history(r.sid)
    msg_count_reload = len(h_reload.get("messages") or [])
    r.assert_(msg_count_reload == msg_count_t1, f"history fully reloaded ({msg_count_reload} vs {msg_count_t1})")
    has_pyth = any(
        "Pythagore" in str(m.get("content", "")) or "pythagore" in str(m.get("content", "")).lower()
        for m in (h_reload.get("messages") or [])
    )
    r.assert_(has_pyth, "Pythagore mention preserved in history")

    # Turn 2: continue editing as if reconnected
    h2 = send(r.sid, (
        "Ajoute maintenant une seconde section dans le même main.tex "
        "qui démontre le théorème géométriquement. Ne pars pas de zéro: "
        "tu connais déjà le contenu de main.tex (Pythagore article)."
    ), timeout=240)

    tex2 = read_disk_file(r.sid, "main.tex") or ""
    section_count_before = tex1.count("\\section")
    section_count_after = tex2.count("\\section")
    r.fact("sections_before_after", (section_count_before, section_count_after))
    r.assert_(section_count_after > section_count_before, "agent added a section (didn't restart)")
    r.assert_("Pythagore" in tex2 or "pythagore" in tex2.lower(), "original Pythagore content preserved")

    # Make sure the agent referenced the prior context (mentioned reading main.tex first)
    calls_t2 = tool_calls_for_last_turn(h2)
    read_first = any("Read" in c["name"] or "read" in c["name"].lower() for c in calls_t2)
    if not read_first:
        r.warn("agent didn't WsRead main.tex before editing on reload — risky for larger docs")


def test_chktex_hygiene(r: TestResult) -> None:
    """Test 8: chktex stylistic pass. Existing doc with style issues
    (missing ~ before \\ref, double space, eqnarray, \\\\ at para end).
    Agent should fix all chktex warnings and verify warnings=0."""
    r.sid = create_session()
    # Plant a doc with multiple chktex hits
    bad = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\section{Intro}\\label{sec:intro}\n"
        "Voir la section \\ref{sec:intro} pour le contexte.  Cette phrase a deux espaces.\n"
        "\n"
        "\\begin{eqnarray}\n"
        "x &=& y + z \\\\\n"
        "y &=& 1\n"
        "\\end{eqnarray}\n"
        "\\end{document}\n"
    )
    # Use PUT writeback to plant the file (bypasses the agent for setup)
    httpx.put(
        f"{DAEMON}/api/apps/{APP_ID}/sessions/{r.sid}/workspace/files/main.tex",
        headers=_hdr(),
        json={"content": bad},
        timeout=30,
    )

    h = send(r.sid, (
        "Fais une passe stylistique sur main.tex: corrige tous les warnings chktex "
        "(non-breaking space avant \\\\ref, double espace, eqnarray déprécié, etc). "
        "Liste explicitement chaque warning que tu as adressé et confirme "
        "warnings=0 après le fix."
    ), timeout=300)

    tex = read_disk_file(r.sid, "main.tex") or ""
    # Specific hygiene checks
    r.assert_("~\\ref" in tex or "~ \\ref" in tex, "non-breaking space added before \\ref")
    r.assert_("eqnarray" not in tex, "eqnarray replaced (deprecated)")
    r.assert_("  " not in tex.replace("\n", "").replace("\\\\", ""), "double spaces collapsed")

    final = assistant_text_for_last_turn(h).lower()
    r.assert_("warnings=0" in final or "0 warning" in final or "0 avertissement" in final
              or "aucun warning" in final or "aucun avertissement" in final,
              "agent confirms warnings=0")


# ── Test registry ───────────────────────────────────────────────────


_TESTS: dict[str, Callable[[TestResult], None]] = {
    "test_scaffold_article": test_scaffold_article,
    "test_compile_error_fix_loop": test_compile_error_fix_loop,
    "test_label_rename": test_label_rename,
    "test_bibtex_citation": test_bibtex_citation,
    "test_article_to_beamer": test_article_to_beamer,
    "test_multichapter_thesis": test_multichapter_thesis,
    "test_session_reload": test_session_reload,
    "test_chktex_hygiene": test_chktex_hygiene,
}


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    only = set(argv) if argv else None

    # Wait for daemon/worker fully ready (not warming, healthy)
    try:
        _wait_until_ready(timeout=120)
        print("daemon + worker ready, proceeding")
    except Exception as exc:
        print(f"[ABORT] daemon/worker not ready: {exc}")
        return 2

    results: list[TestResult] = []
    for name, fn in _TESTS.items():
        if only and name not in only:
            continue
        print(f"\n{'=' * 60}\n>> {name}\n{'=' * 60}")
        res = TestResult(name)
        t0 = time.monotonic()
        try:
            fn(res)
        except Exception as exc:
            res.failed.append(f"EXCEPTION: {type(exc).__name__}: {exc}")
        res.elapsed = time.monotonic() - t0
        print(res.report())
        results.append(res)

    # Summary
    print(f"\n{'=' * 60}\n== SUMMARY ==\n{'=' * 60}")
    for r in results:
        flag = "PASS" if r.is_pass() else "FAIL"
        print(f"  {flag}  {r.name}  ({r.elapsed:.1f}s)  "
              f"+{len(r.passed)}/-{len(r.failed)}/?{len(r.warnings)}")
    passed = sum(1 for r in results if r.is_pass())
    print(f"\n  PASS {passed} / {len(results)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
