.PHONY: install test lint e2e

install:
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

e2e:
	./scripts/e2e.sh

build:
	./scripts/release.sh

publish:
	./scripts/release.sh --publish
