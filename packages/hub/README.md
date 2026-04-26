# Digitorn Hub

Remote service for publishing, searching, and installing Digitorn applications.

## Overview

A Digitorn application is a directory containing a `package.toml` (publishable
identity) and an `app.yaml` (runtime config). The hub stores those packages
as `.tar.gz` archives in object storage and serves metadata over HTTP, so any
daemon can `digitorn install <publisher>/<package_id>` against a hub.

## Architecture

| Layer        | Choice                                              |
|--------------|-----------------------------------------------------|
| Web          | FastAPI + Uvicorn                                   |
| DB           | PostgreSQL (async via `asyncpg` + SQLAlchemy 2.x)   |
| Storage      | S3-compatible object storage (Oracle OCI / MinIO)   |
| Search       | Hybrid: Postgres FTS (`tsvector`) + pgvector kNN    |
|              | with `intfloat/multilingual-e5-small` (fastembed)   |
| Auth         | JWT (HS256) + scoped API tokens                     |
| Migrations   | Alembic                                             |

## Layout

```
packages/hub/
├── src/digitorn_hub/
│   ├── main.py              FastAPI app + lifespan
│   ├── settings.py          Pydantic settings (env-driven)
│   ├── db.py                Async engine + sessionmaker
│   ├── models.py            SQLAlchemy ORM (users, publishers, packages,
│   │                        package_versions, package_tags, api_tokens,
│   │                        download_events)
│   ├── schemas.py           Pydantic IO models
│   ├── archive.py           Parse .tar.gz, extract package.toml
│   ├── auth/                JWT, password hashing, FastAPI deps
│   ├── storage/             Object storage abstraction (S3 backend)
│   ├── search/              Embeddings + hybrid ranking
│   └── routers/             health, auth, publishers, packages, search
├── alembic/                 Migrations
├── docker-compose.dev.yml   Postgres + MinIO for local dev
└── pyproject.toml
```

## Local development

```bash
# 1. Start Postgres + MinIO
docker-compose -f docker-compose.dev.yml up -d

# 2. Install + migrate
cd packages/hub
pip install -e .
alembic upgrade head

# 3. Run
uvicorn digitorn_hub.main:app --reload --port 8001
```

Browse OpenAPI at `http://127.0.0.1:8001/docs`.

## Production

- Provision Postgres with `vector` and `pg_trgm` extensions
- Provision S3-compatible bucket (Oracle OCI Object Storage works via
  S3-compatible API + presigned URLs)
- Set env vars listed in `.env.example`
- Run behind a reverse proxy (nginx/Caddy) with TLS
