"""Rich quota store — admin-defined limits across 4 dimensions.

Covers per-app, per-user, per-session and per-model quotas with
arbitrary **window durations** and two **reset strategies**:

- ``fixed`` / ``fixed_daily`` / ``fixed_weekly`` / ``fixed_monthly`` —
  aligned to the wall clock / calendar (reset at UTC midnight, Monday
  midnight, first of the month, …).

- ``rolling_from_first`` — the window opens on the *first* consumption
  and closes exactly ``window_seconds`` later. After that, the counter
  resets to zero and the next consumption opens a brand-new window.
  This is the Claude-style "100 messages per 5h" semantics.

Windows accept both named aliases (``per_minute``, ``per_hour``,
``per_day``, ``per_week``, ``per_month``) and arbitrary durations
written as ``<N><unit>`` where unit is ``s|m|h|d|w`` — e.g. ``"5h"``,
``"30m"``, ``"3d"``, ``"2w"``. Any metric can carry multiple rules
simultaneously; the most restrictive one fires first.

Metrics supported:
    - ``requests``         — HTTP calls / agent turns
    - ``tokens_input``     — LLM prompt tokens
    - ``tokens_output``    — LLM completion tokens
    - ``tokens_total``     — input + output
    - ``cost_usd``         — provider-reported cost in USD
    - ``messages``         — user ↔ assistant exchanges
    - ``concurrent_sessions`` (int, not a rule — instantaneous check)
    - ``messages_per_session`` (int, not a rule — session-scoped total)

Admins write the full blob via ``PUT``; readers get back both the raw
definition (what was set) and the effective one (after inheriting
global → app → user). Enforcement happens in three places:

    - HTTP middleware (requests per minute) — live today
    - Agent loop (tokens, messages, per-session caps) — wired progressively
    - LLM provider (cost_usd) — wired progressively

Backward compatibility: the legacy ``{"rpm": N}`` and
``{"requests": {"per_minute": N}}`` shapes keep working. They get
auto-upgraded to the rule-based form at read time.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from digitorn.core.kv import KeyValueBackend

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Window parsing — "5h" / "per_day" / "30m" → seconds
# ──────────────────────────────────────────────────────────────────

_NAMED_WINDOWS: dict[str, int] = {
    "per_minute": 60,
    "per_hour": 3600,
    "per_day": 86400,
    "per_week": 604800,
    "per_month": 2592000,   # 30-day approximation for enforcement bucket
    # Note: ``per_month`` with ``fixed_monthly`` reset uses real calendar
    # boundaries — the seconds count is only used for the rolling variant.
}

_UNIT_SECONDS: dict[str, int] = {
    "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800,
}

_WINDOW_RE = re.compile(r"^(\d+)([smhdw])$")


def _window_to_seconds(window: str) -> int:
    """Parse ``window`` into seconds. Raises ``ValueError`` if invalid.

    Accepts named aliases (``per_minute``, …) and the compact form
    ``<N><unit>`` (``5h``, ``30m``, ``7d``, ``2w``).
    """
    if not isinstance(window, str) or not window:
        raise ValueError(f"Window must be a non-empty string, got {window!r}")
    if window in _NAMED_WINDOWS:
        return _NAMED_WINDOWS[window]
    match = _WINDOW_RE.match(window.strip().lower())
    if not match:
        raise ValueError(
            f"Invalid window {window!r}. Use a named alias "
            f"({', '.join(_NAMED_WINDOWS)}) or the compact form "
            f"<N><unit> where unit ∈ s|m|h|d|w (e.g. '5h')."
        )
    n, unit = int(match.group(1)), match.group(2)
    if n <= 0:
        raise ValueError(f"Window amount must be positive, got {n}")
    return n * _UNIT_SECONDS[unit]


def _compute_fixed_reset(window: str, reset: str, now: float) -> float:
    """Return the next reset timestamp for a fixed-schedule window.

    For ``fixed`` we align to epoch modulo window_seconds (simple bucket).
    For ``fixed_daily`` / ``fixed_weekly`` / ``fixed_monthly`` we use UTC
    calendar boundaries (midnight / Monday / first of month).
    """
    if reset == "fixed":
        w = _window_to_seconds(window)
        bucket_start = int(now / w) * w
        return float(bucket_start + w)

    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    if reset == "fixed_daily":
        nxt = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif reset == "fixed_weekly":
        days_to_monday = (7 - dt.weekday()) % 7 or 7
        nxt = (dt + timedelta(days=days_to_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
    elif reset == "fixed_monthly":
        if dt.month == 12:
            nxt = dt.replace(year=dt.year + 1, month=1, day=1,
                             hour=0, minute=0, second=0, microsecond=0)
        else:
            nxt = dt.replace(month=dt.month + 1, day=1,
                             hour=0, minute=0, second=0, microsecond=0)
    else:
        # Fallback to fixed-bucket arithmetic.
        w = _window_to_seconds(window)
        bucket_start = int(now / w) * w
        return float(bucket_start + w)
    return nxt.timestamp()


# ──────────────────────────────────────────────────────────────────
# Pydantic schema — the admin contract
# ──────────────────────────────────────────────────────────────────


ResetStrategy = Literal[
    "fixed",              # epoch-aligned bucket (useful for "5h" / "30m")
    "fixed_daily",        # UTC midnight
    "fixed_weekly",       # Monday UTC midnight
    "fixed_monthly",      # 1st of month UTC 00:00
    "rolling_from_first", # starts on first consumption, ends N seconds later
]


class QuotaRule(BaseModel):
    """A single limit attached to one window."""
    model_config = ConfigDict(extra="forbid")

    limit: int | float = Field(..., ge=0)
    reset: ResetStrategy = "fixed"


def _coerce_rule(value: Any) -> QuotaRule | None:
    """Accept ``int``/``float`` as shorthand for ``{limit: N, reset: 'fixed'}``,
    or a full dict ``{limit, reset}``. ``None`` passes through."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return QuotaRule(limit=float(value), reset="fixed")
    if isinstance(value, dict):
        return QuotaRule.model_validate(value)
    if isinstance(value, QuotaRule):
        return value
    raise ValueError(f"Rule must be int/float or {{limit,reset}} dict, got {type(value).__name__}")


