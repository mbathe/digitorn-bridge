"""Baseline harness for the SessionStore-unification refactor.

Builds a self-contained test daemon (filesystem-first, no Postgres for
chat events, optional SQLite for the residual Postgres-bound tables)
and exercises the user-facing surface end-to-end with latency budgets.

Phase 0 goal: capture a green baseline on the CURRENT code so that any
post-refactor regression (correctness OR latency) is detectable.
"""
