#!/usr/bin/env bash
# release.sh [--publish]
# Build the mayfly-cli distribution (and optionally upload to PyPI).
#
# Default run: lint + tests -> build sdist/wheel into dist/ -> install the
# wheel into a scratch venv and prove the `mayfly` command works.
# With --publish: additionally upload dist/* to PyPI via `uv publish`
# (auth: set UV_PUBLISH_TOKEN to a PyPI API token, or use trusted publishing).
set -euo pipefail
cd "$(dirname "$0")/.."

PUBLISH=0
[ "${1:-}" = "--publish" ] && PUBLISH=1

command -v uv >/dev/null || { echo "missing dependency: uv" >&2; exit 1; }

# version consistency: pyproject vs package
PYPROJECT_V=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
PACKAGE_V=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/mayfly/__init__.py | head -1)
if [ "$PYPROJECT_V" != "$PACKAGE_V" ]; then
  echo "version mismatch: pyproject.toml=$PYPROJECT_V src/mayfly/__init__.py=$PACKAGE_V" >&2
  exit 1
fi
echo "==> releasing mayfly-cli $PYPROJECT_V (CLI command: mayfly)"

echo "==> lint + tests"
uv run ruff check src tests
uv run pytest -q

echo "==> building sdist + wheel"
rm -rf dist
uv build

echo "==> smoke-testing the built wheel in a scratch venv"
SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT
uv venv -q "$SCRATCH/venv"
VIRTUAL_ENV="$SCRATCH/venv" uv pip install -q dist/mayfly_cli-"$PYPROJECT_V"-py3-none-any.whl
INSTALLED_V=$("$SCRATCH/venv/bin/mayfly" version)
if [ "$INSTALLED_V" != "$PYPROJECT_V" ]; then
  echo "wheel smoke test failed: mayfly version reported $INSTALLED_V" >&2
  exit 1
fi
"$SCRATCH/venv/bin/mayfly" --help >/dev/null
echo "    wheel installs; 'mayfly version' -> $INSTALLED_V"

if [ "$PUBLISH" = "1" ]; then
  echo "==> publishing to PyPI"
  uv publish
  echo "==> published mayfly-cli $PYPROJECT_V  (pip install mayfly-cli)"
else
  echo "==> build complete: dist/"
  ls -1 dist
  echo "run again with --publish to upload (requires UV_PUBLISH_TOKEN)"
fi