class MetricQuota(BaseModel):
    """Limits for a single metric across several windows.

    The five first-class fields (``per_minute`` / ``per_hour`` /
    ``per_day`` / ``per_week`` / ``per_month``) accept either a plain
    integer (shorthand for ``{limit: N, reset: 'fixed'}`` — or the
    calendar-aligned equivalent where it makes sense) or a full
    ``QuotaRule``. Arbitrary windows go in ``custom`` keyed by the
    duration literal (``5h``, ``30m``, ``3d``).

    Multiple rules coexist: all fire independently. The request is
    rejected as soon as one rule would overflow.
    """
    model_config = ConfigDict(extra="forbid")

    per_minute: QuotaRule | None = None
    per_hour: QuotaRule | None = None
    per_day: QuotaRule | None = None
    per_week: QuotaRule | None = None
    per_month: QuotaRule | None = None

    # Arbitrary windows — keys like "5h", "3d", "30m".
    custom: dict[str, QuotaRule] | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_shorthand(cls, data: Any) -> Any:
        """Allow ``int``/``float`` for the named fields and for custom entries."""
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for f in ("per_minute", "per_hour", "per_day", "per_week", "per_month"):
            if f in out:
                out[f] = _coerce_rule(out[f])
        if "custom" in out and isinstance(out["custom"], dict):
            out["custom"] = {
                k: _coerce_rule(v) for k, v in out["custom"].items()
            }
            # Validate window syntax now, so the error surfaces here
            # rather than at enforcement time.
            for k in out["custom"]:
                _window_to_seconds(k)
        return out

    def rules(self) -> list[tuple[str, QuotaRule]]:
        """Flatten the metric into ``[(window_name, rule), ...]`` for
        enforcement. Named fields are emitted with a sensible default
        reset (e.g. ``per_day`` → ``fixed_daily`` when not overridden)."""
        out: list[tuple[str, QuotaRule]] = []
        named_defaults = {
            "per_minute": "fixed",
            "per_hour": "fixed",
            "per_day": "fixed_daily",
            "per_week": "fixed_weekly",
            "per_month": "fixed_monthly",
        }
        for name, default_reset in named_defaults.items():
            rule: QuotaRule | None = getattr(self, name)
            if rule is None:
                continue
            # If the admin left reset=fixed on a calendar-aligned window,
            # upgrade to the matching calendar reset so "per_day: 1000"
            # behaves as the user expects.
            r = rule
            if r.reset == "fixed" and default_reset != "fixed":
                r = QuotaRule(limit=r.limit, reset=default_reset)
            out.append((name, r))
        for win, rule in (self.custom or {}).items():
            out.append((win, rule))
        return out


