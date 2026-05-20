"""Client kind detection from request headers.

Every browser SPA, Flutter desktop bundle, Flutter mobile app and CLI
script sends `X-Digitorn-Client` on each request so the daemon can
adapt rendering, attachment handling, feature gating and telemetry per
surface without UA sniffing.

The value is informational - never security-critical. It is trusted
for hints (e.g. "show the markdown footer inline because the web
markdown renderer doesn't expand collapsibles") but spoofable on
purpose. Anything sensitive must come from the JWT, not from here.

Lookup helper: handlers call `client_kind_of(request)` and switch
on the returned enum. The middleware in `server.py` populates
`request.state.client_kind` once per request so repeated calls are
free.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request


CLIENT_HEADER = "x-digitorn-client"


class ClientKind(StrEnum):
    """Granular surface label - matches the values clients are
    contracted to send. Branching on `is_web` / `is_flutter` is
    cheaper and more readable than string comparisons.
    """

    WEB = "web"
    FLUTTER_DESKTOP = "flutter-desktop"
    FLUTTER_MOBILE = "flutter-mobile"
    CLI = "cli"
    UNKNOWN = "unknown"

    @property
    def is_web(self) -> bool:
        return self is ClientKind.WEB

    @property
    def is_flutter(self) -> bool:
        return self in (ClientKind.FLUTTER_DESKTOP, ClientKind.FLUTTER_MOBILE)


def parse_client_kind(raw: str | None) -> ClientKind:
    """Map a raw `X-Digitorn-Client` header value to its enum case.

    Unknown / missing values land on `ClientKind.UNKNOWN` so callers
    never have to special-case `None`. Legacy Flutter builds that
    haven't migrated to the granular suffix yet send the bare
    `flutter` token; that resolves to `FLUTTER_DESKTOP` because
    that's the only Flutter surface deployed today. Re-evaluate the
    default when the mobile build ships.
    """
    if not raw:
        return ClientKind.UNKNOWN
    v = raw.strip().lower()
    if v == "web":
        return ClientKind.WEB
    if v == "flutter-desktop":
        return ClientKind.FLUTTER_DESKTOP
    if v == "flutter-mobile":
        return ClientKind.FLUTTER_MOBILE
    if v in ("flutter", "flutter-unknown"):
        return ClientKind.FLUTTER_DESKTOP
    if v == "cli":
        return ClientKind.CLI
    return ClientKind.UNKNOWN


def client_kind_of(request: "Request") -> ClientKind:
    """Read the parsed client kind, falling back to a fresh header
    parse if the middleware hasn't run yet (test harness, internal
    sub-request, etc.). Returns `UNKNOWN` rather than raising.
    """
    state = getattr(request, "state", None)
    if state is not None:
        ck = getattr(state, "client_kind", None)
        if isinstance(ck, ClientKind):
            return ck
    return parse_client_kind(request.headers.get(CLIENT_HEADER))
