#!/usr/bin/env bash
# overrides.sh plan | build [--cluster NAME]
#
# Dev overrides: build local images for kubedock / ministack / floci /
# floci-az from whatever is in overrides/<component>, so mayfly can run
# unreleased changes. See OVERRIDES.md.
#
#   plan                   list the overrides found and how each will build
#   build [--cluster NAME] build them (and `k3d image import` into NAME),
#                          then write overrides/.env with the
#                          MAYFLY_<COMPONENT>_IMAGE exports mayfly reads
#
# Per component, overrides/<component> is either:
#   rootfs/ overlay  files copied over the pinned upstream image at the same
#                    paths (the old emulator/ whole-file overlay pattern)
#   source checkout  (or a symlink to one) built from source:
#                      kubedock        go.mod -> binary over the pinned image
#                      ministack       Dockerfile -> its own image build
#                      floci/floci-az  pom.xml -> maven package + JVM image
set -euo pipefail
cd "$(dirname "$0")/.."

DIR=${MAYFLY_OVERRIDES_DIR:-overrides}
COMPONENTS=(kubedock ministack floci floci-az)
CACHE="$DIR/.cache"

say() { echo "==> $*" >&2; }
die() { echo "error: $*" >&2; exit 1; }

env_var() { echo "MAYFLY_$(echo "$1" | tr 'a-z-' 'A-Z_')_IMAGE"; }

mode_of() {
  local d="$DIR/$1"
  [ -e "$d" ] || { echo none; return; }
  [ -d "$d/rootfs" ] && { echo overlay; return; }
  case "$1" in
    kubedock) [ -f "$d/go.mod" ] && { echo source; return; } ;;
    ministack) [ -f "$d/Dockerfile" ] && { echo source; return; } ;;
    floci | floci-az)
      [ -f "$d/pom.xml" ] && [ -f "$d/docker/Dockerfile.jvm-package" ] &&
        { echo source; return; } ;;
  esac
  echo unknown
}

check_entries() {
  [ -d "$DIR" ] || return 0
  local entry name
  for entry in "$DIR"/*; do
    [ -e "$entry" ] || continue
    name=$(basename "$entry")
    case " ${COMPONENTS[*]} " in
      *" $name "*) ;;
      *) die "$DIR/$name: unknown component (expected one of: ${COMPONENTS[*]})" ;;
    esac
    [ "$(mode_of "$name")" = unknown ] &&
      die "$DIR/$name: neither a rootfs/ overlay nor a recognized $name source checkout (see OVERRIDES.md)"
  done
  return 0
}

pinned() {
  uv run --quiet python -c "from mayfly.emulators import pinned_image; print(pinned_image('$1'))"
}

docker_arch() {
  case "$(docker info --format '{{.Architecture}}')" in
    aarch64 | arm64) echo arm64 ;;
    x86_64 | amd64) echo amd64 ;;
    *) die "unsupported docker architecture: $(docker info --format '{{.Architecture}}')" ;;
  esac
}

# prints the built image id
build_one() {
  local c="$1" mode="$2" src stage
  src=$(cd "$DIR/$c" && pwd -P)  # resolve symlinks: docker mounts need real paths
  mkdir -p "$CACHE"
  case "$mode" in
    overlay)
      printf 'FROM %s\nCOPY rootfs/ /\n' "$(pinned "$c")" |
        docker build -q -f - "$src"
      ;;
    source)
      case "$c" in
        kubedock)
          local gover arch
          gover=$(sed -n 's/^toolchain go//p' "$src/go.mod")
          [ -n "$gover" ] || gover=$(sed -n 's/^go //p' "$src/go.mod")
          arch=$(docker_arch)
          stage=$(mktemp -d)
          mkdir -p "$CACHE/go"
          say "$c: go build (golang:$gover-alpine, linux/$arch)"
          docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
            -e GOCACHE=/cache/build -e GOMODCACHE=/cache/mod \
            -e CGO_ENABLED=0 -e GOOS=linux -e GOARCH="$arch" \
            -v "$src":/src -v "$(cd "$CACHE/go" && pwd -P)":/cache -v "$stage":/out \
            -w /src "golang:$gover-alpine" go build -o /out/kubedock . >&2
          printf 'FROM %s\nCOPY kubedock /usr/local/bin/kubedock\n' "$(pinned "$c")" |
            docker build -q -f - "$stage"
          rm -rf "$stage"
          ;;
        ministack)
          say "$c: docker build (its own Dockerfile)"
          docker build -q "$src"
          ;;
        floci | floci-az)
          mkdir -p "$CACHE/m2"
          say "$c: maven package (maven:3.9-eclipse-temurin-25)"
          docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e MAVEN_CONFIG=/tmp/.m2 \
            -v "$src":/src -v "$(cd "$CACHE/m2" && pwd -P)":/m2 -w /src \
            maven:3.9-eclipse-temurin-25 \
            mvn -B -q -Dmaven.repo.local=/m2/repository package -DskipTests >&2
          docker build -q -f "$src/docker/Dockerfile.jvm-package" "$src"
          ;;
      esac
      ;;
  esac
}

cmd_plan() {
  check_entries
  local c mode
  for c in "${COMPONENTS[@]}"; do
    mode=$(mode_of "$c")
    [ "$mode" = none ] || echo "$c $mode $(env_var "$c")"
  done
}

cmd_build() {
  local cluster=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --cluster) cluster=${2:?--cluster needs a name}; shift 2 ;;
      *) die "unknown argument: $1" ;;
    esac
  done
  check_entries
  local c mode id tag envfile="$DIR/.env" built=0
  mkdir -p "$DIR"
  : > "$envfile"
  for c in "${COMPONENTS[@]}"; do
    mode=$(mode_of "$c")
    [ "$mode" = none ] && continue
    say "building $c override ($mode)"
    id=$(build_one "$c" "$mode" | tail -1)
    [ -n "$id" ] || die "$c: build produced no image"
    # content-addressed tag: a changed override always rolls out
    # (a reused tag would leave running pods on the old image)
    tag="mayfly-override/$c:dev-$(echo "${id#sha256:}" | cut -c1-12)"
    docker tag "$id" "$tag"
    if [ -n "$cluster" ]; then
      k3d image import "$tag" -c "$cluster" >/dev/null
      say "$c: $tag (imported into k3d cluster $cluster)"
    else
      say "$c: $tag"
    fi
    echo "export $(env_var "$c")=$tag" >> "$envfile"
    built=$((built + 1))
  done
  if [ "$built" -eq 0 ]; then
    say "no overrides in $DIR/"
  fi
  cat "$envfile"
}

case "${1:-}" in
  plan) shift; cmd_plan "$@" ;;
  build) shift; cmd_build "$@" ;;
  *) die "usage: overrides.sh plan | build [--cluster NAME]" ;;
esac
