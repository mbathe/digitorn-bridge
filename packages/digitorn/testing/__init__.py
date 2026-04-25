"""Digitorn Testing SDK — the CLIENT LIBRARY used to write live tests.

THIS IS NOT WHERE YOU ADD TESTS.

This package exposes the tools (``DevClient``, ``LiveEventStream``,
``assertions``) that a test consumer imports. The tests themselves —
scenarios, fixtures, cases — live OUTSIDE this package, typically in
``tools/live_tests/`` or your own harness.

Rule of thumb:
    - Need a new API endpoint, a new helper, a new assertion primitive?
      Add it HERE. It's a reusable piece of the SDK.
    - Need a scenario that exercises the product (queue, abort,
      cross-session, MCP, RAG, background, channels…)? Write it
      OUTSIDE, import from ``digitorn.testing`` as any external
      consumer would.

Usage:
    from digitorn.testing import DevClient, LiveEventStream, assertions

    client = DevClient()
    session = client.create_session("digitorn-chat")
    stream = client.send_live(session, "hello")
    events = assertions.sort_by_seq(stream.events())
    ok, detail = assertions.event_order(
        events, ["user_message", "message_started", "message_done"],
    )
    stream.stop()
"""

from digitorn.testing.client import DevClient
from digitorn.testing.events import LiveEventStream
from digitorn.testing.models import AppHandle, LiveEvent, SessionHandle, TurnResult
from digitorn.testing import assertions

__all__ = [
    "DevClient",
    "AppHandle",
    "LiveEvent",
    "LiveEventStream",
    "SessionHandle",
    "TurnResult",
    "assertions",
]
