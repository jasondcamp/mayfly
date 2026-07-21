#!/usr/bin/env bash
# build.sh [url] [baseUrl]
# Build the docs as a plain static site for self-hosting (e.g. DreamHost).
#
#   ./build.sh                          # url https://docs.mayfly.sh, served at /
#   ./build.sh https://docs.example.com # your domain
#   ./build.sh https://example.com /docs/   # served under a subpath
#
# Output: build/ — upload its CONTENTS to your web root, e.g.:
#   scp -r build/* user@server.dreamhost.com:~/docs.mayfly.sh/
set -euo pipefail
cd "$(dirname "$0")"

URL=${1:-${DOCS_URL:-https://docs.mayfly.sh}}
BASE=${2:-${DOCS_BASE_URL:-/}}

command -v npm >/dev/null || { echo "missing dependency: npm (node 18+)" >&2; exit 1; }
[ -d node_modules ] || npm ci

echo "==> building for ${URL} (baseUrl ${BASE})"
DOCS_URL="$URL" DOCS_BASE_URL="$BASE" npm run build

echo
echo "==> static site in $(pwd)/build — upload its contents to your web root:"
echo "    scp -r build/* user@server.dreamhost.com:~/docs.mayfly.sh/"
