#!/usr/bin/env bash
# release.sh [patch|minor|major|--no-bump] [--publish]
# Build the mayfly-cli distribution (and optionally upload to PyPI).
#
# Every run bumps the PATCH version by default (pyproject.toml +
# src/mayfly/__init__.py — files only, committing/tagging is left to you).
# Pass minor|major to bump differently, or --no-bump to rebuild as-is.
# Then: lint + tests -> build sdist/wheel into dist/ -> install the wheel
# into a scratch venv and prove the `mayfly` command works.
# After a successful build you're asked whether to upload to PyPI via
# `uv publish` (auth: set UV_PUBLISH_TOKEN to a PyPI API token, or use
# trusted publishing). --publish skips the prompt (CI/non-interactive).
set -euo pipefail
cd "$(dirname "$0")/.."

PUBLISH=0
BUMP="patch"
for arg in "$@"; do
  case "$arg" in
    --publish) PUBLISH=1 ;;
    patch|minor|major) BUMP=$arg ;;
    --no-bump) BUMP="" ;;
    *) echo "usage: release.sh [patch|minor|major|--no-bump] [--publish]" >&2; exit 2 ;;
  esac
done

command -v uv >/dev/null || { echo "missing dependency: uv" >&2; exit 1; }

if [ -n "$BUMP" ]; then
  NEW_V=$(python3 - "$BUMP" <<'PY'
import re, sys
level = sys.argv[1]
text = open("pyproject.toml").read()
current = re.search(r'^version = "(\d+)\.(\d+)\.(\d+)"$', text, re.M)
major, minor, patch = map(int, current.groups())
if level == "major":
    major, minor, patch = major + 1, 0, 0
elif level == "minor":
    minor, patch = minor + 1, 0
else:
    patch += 1
new = f"{major}.{minor}.{patch}"
open("pyproject.toml", "w").write(
    text.replace(current.group(0), f'version = "{new}"', 1)
)
init = "src/mayfly/__init__.py"
itext = open(init).read()
open(init, "w").write(
    re.sub(r'^__version__ = ".*"$', f'__version__ = "{new}"', itext, 1, re.M)
)
print(new)
PY
)
  echo "==> bumped version ($BUMP) -> $NEW_V  (commit + tag when ready)"
fi

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

echo "==> build complete: dist/"
ls -1 dist

if [ "$PUBLISH" != "1" ]; then
  if [ -t 0 ]; then
    printf "Publish mayfly-cli %s to PyPI? [y/N] " "$PYPROJECT_V"
    read -r REPLY
    case "$REPLY" in
      [Yy]|[Yy][Ee][Ss]) PUBLISH=1 ;;
    esac
  else
    echo "non-interactive shell: not publishing (pass --publish to upload)"
  fi
fi

if [ "$PUBLISH" = "1" ]; then
  echo "==> publishing to PyPI"
  uv publish
  echo "==> published mayfly-cli $PYPROJECT_V  (pip install mayfly-cli)"
else
  echo "==> not published (rerun and answer y, or pass --publish)"
fi
