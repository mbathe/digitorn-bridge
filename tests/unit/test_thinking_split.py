"""Unit test for the thinking/content split regression.

Scenario from BUG report:
  - Provider emits N chunks with `chunk.thinking="…"` (native thinking)
  - Then chunks with `chunk.delta="…"` (actual response content)
Expected:
  - on_thinking called ONCE with the thinking text ONLY
  - on_token (or content_parts) contains the response text ONLY
  - NO thinking snapshot contains any response text
Regression:
  - Daemon was classifying post-thinking content deltas as "still thinking"
    because _in_think was shared between native-mode and tag-mode paths.
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from digitorn.core.runtime.callbacks import AgentTurnCallbacks  # noqa: E402
from digitorn.core.runtime.streaming import _StreamState  # noqa: E402


class _Collector:
    def __init__(self):
        self.thinking_snapshots: list[str] = []
        self.thinking_deltas: list[str] = []
        self.tokens: list[str] = []
        self.started = 0

    async def on_thinking(self, text: str) -> None:
        self.thinking_snapshots.append(text)

    def on_thinking_started(self) -> None:
        self.started += 1

    def on_thinking_delta(self, text: str) -> None:
        self.thinking_deltas.append(text)

    async def on_token(self, text: str) -> None:
        self.tokens.append(text)


async def run() -> int:
    c = _Collector()
    cb = AgentTurnCallbacks(
        on_token=c.on_token,
        on_thinking=c.on_thinking,
        on_thinking_started=c.on_thinking_started,
        on_thinking_delta=c.on_thinking_delta,
    )
    state = _StreamState(cb, ctx=None)

    # 5 native thinking chunks of growing text
    thinking_parts = [
        "Commençons par ",
        "me présenter et ",
        "demander ce que le ",
        "user souhaite faire. ",
        "Proposons d'abord quelques options.",
    ]
    for tp in thinking_parts:
        chunk = SimpleNamespace(delta="", finish_reason=None,
                                 tool_call=None, tool_calls=None,
                                 thinking=tp, usage=None)
        await state.process_chunk(chunk)

    # Then content deltas - the actual response
    response_parts = [
        "Bonjour ! Je suis votre ",
        "assistant Builder pour créer ",
        "des applications Digitorn.",
    ]
    for rp in response_parts:
        chunk = SimpleNamespace(delta=rp, finish_reason=None,
                                 tool_call=None, tool_calls=None,
                                 thinking=None, usage=None)
        await state.process_chunk(chunk)

    # finish
    chunk = SimpleNamespace(delta="", finish_reason="stop",
                             tool_call=None, tool_calls=None,
                             thinking=None, usage=None)
    await state.process_chunk(chunk)
    await state.flush()

    # Assertions
    failures: list[str] = []

    # Exactly one thinking snapshot containing ONLY the thinking text
    if len(c.thinking_snapshots) != 1:
        failures.append(
            f"expected 1 thinking snapshot, got {len(c.thinking_snapshots)}: "
            f"{c.thinking_snapshots}"
        )
    else:
        snap = c.thinking_snapshots[0]
        if "Bonjour" in snap or "Builder" in snap:
            failures.append(
                f"thinking snapshot LEAKED response content: {snap!r}"
            )
        if "Commençons" not in snap or "présenter" not in snap:
            failures.append(
                f"thinking snapshot missing thinking content: {snap!r}"
            )

    # Content parts MUST contain the response and NOT the thinking
    content_all = "".join(state.content_parts)
    if "Bonjour" not in content_all:
        failures.append(f"content missing response: {content_all!r}")
    if "Commençons" in content_all:
        failures.append(f"content LEAKED thinking: {content_all!r}")

    # on_token only fires for visible content (not thinking)
    visible = "".join(c.tokens)
    if "Commençons" in visible:
        failures.append(f"on_token LEAKED thinking: {visible!r}")

    if failures:
        print("FAIL - native-mode split:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - native-mode thinking/content cleanly separated")
    print(f"  thinking snapshot: {c.thinking_snapshots[0][:80]!r}...")
    print(f"  content parts:     {content_all[:80]!r}...")

    # ── Second scenario: text-tag thinking (<think>…</think>) ──
    c2 = _Collector()
    cb2 = AgentTurnCallbacks(
        on_token=c2.on_token,
        on_thinking=c2.on_thinking,
        on_thinking_started=c2.on_thinking_started,
        on_thinking_delta=c2.on_thinking_delta,
    )
    state2 = _StreamState(cb2, ctx=None)

    text_chunks = [
        "<think>reflexion en cours</think>",
        "Reponse finale en clair.",
    ]
    for t in text_chunks:
        chunk = SimpleNamespace(delta=t, finish_reason=None,
                                 tool_call=None, tool_calls=None,
                                 thinking=None, usage=None)
        await state2.process_chunk(chunk)
    chunk = SimpleNamespace(delta="", finish_reason="stop",
                             tool_call=None, tool_calls=None,
                             thinking=None, usage=None)
    await state2.process_chunk(chunk)
    await state2.flush()

    failures2: list[str] = []
    if len(c2.thinking_snapshots) != 1:
        failures2.append(
            f"expected 1 text-tag thinking, got {len(c2.thinking_snapshots)}"
        )
    else:
        snap = c2.thinking_snapshots[0]
        if "Reponse" in snap:
            failures2.append(f"text-tag thinking LEAKED response: {snap!r}")
    v = "".join(c2.tokens)
    if "Reponse" not in v:
        failures2.append(f"text-tag response missing from tokens: {v!r}")
    if "reflexion en cours" in v:
        failures2.append(f"text-tag thinking LEAKED into tokens: {v!r}")
    if failures2:
        print("FAIL - text-tag split:")
        for f in failures2:
            print(f"  - {f}")
        return 1
    print("PASS - text-tag thinking/content cleanly separated")

    # ── Scenario 3: mixed - native thinking chunks followed by
    # content that happens to contain the SAME pattern as the production
    # bug (Builder says "Commençons..." then "Bonjour ! Je suis..." all
    # inside a single big text burst). We simulate the exact shape the
    # daemon saw when the envelope seq=133378 was emitted. ──
    c3 = _Collector()
    cb3 = AgentTurnCallbacks(
        on_token=c3.on_token,
        on_thinking=c3.on_thinking,
        on_thinking_started=c3.on_thinking_started,
        on_thinking_delta=c3.on_thinking_delta,
    )
    state3 = _StreamState(cb3, ctx=None)
    # First: 1 native-thinking chunk with the FULL reasoning
    chunk = SimpleNamespace(
        delta="",
        finish_reason=None, tool_call=None, tool_calls=None,
        thinking=(
            "Commençons par me présenter et demander ce que le user "
            "souhaite faire. Proposons d'abord quelques options."
        ),
        usage=None,
    )
    await state3.process_chunk(chunk)
    # Then: 1 content chunk with the FULL reply
    chunk = SimpleNamespace(
        delta=(
            "Bonjour ! Je suis votre assistant Builder pour créer "
            "des applications Digitorn."
        ),
        finish_reason=None, tool_call=None, tool_calls=None,
        thinking=None, usage=None,
    )
    await state3.process_chunk(chunk)
    chunk = SimpleNamespace(delta="", finish_reason="stop", tool_call=None,
                             tool_calls=None, thinking=None, usage=None)
    await state3.process_chunk(chunk)
    await state3.flush()

    failures3: list[str] = []
    if len(c3.thinking_snapshots) != 1:
        failures3.append(f"expected 1 snapshot, got {len(c3.thinking_snapshots)}")
    else:
        snap = c3.thinking_snapshots[0]
        if "Bonjour" in snap:
            failures3.append(f"PROD-BUG REGRESSION: snapshot contains reply: {snap!r}")
    v = "".join(c3.tokens)
    if "Commençons" in v:
        failures3.append(f"PROD-BUG REGRESSION: tokens contain thinking: {v!r}")
    if failures3:
        print("FAIL - prod bug scenario:")
        for f in failures3:
            print(f"  - {f}")
        return 1
    print("PASS - exact prod seq=133378 scenario: thinking/reply separated")

    # ── Scenario 4: OPEN <think> with NO </think> close - model
    # forgot to close the tag. Currently _in_think stays True forever
    # and ALL subsequent content pollutes the thinking buffer, emitted
    # at flush() time. This is the most likely prod cause when the
    # reasoning appears as text-tag (not native). ──
    c4 = _Collector()
    cb4 = AgentTurnCallbacks(
        on_token=c4.on_token,
        on_thinking=c4.on_thinking,
        on_thinking_started=c4.on_thinking_started,
        on_thinking_delta=c4.on_thinking_delta,
    )
    state4 = _StreamState(cb4, ctx=None)
    # Model opens <think> but never closes it; reply follows directly
    deltas = [
        "<think>\nCommençons par me présenter ",
        "et demander ce que le user souhaite.\n",
        "Bonjour ! Je suis votre assistant ",
        "Builder pour créer des applications.",
    ]
    for d in deltas:
        chunk = SimpleNamespace(delta=d, finish_reason=None, tool_call=None,
                                 tool_calls=None, thinking=None, usage=None)
        await state4.process_chunk(chunk)
    chunk = SimpleNamespace(delta="", finish_reason="stop", tool_call=None,
                             tool_calls=None, thinking=None, usage=None)
    await state4.process_chunk(chunk)
    await state4.flush()

    failures4: list[str] = []
    v = "".join(c4.tokens)
    snaps = c4.thinking_snapshots
    # Best-case outcome: open <think> with no close → either
    #   (a) everything shows as thinking (no response visible), OR
    #   (b) everything shows as response (no snapshot), OR
    #   (c) separated cleanly (snapshot=thinking only, tokens=reply only)
    # The WORST outcome - the actual prod bug - is: snapshot contains
    # BOTH the thinking AND the reply glued together.
    if snaps and any("Bonjour" in s and "Commençons" in s for s in snaps):
        failures4.append(
            f"UNCLOSED <think> BUG: snapshot glues reasoning+reply: {snaps!r}"
        )
    if failures4:
        print("FAIL - unclosed <think> scenario:")
        for f in failures4:
            print(f"  - {f}")
        return 1
    print(f"PASS - unclosed <think> handled (snaps={len(snaps)}, tokens_len={len(v)})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
