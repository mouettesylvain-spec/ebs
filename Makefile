.PHONY: check check-all lint test-unit test-perf test-integration fmt cov nightly

UV ?= uv
RUN := $(UV) run --frozen --extra dev
PYTEST := $(RUN) pytest

# Every commit: lint, types, layering, unit tests with coverage and per-package gates.
check: lint test-unit test-perf

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	$(RUN) mypy --strict
	$(RUN) lint-imports

test-unit:
	$(PYTEST) -m "unit and not perf" -q --cov --cov-report=term --cov-report=json:coverage.json
	$(RUN) python scripts/coverage_gate.py coverage.json src

# Wall-clock budgets, measured without coverage tracing (it slows code down 2-3x).
test-perf:
	$(PYTEST) -m perf -q

# Every MR: also integration and e2e tests (Postgres via EBS_TEST_PG_URL or Docker).
check-all: check test-integration

test-integration:
	$(PYTEST) -m integration -q

fmt:
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

cov:
	$(PYTEST) -m "(unit or integration) and not perf" -q --cov --cov-report=term-missing \
		--cov-report=html --cov-report=json:coverage.json
	$(RUN) python scripts/coverage_gate.py coverage.json src

# Nightly: real grid, vendor tools and the long Hypothesis run.
nightly:
	HYPOTHESIS_PROFILE=nightly $(PYTEST) -m "grid or vendor or unit" -q
