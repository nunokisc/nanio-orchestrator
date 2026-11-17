# Contributing

Contributions are welcome. This document covers the development workflow, code conventions,
and pull request process.

---

## Getting started

```bash
git clone https://github.com/nunokisc/nanio-orchestrator.git
cd nanio-orchestrator

# Set up dev environment
uv sync                     # with uv (recommended)
# or:
pip install -e ".[dev]"     # with pip

source .venv/bin/activate

# Build the web panel once (needs Node.js 20+); again after changing ui/
make ui

# Start the dev server
make run
```

The dev server runs at `http://localhost:8080` with API key `dev`. Without `make ui` the API
works and the panel's address explains how to build it.

### Working on the web panel

The panel is a React + TypeScript application in `ui/`, built by Vite into
`nanio_orchestrator/web/dist` (not in git; shipped inside the wheel and the Docker image).

```bash
make run            # the API, on :8080
make ui-dev         # the panel with hot reload on http://localhost:5173, /api proxied to :8080

cd ui
npm run lint        # eslint
npm run typecheck   # tsc --noEmit (strict)
npm test            # vitest + Testing Library
npm run build       # what `make ui` runs after `npm ci`
```

Conventions:

- No CSS framework and no component library: `src/styles.css` holds the design tokens (CSS
  variables, light and dark) and the components. Nothing is loaded from a CDN.
- `src/api.ts` is the only module that calls the API and holds its types. Server state goes
  through react-query (`src/queries.ts`); there is no global state library.
- Forms validate with the same rules as `nanio_orchestrator/models.py` (`src/lib/validation.ts`):
  a rule changed on one side is changed on the other.
- An error of the API is shown with its `detail` as written. Do not paraphrase it in the panel:
  improve the message in the API.
- Test the behaviour that is easy to get wrong (what is waiting for a reload, overrides,
  confirmations), not the markup.

---

## Running tests

```bash
make test           # pytest -v
make test-cov       # with coverage; fails below 100%
```

Coverage of statements and branches is **100% and enforced** in CI (`fail_under = 100` in
`pyproject.toml`) on Python 3.11 and 3.12. On 3.9 the whole suite must pass, but the threshold
is not applied: that version's tracer misreports `continue` lines and `async for` exits. A change that adds code adds the tests that exercise it.

- Test the behaviour, not the line: assert on the response and on the resulting state (database
  rows through the API, generated nginx files, sidecars, the S3 or rclone calls issued).
- Code that cannot be reached is deleted, not excluded. There is no `# pragma: no cover` in the
  package; adding one needs a comment that says why.
- Background loops and locks are created when the loop starts, never at import (see
  `test_no_asyncio_primitive_is_created_at_import`): on Python 3.9 they would be bound to the
  wrong event loop.

Tests use pytest-asyncio (`asyncio_mode = auto`) and httpx for API testing.
No external services are required — nginx and S3 are mocked.

Two more layers run against real software, in CI and locally:

```bash
make e2e            # Docker: real nginx, two nanio servers, rclone — pools, routes, migrations
NANIO_TEST_ENDPOINT=127.0.0.1:9000 pytest tests/test_s3client_integration.py   # S3 client vs a real nanio
```

A change to routing, to the generated nginx config or to the migration engine should be
checked with `make e2e`: mocks cannot tell you what nginx or nanio really do.

**Tests must pass before every commit.** CI enforces this, for the Python package and for the
panel (`ui` job: lint, type check, tests, build).

---

## Code style

```bash
make lint           # ruff check (errors only)
make fmt            # ruff format (auto-fix)
```

Rules: `ruff` with `E`, `F`, `I`, `W`, `UP`, `B`, `SIM`, `RUF` — the same set nanio uses — on the
package and on the tests. Line length 120. Target: Python 3.9+, which is why `Optional[X]` is
kept instead of `X | None` (Pydantic evaluates annotations at runtime).

---

## Commit conventions

Plain English, imperative mood:
```
fix: nil check in _enrich_vhost when ip_rule_ips_json is absent
feat: add per-pool bucket status endpoint
chore: bump ruff to 0.5
```

Prefixes: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`.

---

## Adding a new feature checklist

- [ ] Add/update Pydantic models in `models.py`
- [ ] Add DB column with `ALTER TABLE` migration in `db.py` (both `_run_migrations_async` and `init_db_sync`)
- [ ] Implement API endpoint in `api/`
- [ ] Update `sidecar.py` if the new field must survive DB loss (add to `write_vhost_sidecar` or `write_pool_sidecar`)
- [ ] Update `rebuild.py` to restore the field from sidecar on rebuild
- [ ] Update nginx template in `nginx/templates/` if the field affects config generation
- [ ] Update `nginx/generator.py` to pass new template variables
- [ ] Add tests in `tests/`
- [ ] Show it in the web panel (`ui/src/api.ts` for the types, the screen, its validation)
- [ ] Update `docs/` and `CLAUDE.md` if needed

---

## Pull request process

1. Fork the repo and create a branch: `git checkout -b feat/my-feature`
2. Make changes following the checklist above
3. Run `make test` and `make lint` — both must pass; for a change in `ui/`, also
   `npm run lint && npm run typecheck && npm test` there
4. Open a PR against `main`
5. CI will run tests on Python 3.9, 3.11, and 3.12
6. At least one review required before merge

---

## Project structure quick reference

See [CLAUDE.md](../CLAUDE.md) for the full module-by-module description and golden rules.

---

## Releasing

Releases are published to PyPI automatically via GitHub Actions when a tag is pushed:

```bash
# Bump version in pyproject.toml and nanio_orchestrator/__init__.py
# Then:
git tag v0.2.0
git push origin v0.2.0
```

The CI publish workflow builds the web panel, then the wheel and sdist (which contain it), and
uploads to PyPI using a trusted publisher (OIDC — no stored API token needed). Building a wheel
by hand is `make build`, which runs `make ui` first.

See [`.github/workflows/publish.yml`](../.github/workflows/publish.yml).
