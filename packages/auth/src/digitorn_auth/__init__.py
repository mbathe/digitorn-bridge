"""digitorn-auth - central authentication service.

Standalone FastAPI service that owns user accounts, JWT issuance,
OAuth federation, and device pairing for the Digitorn ecosystem.

The daemon's embedded ``digitorn.core.auth`` keeps running unchanged
during the migration window; this package builds the central service
in parallel against the same Postgres so we can switch over without
touching live data.
"""

__version__ = "0.1.0"