class ModelOverride(BaseModel):
    """Per-model overrides. Same structure as the top-level; any field
    set here overrides the parent scope for that model only."""
    model_config = ConfigDict(extra="forbid")

    requests: MetricQuota | None = None
    tokens_input: MetricQuota | None = None
    tokens_output: MetricQuota | None = None
    tokens_total: MetricQuota | None = None
    cost_usd: MetricQuota | None = None
    messages: MetricQuota | None = None


class QuotaDefinition(BaseModel):
    """The full quota contract — what an admin writes via PUT."""
    model_config = ConfigDict(extra="forbid")

    # Time-windowed metrics (one MetricQuota per metric — multi-window support).
    requests: MetricQuota | None = None
    tokens_input: MetricQuota | None = None
    tokens_output: MetricQuota | None = None
    tokens_total: MetricQuota | None = None
    cost_usd: MetricQuota | None = None
    messages: MetricQuota | None = None

    # Non-time-windowed caps.
    concurrent_sessions: int | None = Field(None, ge=0)
    messages_per_session: int | None = Field(None, ge=0)
    session_duration_seconds: int | None = Field(
        None, ge=0,
        description=(
            "Max wall-clock duration of a single session. When this elapses "
            "a new session is required — same idea as Claude's conversation "
            "auto-rotation. 0/null = no limit."
        ),
    )

    # Per-model overrides.
    models: dict[str, ModelOverride] | None = None


class QuotaPutRequest(BaseModel):
    """PUT body — accepts the legacy shape ``{rpm: N}`` OR the rich shape.

    Admin-only. The daemon rejects writes from non-admin callers at the
    HTTP route layer, not here.
    """
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    rpm: int | None = Field(
        None, ge=0,
        description="Legacy shortcut — maps to requests.per_minute.",
    )
    quota: QuotaDefinition | None = None

    @model_validator(mode="after")
    def _coerce(self) -> "QuotaPutRequest":
        if self.quota is None and self.rpm is not None:
            self.quota = QuotaDefinition(
                requests=MetricQuota(
                    per_minute=QuotaRule(limit=self.rpm, reset="fixed"),
                ),
            )
        elif self.quota is not None and self.rpm is not None:
            # Legacy wins on per_minute only.
            reqs = self.quota.requests or MetricQuota()
            reqs.per_minute = QuotaRule(limit=self.rpm, reset="fixed")
            self.quota.requests = reqs
        if self.quota is None:
            raise ValueError(
                "Provide either {rpm} (legacy) or {quota: {...}} (recommended).",
            )
        return self


# ──────────────────────────────────────────────────────────────────
# Storage — KV-backed definition store + rolling counter tracker
# ──────────────────────────────────────────────────────────────────


@dataclass
class CounterState:
    """Result of a counter increment. Callers raise / gate on ``over``."""
    metric: str
    window: str
    current: float
    limit: float
    reset_at: float
    over: bool
    scope: str = ""   # "app" / "user" — which level triggered


