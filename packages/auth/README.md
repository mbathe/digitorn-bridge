# digitorn-auth

Standalone authentication service for the Digitorn ecosystem.

## What it is

A self-contained FastAPI service that owns:

- User accounts (local password, OAuth federation Google / Microsoft / GitHub, LDAP, API keys)
- JWT access + refresh token issuance
- Roles and permissions
- OAuth2 federation
- **Device pairing** (long-lived per-device tokens that let daemons authenticate users offline)
- JWKS endpoint so any daemon can verify tokens without ever needing the private key

## Why standalone

Today, every daemon embeds the auth code (`digitorn.core.auth`) and signs its own
JWTs with a per-daemon secret. That couples identity to the daemon and prevents
the "one Digitorn account, many daemons" model.

This package extracts auth into a service that:
- Owns the private signing key — one trust root for the whole ecosystem
- Exposes JWKS so daemons (cloud OR self-hosted at the user's home) can verify
  tokens against the public key, fully offline once cached
- Issues device tokens on first pair so a self-hosted daemon can authenticate
  its user without internet for ~90 days

## Status

Work in progress. The original `digitorn.core.auth` keeps running unchanged in
the daemon. This package builds the new central service in parallel. Once it's
fully tested we cut over.

## Layout

```
src/digitorn_auth/
├── __init__.py
├── __main__.py            # CLI entrypoint: `digitorn-auth serve`
├── server.py              # FastAPI app + lifespan
├── config.py              # AuthSettings (env-driven)
├── database.py            # async engine + Base + session factory
├── models.py              # User, Role, UserRole, RefreshToken, UserOAuthToken, PairedDevice
├── jwt.py                 # JWT signing + verification (copied from core/auth)
├── service.py             # AuthService (copied)
├── middleware.py          # FastAPI dependency (require_logged_in_user) — server-side use
├── providers/
│   ├── base.py            # AuthProvider interface
│   ├── local.py           # email/password
│   ├── oauth2.py          # Google / Microsoft / GitHub
│   ├── ldap.py            # LDAP / AD bind
│   └── api_key.py         # M2M API keys
└── api/
    ├── auth.py            # /auth/login, /auth/register, /auth/refresh, /auth/logout
    ├── oauth.py           # /auth/oauth/{provider}/start, /auth/oauth/{provider}/callback
    ├── devices.py         # /auth/devices/pair, /auth/devices/{id}/revalidate, DELETE /auth/devices/{id}
    └── jwks.py            # /.well-known/openid-configuration, /.well-known/jwks.json
```

## Run

```bash
pip install -e .
DIGITORN_AUTH_DATABASE_URL=postgresql+asyncpg://... \
DIGITORN_AUTH_ISSUER=https://auth.digitorn.ai \
digitorn-auth serve --host 0.0.0.0 --port 8001
```

## Migration plan

1. Run this service alongside the daemon, point at the same Postgres
2. Daemon keeps signing JWTs (status quo)
3. Add JWKS verification to the daemon as a parallel path
4. Switch new logins to come from this service
5. Once stable, remove the daemon's signing code
