"""Pydantic schema for the quota envelope (= what an admin writes
into a Plan or a per-user override).

Mirrors the existing daemon schema (`core/quota.py`) so the contract
is identical: 6 metrics x 5 calendar windows + free-form custom
windows + per-model overrides + 3 non-time caps.

The shape is identical so a frontend that knew how to render the
daemon's quota config keeps working against the gateway after the
migration. Once the daemon's quota.py is physically deleted, this
file is the only schema definition left.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ── Window parsing ─────────────────────────────────────────────────


_WINDOW_RE = re.compile(r"^(\d+)([smhdw])$")
_WINDOW_UNIT_TO_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def window_to_seconds(window: str) -> int:
    """Parse a window literal (`5h`, `30m`, `3d`, `90s`, `2w`) into
    seconds. Raises ValueError on unrecognised shape - kept strict
    so admin typos surface at write time, not at enforcement time.
    """
    m = _WINDOW_RE.match(window)
    if not m:
        raise ValueError(
            f"invalid window literal {window!r}: expected '<int><unit>' "
            "with unit in s/m/h/d/w (e.g. '5h', '30m', '3d')",
        )
    value, unit = int(m.group(1)), m.group(2)
    if value <= 0:
        raise ValueError(f"invalid window literal {window!r}: value must be > 0")
    return value * _WINDOW_UNIT_TO_SECONDS[unit]


# ── Reset strategies ───────────────────────────────────────────────


ResetStrategy = Literal[
    "fixed",              # epoch-aligned bucket, useful for "5h" / "30m"
    "fixed_daily",        # UTC midnight
    "fixed_weekly",       # Monday UTC midnight
    "fixed_monthly",      # 1st of month UTC 00:00
    "rolling_from_first", # starts on first consumption, ends N seconds later
]


# ── Rule + metric ──────────────────────────────────────────────────


class QuotaRule(BaseModel):
    """A single limit attached to one window."""

    model_config = ConfigDict(extra="forbid")

    limit: float = Field(..., ge=0)
    reset: ResetStrategy = "fixed"


def _coerce_rule(value: Any) -> QuotaRule | None:
    """Accept ``int``/``float`` as shorthand for ``{limit: N}``, or a
    full ``{limit, reset}`` dict, or pass-through. None passes through.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Rule cannot be a bool")
    if isinstance(value, (int, float)):
        return QuotaRule(limit=float(value), reset="fixed")
    if isinstance(value, dict):
        return QuotaRule.model_validate(value)
    if isinstance(value, QuotaRule):
        return value
    raise ValueError(
        f"Rule must be int/float or {{limit,reset}} dict, got {type(value).__name__}",
    )


class MetricQuota(BaseModel):
    """Limits for a single metric across several windows. All non-None
    rules fire independently - the request is rejected as soon as ONE
    overflows.
    """

    model_config = ConfigDict(extra="forbid")

    per_minute: QuotaRule | None = None
    per_hour: QuotaRule | None = None
    per_day: QuotaRule | None = None
    per_week: QuotaRule | None = None
    per_month: QuotaRule | None = None

    custom: dict[str, QuotaRule] | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_shorthand(cls, data: Any) -> Any:
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
            for k in out["custom"]:
                window_to_seconds(k)
        return out

    def rules(self) -> list[tuple[str, QuotaRule]]:
        """Flatten the metric into ``[(window_name, rule), ...]`` for
        enforcement. Named windows are upgraded to their calendar
        reset when the admin left ``reset=fixed``.
        """
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
            r = rule
            if r.reset == "fixed" and default_reset != "fixed":
                r = QuotaRule(limit=r.limit, reset=default_reset)  # type: ignore[arg-type]
            out.append((name, r))
        for win, rule in (self.custom or {}).items():
            out.append((win, rule))
        return out


# ── Per-model override ─────────────────────────────────────────────


class ModelOverride(BaseModel):
    """Per-model overrides. Same shape as the top-level; any field
    set here overrides the parent scope for that model only."""

    model_config = ConfigDict(extra="forbid")

    requests: MetricQuota | None = None
    tokens_input: MetricQuota | None = None
    tokens_output: MetricQuota | None = None
    tokens_total: MetricQuota | None = None
    cost_usd: MetricQuota | None = None
    messages: MetricQuota | None = None


# ── Top-level definition ───────────────────────────────────────────


METRICS: tuple[str, ...] = (
    "requests",
    "tokens_input",
    "tokens_output",
    "tokens_total",
    "cost_usd",
    "messages",
)


class QuotaDefinition(BaseModel):
    """The full quota contract attached to a Plan (or per-user override)."""

    model_config = ConfigDict(extra="forbid")

    requests: MetricQuota | None = None
    tokens_input: MetricQuota | None = None
    tokens_output: MetricQuota | None = None
    tokens_total: MetricQuota | None = None
    cost_usd: MetricQuota | None = None
    messages: MetricQuota | None = None

    concurrent_sessions: int | None = Field(None, ge=0)
    messages_per_session: int | None = Field(None, ge=0)
    session_duration_seconds: int | None = Field(None, ge=0)

    models: dict[str, ModelOverride] | None = None

    def metric(self, name: str) -> MetricQuota | None:
        return getattr(self, name, None) if name in METRICS else None

    def metric_for_model(
        self, name: str, model_alias: str | None,
    ) -> MetricQuota | None:
        """Per-model override wins if the alias matches an entry in
        ``models`` AND that model has a rule for the requested metric.
        """
        if model_alias and self.models and model_alias in self.models:
            override = self.models[model_alias]
            override_metric = getattr(override, name, None)
            if override_metric is not None:
                return override_metric
        return self.metric(name)


# ── HTTP request shapes ────────────────────────────────────────────


class PlanCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = Field(None, max_length=512)
    is_default: bool = False
    quota_def: QuotaDefinition


class PlanUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = Field(None, max_length=512)
    is_default: bool | None = None
    quota_def: QuotaDefinition | None = None


class UserPlanAssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(..., min_length=1, max_length=64)
    override_quota_def: QuotaDefinition | None = None
    reason: str | None = Field(
        default=None,
        max_length=512,
        description=(
            "Optional free-text reason for the change. Stored in "
            "gateway_user_plan_history for the audit trail."
        ),
    )