class QuotaExceededError(Exception):
    """Raised by ``QuotaStore.check`` when one of the rules would overflow.

    Carries enough structured info for the HTTP layer to produce a
    proper 429 with ``Retry-After`` and for SSE to emit a typed
    ``quota_exceeded`` event the client can render.
    """

    def __init__(self, state: CounterState) -> None:
        self.state = state
        # User-friendly default message — the HTTP error classifier
        # (_errors.py §2) replaces this with an even richer version
        # before it reaches the client, but we still aim to produce a
        # message that reads well on its own (agent logs, sync-mode
        # error returns, CLI output, etc.).
        pretty_metric = _PRETTY_METRIC_NAMES.get(state.metric, state.metric)
        pretty_window = _PRETTY_WINDOW_NAMES.get(state.window, state.window)
        retry_s = int(max(0, state.reset_at - time.time()))
        scope_label = {"app": "app-wide", "user": "per-user"}.get(state.scope, state.scope)
        super().__init__(
            f"You've hit the {scope_label} {pretty_metric} limit "
            f"({int(state.current)}/{int(state.limit)} "
            f"{pretty_window}). Try again in {retry_s}s."
        )

    def to_dict(self) -> dict[str, Any]:
        s = self.state
        return {
            "error": "quota_exceeded",
            "scope": s.scope,
            "metric": s.metric,
            "window": s.window,
            "current": s.current,
            "limit": s.limit,
            "reset_at": s.reset_at,
            "retry_after_seconds": max(0, s.reset_at - time.time()),
        }


_PRETTY_METRIC_NAMES: dict[str, str] = {
    "requests": "request",
    "messages": "message",
    "tokens_input": "input-token",
    "tokens_output": "output-token",
    "tokens_total": "token",
    "cost_usd": "USD cost",
    "concurrent_sessions": "concurrent session",
    "messages_per_session": "message-per-session",
    "session_duration_seconds": "session duration",
}


_PRETTY_WINDOW_NAMES: dict[str, str] = {
    "per_minute": "per minute",
    "per_hour": "per hour",
    "per_day": "per day",
    "per_week": "per week",
    "per_month": "per month",
}


