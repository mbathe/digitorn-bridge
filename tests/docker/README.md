# Deployment dry-run rig

A clean Ubuntu 24.04 container that mirrors what `scripts/bootstrap.sh`
sets up on the real Hetzner box, **minus systemd**. Lets us catch
missing OS packages and pyproject deps before paying for a real deploy.

## What this validates

- ✓ Every apt package needed for the daemon's wheels is present
- ✓ `pip install -e .` resolves cleanly (no missing system headers)
- ✓ The daemon imports + boots without crashing
- ✓ It talks to a sibling Redis container
- ✓ `/health` returns `status=ok` with `warming_up=false` within 90s

## What this does NOT validate

- ✗ The systemd unit file (no init system in Docker)
- ✗ Caddy + auto-TLS (needs a real public DNS)
- ✗ Real Neon Postgres (uses an in-container SQLite by default)

These are tested on the real Hetzner box at first deploy.

## Run it

From the repo root:

```bash
bash tests/docker/run_test.sh
```

Add `--keep` to leave the containers up after a successful run so you
can poke at the daemon manually:

```bash
bash tests/docker/run_test.sh --keep
# then in another terminal:
curl http://127.0.0.1:8000/health
docker compose -f tests/docker/docker-compose.test.yml logs -f daemon
docker compose -f tests/docker/docker-compose.test.yml down -v   # tear down
```

## Test against real Postgres (optional)

To validate the deploy hits Neon EU end-to-end, edit
`tests/docker/docker-compose.test.yml` and uncomment / fill the
`DIGITORN_DATABASE__URL` env var, then re-run.

## When the build fails

The most common failures and fixes:

| Symptom in the build log | Fix |
| ---------------------------- | ---- |
| `error: Microsoft Visual C++ 14.0 is required` | not applicable - Linux build, ignore |
| `Could not build wheels for X` | add the missing `-dev` apt package to the Dockerfile (and to `scripts/bootstrap.sh`) |
| `ModuleNotFoundError` after build succeeds | the dep is missing from `pyproject.toml`'s `[project.dependencies]` - add it |
| `RuntimeError: Database not initialized` | expected on first hit of an endpoint without DB; ignore as long as `/health` works |
| `SelectorEventLoop … NotImplementedError` | bug already fixed (server.py forces `WindowsProactorEventLoopPolicy`); should not happen on Linux |

## Tear-down

```bash
docker compose -f tests/docker/docker-compose.test.yml down -v
```

`-v` removes the `daemon_data` volume too. Run this if you want a
truly fresh slate next time.
