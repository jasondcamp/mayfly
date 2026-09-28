.PHONY: install test lint e2e emulator-test overrides

install:
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

e2e:
	./scripts/e2e.sh

# emulator conformance on a disposable k3d cluster:
#   make emulator-test KIND=ministack|floci|floci-az
emulator-test:
	./scripts/emulator-test.sh $(KIND)

# build dev overrides from overrides/ (see OVERRIDES.md); CLUSTER=name also
# imports them into that k3d cluster. Then: source overrides/.env
overrides:
	./scripts/overrides.sh build $(if $(CLUSTER),--cluster $(CLUSTER))

build:
	./scripts/release.sh

publish:
	./scripts/release.sh --publish
