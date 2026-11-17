.PHONY: install-dev run test lint fmt clean build stack stack-down e2e

# ── Development ───────────────────────────────────────────────────────────────

install-dev:
	@echo "Setting up development environment..."
	@if command -v uv >/dev/null 2>&1; then \
		echo "Using uv..."; \
		uv venv .venv 2>/dev/null || python3 -m venv .venv; \
		uv pip install --python .venv/bin/python -e ".[dev]"; \
	else \
		echo "Using pip..."; \
		python3 -m venv .venv; \
		.venv/bin/python -m ensurepip --upgrade 2>/dev/null || true; \
		.venv/bin/pip install --upgrade pip; \
		.venv/bin/pip install -e ".[dev]"; \
	fi
	@echo ""
	@echo "Done. Activate with: source .venv/bin/activate"

run:
	@if [ ! -f dev.env ]; then cp dev.env.example dev.env; fi
	DEV=true .venv/bin/python -m nanio_orchestrator

test:
	.venv/bin/python -m pytest -v

test-cov:          # fails below 100% of statements and branches
	.venv/bin/python -m pytest -q --cov --cov-report=term --cov-report=html

lint:
	.venv/bin/ruff check nanio_orchestrator/ tests/

fmt:
	.venv/bin/ruff format nanio_orchestrator/ tests/

clean:
	rm -rf .venv dev-data __pycache__ dist build *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true

# ── Full stack in Docker (real nginx, two nanio servers) ─────────────────────

stack:
	docker compose -f compose.dev.yaml up -d --build --wait
	@echo "panel http://localhost:8080 (API key: dev-key) — gateway http://localhost:8081"

stack-down:
	docker compose -f compose.dev.yaml down -v

e2e: stack
	docker compose -f compose.dev.yaml exec -T orchestrator python /e2e/run.py

# ── Packaging ─────────────────────────────────────────────────────────────────

build:
	@echo "Building wheel..."
	@if command -v uv >/dev/null 2>&1; then \
		uv build; \
	else \
		.venv/bin/pip install build; \
		.venv/bin/python -m build; \
	fi
	@echo "Wheel built in dist/"