class QuotaStore:
    """KV-backed store for quota definitions + rolling counters.

    Definition keys:
        __qdef__:<app_id>                app-level envelope
        __qdef__:<app_id>:u:<user_id>    user override on this app

    Each envelope = ``{quota: {QuotaDefinition}, updated_at, updated_by}``.

    Counter keys (for ``incr``, used by enforcement code):
        cnt:<scope>:<metric>:<window>:<bucket_id>   fixed-bucket counter
        roll:<scope>:<metric>:<window>              rolling counter envelope
    """

    def __init__(self, backend: KeyValueBackend) -> None:
        self._backend = backend

    # ── Writes ─────────────────────────────────────────────────────

    def set_app_quota(
        self,
        app_id: str,
        quota: QuotaDefinition,
        *,
        updated_by: str = "",
    ) -> dict[str, Any]:
        envelope = {
            "quota": quota.model_dump(mode="json", exclude_none=True),
            "updated_at": _iso_now(),
            "updated_by": updated_by,
        }
        self._backend.set(self._app_key(app_id), envelope)
        return envelope

    def set_user_quota(
        self,
        app_id: str,
        user_id: str,
        quota: QuotaDefinition,
        *,
        updated_by: str = "",
    ) -> dict[str, Any]:
        envelope = {
            "quota": quota.model_dump(mode="json", exclude_none=True),
            "updated_at": _iso_now(),
            "updated_by": updated_by,
        }
        self._backend.set(self._user_key(app_id, user_id), envelope)
        return envelope

    # ── Reads ──────────────────────────────────────────────────────

    def get_app_quota(self, app_id: str) -> dict[str, Any] | None:
        raw = self._backend.get(self._app_key(app_id))
        return raw if isinstance(raw, dict) else None

    def get_user_quota(self, app_id: str, user_id: str) -> dict[str, Any] | None:
        raw = self._backend.get(self._user_key(app_id, user_id))
        return raw if isinstance(raw, dict) else None

    def effective_quota(
        self,
        app_id: str,
        user_id: str | None = None,
        *,
        global_default_rpm: int = 60,
    ) -> dict[str, Any]:
        """Return the merged quota after global → app → user inheritance.

        A ``None`` leaf in the override means "inherit from parent".
        """
        base = QuotaDefinition(
            requests=MetricQuota(
                per_minute=QuotaRule(limit=global_default_rpm, reset="fixed"),
            ),
        ).model_dump(mode="json", exclude_none=True)

        app_env = self.get_app_quota(app_id)
        if app_env and isinstance(app_env.get("quota"), dict):
            base = _merge_quota(base, app_env["quota"])

        if user_id:
            user_env = self.get_user_quota(app_id, user_id)
            if user_env and isinstance(user_env.get("quota"), dict):
                base = _merge_quota(base, user_env["quota"])

        return base

    # ── Deletes ────────────────────────────────────────────────────

    def remove_app_quota(self, app_id: str) -> bool:
        had = self._backend.get(self._app_key(app_id)) is not None
        self._backend.delete(self._app_key(app_id))
        return had

    def remove_user_quota(self, app_id: str, user_id: str) -> bool:
        had = self._backend.get(self._user_key(app_id, user_id)) is not None
        self._backend.delete(self._user_key(app_id, user_id))
        return had

    # ── High-level enforcement helpers ─────────────────────────────

    def check_and_charge(
        self,
        *,
        app_id: str,
        user_id: str | None,
        charges: dict[str, float],
        model: str | None = None,
    ) -> None:
        """Atomic: for every metric in ``charges``, check every rule
        against limit AT BOTH SCOPES (app + user when user_id given),
        then increment if nothing overflows.

        Raises :class:`QuotaExceededError` on the first rule that would
        go over the limit, WITHOUT having incremented anything. Caller
        catches and propagates as 429/SSE.

        ``charges`` example::

            {"requests": 1, "messages": 1, "tokens_input": 4200,
             "tokens_output": 180, "tokens_total": 4380, "cost_usd": 0.012}

        ``model`` routes the charges through per-model overrides when
        they exist — the per-model rules are enforced ON TOP of the
        aggregate (so an Opus call consumes both the model bucket and
        the global bucket).
        """
        # Resolve effective quota for both scopes.
        from digitorn.core.config import get_settings
        try:
            settings = get_settings()
            global_rpm = int(getattr(settings.server, "rate_limit_rpm", 60))
        except Exception:
            global_rpm = 60

        app_eff = self.effective_quota(app_id, global_default_rpm=global_rpm)
        user_eff = (
            self.effective_quota(app_id, user_id=user_id, global_default_rpm=global_rpm)
            if user_id else None
        )

        # Phase 1 — pre-flight check every rule at every scope.
        scopes_to_check: list[tuple[str, str, dict]] = [
            ("app", f"app:{app_id}", app_eff),
        ]
        if user_id and user_eff is not None:
            scopes_to_check.append(
                ("user", f"user:{user_id}:app:{app_id}", user_eff),
            )

        # Build list of (scope_label, scope_key, metric, window, reset, limit, amount)
        plan: list[tuple[str, str, str, str, str, float, float]] = []
        for scope_label, scope_key, eff in scopes_to_check:
            for metric, amount in charges.items():
                if amount <= 0:
                    continue
                plan.extend(self._expand_rules(
                    scope_label, scope_key, eff, metric, amount,
                ))
                # Per-model overrides stack on top of the aggregate.
                if model:
                    model_cfg = (eff.get("models") or {}).get(model)
                    if isinstance(model_cfg, dict):
                        model_scope_key = f"{scope_key}:model:{model}"
                        plan.extend(self._expand_rules(
                            scope_label, model_scope_key, model_cfg, metric, amount,
                        ))

        # Phase 1a — peek current, check if (current + amount) > limit.
        for scope_label, scope_key, metric, window, reset, limit, amount in plan:
            current, reset_at = self.peek_counter(
                scope=scope_key, metric=metric, window=window, reset=reset,
            )
            if (current + amount) > limit:
                raise QuotaExceededError(CounterState(
                    metric=metric, window=window,
                    current=current + amount, limit=limit,
                    reset_at=reset_at, over=True, scope=scope_label,
                ))

        # Phase 2 — all checks passed, actually charge.
        for scope_label, scope_key, metric, window, reset, limit, amount in plan:
            self.incr_counter(
                scope=scope_key, metric=metric, window=window,
                reset=reset, amount=amount,
            )

    def _expand_rules(
        self, scope_label: str, scope_key: str, eff: dict,
        metric: str, amount: float,
    ) -> list[tuple[str, str, str, str, str, float, float]]:
        """Turn a metric's MetricQuota dict into a flat list of
        ``(scope_label, scope_key, metric, window, reset, limit, amount)``
        tuples. Skips metrics that aren't defined at this scope.
        """
        out: list[tuple[str, str, str, str, str, float, float]] = []
        bucket = eff.get(metric)
        if not isinstance(bucket, dict):
            return out
        # Rehydrate MetricQuota to use its rules() helper (handles the
        # auto-upgrade of reset strategies for calendar-aligned windows).
        try:
            mq = MetricQuota.model_validate(bucket)
        except Exception:
            return out
        for window, rule in mq.rules():
            out.append((
                scope_label, scope_key, metric, window,
                rule.reset, float(rule.limit), amount,
            ))
        return out

    def snapshot_usage(
        self,
        app_id: str,
        user_id: str | None = None,
        *,
        global_default_rpm: int = 60,
    ) -> dict[str, Any]:
        """Return current counters for every rule in the effective quota,
        without incrementing. Used by the GET endpoints to surface a
        full usage report to the admin UI.
        """
        eff = self.effective_quota(
            app_id, user_id=user_id, global_default_rpm=global_default_rpm,
        )
        scope_key = (
            f"user:{user_id}:app:{app_id}" if user_id else f"app:{app_id}"
        )
        out: dict[str, Any] = {}
        for metric in ("requests", "tokens_input", "tokens_output",
                       "tokens_total", "cost_usd", "messages"):
            bucket = eff.get(metric)
            if not isinstance(bucket, dict):
                continue
            try:
                mq = MetricQuota.model_validate(bucket)
            except Exception:
                continue
            report: dict[str, Any] = {}
            for window, rule in mq.rules():
                current, reset_at = self.peek_counter(
                    scope=scope_key, metric=metric,
                    window=window, reset=rule.reset,
                )
                report[window] = {
                    "current": current,
                    "limit": float(rule.limit),
                    "reset_at": reset_at,
                    "reset_at_iso": datetime.fromtimestamp(
                        reset_at, tz=timezone.utc,
                    ).isoformat().replace("+00:00", "Z"),
                    "reset": rule.reset,
                }
            if report:
                out[metric] = report
        return out

    # ── Rolling + fixed counter primitives (used by enforcement) ──

    def incr_counter(
        self,
        *,
        scope: str,             # e.g. "app:my-app" / "user:u1:app:my-app"
        metric: str,
        window: str,
        reset: str,
        amount: float,
    ) -> tuple[float, float]:
        """Increment the counter for (scope, metric, window). Returns
        ``(new_total, reset_at_epoch)``. Caller compares to the rule's
        limit. TTL-safe on both DiskCache and Redis backends.
        """
        now = time.time()
        if reset == "rolling_from_first":
            return self._incr_rolling(scope, metric, window, now, amount)
        return self._incr_fixed(scope, metric, window, reset, now, amount)

    def peek_counter(
        self,
        *,
        scope: str,
        metric: str,
        window: str,
        reset: str,
    ) -> tuple[float, float]:
        """Return ``(current, reset_at)`` without incrementing."""
        now = time.time()
        if reset == "rolling_from_first":
            return self._peek_rolling(scope, metric, window, now)
        return self._peek_fixed(scope, metric, window, reset, now)

    # ── Internals ──────────────────────────────────────────────────

    def _app_key(self, app_id: str) -> str:
        return f"__qdef__:{app_id}"

    def _user_key(self, app_id: str, user_id: str) -> str:
        return f"__qdef__:{app_id}:u:{user_id}"

    def _incr_fixed(
        self, scope: str, metric: str, window: str, reset: str,
        now: float, amount: float,
    ) -> tuple[float, float]:
        w = _window_to_seconds(window)
        reset_at = _compute_fixed_reset(window, reset, now)
        bucket_id = int(reset_at)
        key = f"cnt:{scope}:{metric}:{window}:{bucket_id}"
        # Prefer the atomic incr path when the backend exposes it.
        try:
            new_total = self._backend.incr(
                key, delta=int(amount) if amount == int(amount) else amount,
                expire=max(w * 2, 300),
            )
        except (AttributeError, TypeError):
            current = self._backend.get(key, 0) or 0
            new_total = float(current) + amount
            self._backend.set(key, new_total, expire=max(w * 2, 300))
        return (float(new_total), reset_at)

    def _peek_fixed(
        self, scope: str, metric: str, window: str, reset: str, now: float,
    ) -> tuple[float, float]:
        reset_at = _compute_fixed_reset(window, reset, now)
        bucket_id = int(reset_at)
        key = f"cnt:{scope}:{metric}:{window}:{bucket_id}"
        return (float(self._backend.get(key, 0) or 0), reset_at)

    def _incr_rolling(
        self, scope: str, metric: str, window: str, now: float, amount: float,
    ) -> tuple[float, float]:
        w = _window_to_seconds(window)
        key = f"roll:{scope}:{metric}:{window}"
        env = self._backend.get(key) or {}
        started_at = float(env.get("started_at", 0) or 0)
        current = float(env.get("current", 0) or 0)
        # Expired? → fresh window
        if started_at == 0 or (now - started_at) >= w:
            started_at = now
            current = 0.0
        current += amount
        self._backend.set(
            key,
            {"started_at": started_at, "current": current},
            expire=max(w * 2, 300),
        )
        return (current, started_at + w)

    def _peek_rolling(
        self, scope: str, metric: str, window: str, now: float,
    ) -> tuple[float, float]:
        w = _window_to_seconds(window)
        key = f"roll:{scope}:{metric}:{window}"
        env = self._backend.get(key) or {}
        started_at = float(env.get("started_at", 0) or 0)
        current = float(env.get("current", 0) or 0)
        if started_at == 0 or (now - started_at) >= w:
            return (0.0, now + w)
        return (current, started_at + w)

    # ── Listing (best-effort, requires backend iteration) ──────────

    def list_user_overrides(self, app_id: str) -> list[dict[str, Any]]:
        """Raw envelopes for every user override on this app."""
        prefix = f"__qdef__:{app_id}:u:"
        out: list[dict[str, Any]] = []
        try:
            keys = self._list_keys(prefix)
        except Exception as exc:
            logger.debug("list_user_overrides: backend iteration failed: %s", exc)
            return []
        for key in keys:
            user_id = key[len(prefix):]
            env = self._backend.get(key)
            if isinstance(env, dict):
                out.append({"user_id": user_id, **env})
        return out

    def _list_keys(self, prefix: str) -> list[str]:
        backend = self._backend
        for attr in ("list_keys", "iter_keys", "keys"):
            method = getattr(backend, attr, None)
            if callable(method):
                try:
                    return [k for k in method(prefix) if k.startswith(prefix)]
                except TypeError:
                    try:
                        return [k for k in method() if k.startswith(prefix)]
                    except Exception:
                        continue
                except Exception:
                    continue
        inner = getattr(backend, "_cache", None)
        if inner is not None:
            try:
                return [k for k in inner.iterkeys() if k.startswith(prefix)]
            except Exception:
                pass
        return []


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _merge_quota(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge two quota dicts. Override wins on leaf values; ``None``
    leaves the base value in place (inherit semantics).

    For the nested ``models`` dict, each model key is merged independently.
    For ``custom`` dicts under a metric, overrides add/replace the named
    window without touching the other named windows.
    """
    out = dict(base)
    for k, v in override.items():
        if k == "models" and isinstance(v, dict):
            merged = dict(out.get("models") or {})
            for mname, mcfg in v.items():
                if mname in merged and isinstance(merged[mname], dict) and isinstance(mcfg, dict):
                    merged[mname] = _merge_quota(merged[mname], mcfg)
                else:
                    merged[mname] = mcfg
            out["models"] = merged
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_quota(out[k], v)
        elif v is not None:
            out[k] = v
    return out


__all__ = [
    "CounterState",
    "MetricQuota",
    "ModelOverride",
    "QuotaDefinition",
    "QuotaPutRequest",
    "QuotaRule",
    "QuotaStore",
    "ResetStrategy",
    "_compute_fixed_reset",
    "_window_to_seconds",
]
