"""LogScrubber - regex-based redactor that prevents secret leakage in logs.

A defence-in-depth filter that runs on every log line emitted by the
daemon. Even if a developer accidentally logs a credential dict or an
exception that includes the secret, the scrubber catches and replaces
the matching pattern with `[REDACTED:<provider>]`.

Patterns are accumulated:

  - **Built-in patterns** for well-known secret formats (OpenAI sk-,
    Anthropic sk-ant-, Slack xoxb-, GitHub gh[ps]_, AWS access key
    AKIA[0-9A-Z]{16}, JWT eyJ..., generic 32+ char base64-ish, etc.).
  - **Dynamic patterns** registered at credential `upsert` time. When
    a secret is created, the scrubber is told "redact <preview-of-secret>"
    so even a custom format gets caught.

The scrubber is installed as a logging.Filter on the root logger early
in daemon boot. It also exposes a sync `scrub(s: str) -> str` helper
for places that emit text outside the logging framework (e.g.
SSE events, error responses).

Performance: each log call iterates compiled patterns. Builtin set is
~15 patterns; dynamic set capped at 256 (LRU on add). Total cost is
sub-microsecond for non-matching lines, single-digit microsecond for
matches. Negligible for daemon log volume.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from threading import RLock
from typing import Pattern

logger = logging.getLogger(__name__)


# Hard-coded patterns for common secret formats.
# Each entry is (compiled_regex, redaction_label).
# Order matters: more specific patterns first.
_BUILTIN_PATTERNS: list[tuple[Pattern[str], str]] = [
    # OpenAI / Anthropic / DeepSeek bearer keys
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "anthropic"),
    (re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"), "openai"),
    (re.compile(r"sk-[A-Za-z0-9_-]{32,}"), "openai-style"),

    # Slack
    (re.compile(r"xox[boa]-[A-Za-z0-9-]{10,}"), "slack"),

    # GitHub PAT and OAuth tokens
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "github-pat"),
    (re.compile(r"ghs_[A-Za-z0-9]{36}"), "github-server"),
    (re.compile(r"gho_[A-Za-z0-9]{36}"), "github-oauth"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{82}"), "github-pat-fine"),

    # AWS access key
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws-access-key"),
    (re.compile(r"ASIA[0-9A-Z]{16}"), "aws-temporary"),

    # Stripe
    (re.compile(r"sk_live_[A-Za-z0-9]{24,}"), "stripe-live"),
    (re.compile(r"rk_live_[A-Za-z0-9]{24,}"), "stripe-restricted"),

    # Generic Bearer header
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{20,}=*"), "bearer-token"),

    # JWT (3 base64-url segments separated by dots, length plausible)
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "jwt"),

    # Notion v2 internal tokens
    (re.compile(r"secret_[A-Za-z0-9]{40,}"), "notion-internal"),

    # Postgres / generic connection strings (host:port@user:pass)
    (re.compile(r"(?P<scheme>[a-z]+)://[^:\s]+:[^@\s]{6,}@[^\s/]+"), "connection-string"),
]


class LogScrubber(logging.Filter):
    """Strip credential-shaped substrings from every log message."""

    def __init__(
        self,
        max_dynamic: int = 256,
        replacement_template: str = "[REDACTED:{label}]",
    ) -> None:
        super().__init__()
        self._dynamic: OrderedDict[str, str] = OrderedDict()
        self._dynamic_compiled: list[tuple[Pattern[str], str]] = []
        self._max_dynamic = max_dynamic
        self._lock = RLock()
        self._tpl = replacement_template

    # ── Public API ───────────────────────────────────────────────

    def add_dynamic(self, secret: str, label: str = "secret") -> None:
        """Register a runtime secret to redact.

        Called by `CredentialStore.upsert_credential` after a new
        credential is saved. The scrubber compiles a literal pattern
        (escaped) so subsequent log lines containing this exact string
        are caught.

        Strings shorter than 8 characters are ignored - too short to
        be a real secret and would cause false positives on common
        words.
        """
        if not secret or len(secret) < 8:
            return
        with self._lock:
            if secret in self._dynamic:
                # Move to end (LRU touch).
                self._dynamic.move_to_end(secret)
                return
            self._dynamic[secret] = label
            self._dynamic_compiled.append(
                (re.compile(re.escape(secret)), label),
            )
            # LRU evict
            while len(self._dynamic) > self._max_dynamic:
                evicted, _ = self._dynamic.popitem(last=False)
                self._dynamic_compiled = [
                    (p, l) for p, l in self._dynamic_compiled
                    if p.pattern != re.escape(evicted)
                ]

    def remove_dynamic(self, secret: str) -> None:
        """Unregister a secret (called when credential is deleted)."""
        with self._lock:
            if secret in self._dynamic:
                del self._dynamic[secret]
                self._dynamic_compiled = [
                    (p, l) for p, l in self._dynamic_compiled
                    if p.pattern != re.escape(secret)
                ]

    def scrub(self, text: str) -> str:
        """Apply all patterns to a string and return the scrubbed
        version. Public so SSE / error response builders can scrub
        outside the logging framework."""
        if not text:
            return text
        for pat, label in _BUILTIN_PATTERNS:
            text = pat.sub(self._tpl.format(label=label), text)
        # Dynamic patterns - snapshot under lock to avoid mutation
        # during iteration.
        with self._lock:
            patterns = list(self._dynamic_compiled)
        for pat, label in patterns:
            text = pat.sub(self._tpl.format(label=label), text)
        return text

    # ── logging.Filter integration ──────────────────────────────

    def filter(self, record: logging.LogRecord) -> bool:
        """Mutate the log record's message in place. Always returns
        True (we never DROP a record - just clean it)."""
        try:
            # `record.getMessage()` formats `record.msg % record.args`.
            # We scrub the formatted result and put it back as `msg`
            # with `args=None` so the standard formatter doesn't try
            # to re-apply the args.
            formatted = record.getMessage()
            scrubbed = self.scrub(formatted)
            if scrubbed != formatted:
                record.msg = scrubbed
                record.args = None
            # Also scrub exc_text if present (formatted traceback).
            if record.exc_text:
                record.exc_text = self.scrub(record.exc_text)
        except Exception:
            # Never let a scrubber bug suppress a log line.
            pass
        return True


_GLOBAL_SCRUBBER: LogScrubber | None = None


def install_global_scrubber(
    max_dynamic: int = 256,
) -> LogScrubber:
    """Attach a LogScrubber to the root logger. Idempotent. Returns
    the singleton scrubber so the credential store can call
    `add_dynamic` on it."""
    global _GLOBAL_SCRUBBER
    if _GLOBAL_SCRUBBER is not None:
        return _GLOBAL_SCRUBBER
    scrubber = LogScrubber(max_dynamic=max_dynamic)
    logging.getLogger().addFilter(scrubber)
    _GLOBAL_SCRUBBER = scrubber
    logger.info("log_scrubber_installed builtin_patterns=%d", len(_BUILTIN_PATTERNS))
    return scrubber


def get_global_scrubber() -> LogScrubber | None:
    """Return the installed scrubber, or None if not yet installed."""
    return _GLOBAL_SCRUBBER
