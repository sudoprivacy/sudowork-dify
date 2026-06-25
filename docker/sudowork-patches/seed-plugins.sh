#!/usr/bin/env bash
#
# Seed the Dify plugin_daemon volume with .difypkg files for every plugin
# listed in `default-plugins.json`. Run this once on the deployment host
# (it needs marketplace.dify.ai access); afterwards every tenant
# auto-provisioned via sudowork-server can install these plugins WITHOUT
# any further internet egress.
#
# Idempotent: if a .difypkg already lives in plugin_packages/ we skip the
# download. Safe to rerun whenever default-plugins.json is updated.
#
# Usage:
#   ./seed-plugins.sh                                # default paths
#   PLUGIN_DIR=/custom/path ./seed-plugins.sh        # override storage
#   MARKETPLACE=https://my-mirror ./seed-plugins.sh  # use a mirror
#
# After running, restart plugin_daemon so it indexes the new packages:
#   docker compose restart plugin_daemon

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIST_FILE="${LIST_FILE:-$SCRIPT_DIR/default-plugins.json}"
PLUGIN_DIR="${PLUGIN_DIR:-$SCRIPT_DIR/../volumes/plugin_daemon/plugin_packages/langgenius}"
MARKETPLACE="${MARKETPLACE:-https://marketplace.dify.ai}"

# Wheel cache for plugin Python dependencies. plugin_daemon runs `uv sync`
# per plugin at install time; on an airgapped customer host that fails
# unless every wheel is already on disk. We populate this dir alongside
# the .difypkg downloads (still requires PyPI access AT BUILD TIME, on
# the host running this script) and bind-mount it read-only into
# plugin_daemon at /app/storage/wheels.
WHEELS_DIR="${WHEELS_DIR:-$SCRIPT_DIR/../volumes/plugin_daemon/wheels}"
# Target Python ABI inside plugin_daemon container (python:3.12-slim-bookworm).
TARGET_PYTHON_VERSION="${TARGET_PYTHON_VERSION:-3.12}"
# Lowest manylinux tag that covers anything ≥ glibc 2.17 (the dify image
# is on glibc 2.36 so anything works; pick the broadest tag for max
# coverage on niche packages).
TARGET_PIP_PLATFORMS=(
    manylinux2014_x86_64
    manylinux_2_17_x86_64
    manylinux_2_28_x86_64
)

mkdir -p "$PLUGIN_DIR" "$WHEELS_DIR"

if [[ ! -f "$LIST_FILE" ]]; then
    echo "ERROR: plugin list not found: $LIST_FILE" >&2
    exit 1
fi

# Pull plugin specs from the JSON. We rely on python3 (always present in
# our deploy hosts) instead of jq to avoid an extra dependency.
mapfile -t SPECS < <(python3 -c "
import json, sys
with open('$LIST_FILE') as f:
    d = json.load(f)
for s in d.get('plugins', []):
    print(s)
")

total=${#SPECS[@]}
echo "→ seeding $total plugin(s) into $PLUGIN_DIR"
echo

ok=0
skip=0
fail=0
# Lockfile records the resolved <unique_identifier> for each spec so the
# tenant provisioning step doesn't have to hit marketplace again. Stored
# next to default-plugins.json, picked up by Python at provision time.
LOCK_FILE="$SCRIPT_DIR/default-plugins.lock.json"
declare -a LOCK_ENTRIES=()

for spec in "${SPECS[@]}"; do
    # spec format: langgenius/openai:0.4.2  → org=langgenius, id=openai, ver=0.4.2
    org="${spec%%/*}"
    rest="${spec#*/}"
    id="${rest%%:*}"
    ver="${rest#*:}"

    # Resolve the version doc to grab the full unique_identifier (with sha).
    resolved=$(python3 -c "
import json, sys, urllib.request, urllib.error
req = urllib.request.Request(
    '$MARKETPLACE/api/v1/plugins/$org/$id/$ver',
    headers={'User-Agent': 'curl/8.0'})
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode('utf-8'), strict=False)
    if d.get('code') != 0:
        sys.exit(2)
    print(d['data']['version']['unique_identifier'])
except Exception as e:
    sys.stderr.write(str(e) + '\n')
    sys.exit(3)
")

    if [[ -z "$resolved" ]]; then
        printf "  ✗ %-45s  resolve failed\n" "$spec"
        fail=$((fail + 1))
        continue
    fi

    # File name is the suffix after "<org>/" in the unique_identifier.
    # e.g. langgenius/openai:0.4.2@xxx  →  openai:0.4.2@xxx
    fname="${resolved#*/}"
    target="$PLUGIN_DIR/$fname"

    # Track the resolved identifier for the lockfile regardless of cache.
    LOCK_ENTRIES+=("$spec=$resolved")

    if [[ -f "$target" ]]; then
        printf "  ✓ %-45s  cached\n" "$spec"
        skip=$((skip + 1))
        continue
    fi

    # URL-encode the unique_identifier for the download endpoint.
    encoded=$(python3 -c "
import urllib.parse
print(urllib.parse.quote('$resolved', safe=''))")
    url="$MARKETPLACE/api/v1/plugins/download?unique_identifier=$encoded"

    if curl -sSfL "$url" -o "$target.tmp"; then
        mv "$target.tmp" "$target"
        size=$(stat -f%z "$target" 2>/dev/null || stat -c%s "$target")
        printf "  ✓ %-45s  downloaded (%s bytes)\n" "$spec" "$size"
        ok=$((ok + 1))
    else
        rm -f "$target.tmp"
        printf "  ✗ %-45s  download failed\n" "$spec"
        fail=$((fail + 1))
    fi
done

echo
echo "→ done. downloaded=$ok cached=$skip failed=$fail total=$total"

# Emit lockfile. tenant_provisioning_service.py reads this to know which
# unique_identifiers to feed into install_from_marketplace_pkg.
python3 - "$LOCK_FILE" "${LOCK_ENTRIES[@]}" <<'PY'
import json, sys
out_path = sys.argv[1]
pairs = sys.argv[2:]
resolved = {}
for pair in pairs:
    spec, uid = pair.split("=", 1)
    resolved[spec] = uid
with open(out_path, "w") as f:
    json.dump({
        "_comment": "Generated by seed-plugins.sh. Do not edit by hand — rerun seed-plugins.sh after changing default-plugins.json.",
        "resolved": resolved,
    }, f, indent=2, ensure_ascii=False)
PY
echo "→ wrote lockfile: $LOCK_FILE (${#LOCK_ENTRIES[@]} entries)"

if [[ $fail -gt 0 ]]; then
    echo "✗ some downloads failed; rerun later or remove the entries from default-plugins.json" >&2
    exit 1
fi

# -------------------------------------------------------------------
# Phase 2: cache Python wheels for every plugin's transitive deps.
#
# This is what closes the airgap: plugin_daemon's `uv sync` will look
# in $WHEELS_DIR (via UV_FIND_LINKS) before reaching for PyPI, and
# UV_NO_INDEX flips off the upstream lookup entirely. As long as we
# downloaded everything here at build time, the customer host can
# install plugins fully offline.
# -------------------------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
    echo "✗ uv not on PATH — wheel phase needs uv to resolve pyproject.toml" >&2
    echo "  install with: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi
if ! command -v pip3 >/dev/null 2>&1 && ! command -v pip >/dev/null 2>&1; then
    echo "✗ pip not on PATH — wheel phase needs pip download" >&2
    exit 1
fi
PIP=$(command -v pip3 || command -v pip)

echo
echo "→ caching wheels for $total plugin(s) → $WHEELS_DIR"
echo "  target: cpython-$TARGET_PYTHON_VERSION on Linux x86_64"
echo

# Build the platform flag block once.
PLATFORM_FLAGS=()
for p in "${TARGET_PIP_PLATFORMS[@]}"; do
    PLATFORM_FLAGS+=(--platform "$p")
done
# Also accept pure-Python wheels.
PLATFORM_FLAGS+=(--platform any)

TMP_WHEEL_WORK=$(mktemp -d -t sudowork-wheel-cache-XXXX)
trap 'rm -rf "$TMP_WHEEL_WORK"' EXIT

wheel_ok=0
wheel_fail=0
for spec in "${SPECS[@]}"; do
    # spec → resolved uid: scan LOCK_ENTRIES built in Phase 1.
    resolved=""
    for entry in "${LOCK_ENTRIES[@]}"; do
        if [[ "$entry" == "$spec="* ]]; then
            resolved="${entry#*=}"
            break
        fi
    done
    [[ -z "$resolved" ]] && { printf "  ⚠ %-45s  no lockfile entry, skip\n" "$spec"; continue; }
    fname="${resolved#*/}"
    pkg_path="$PLUGIN_DIR/$fname"
    [[ -f "$pkg_path" ]] || { printf "  ✗ %-45s  missing .difypkg, skip\n" "$spec"; wheel_fail=$((wheel_fail + 1)); continue; }

    # Extract pyproject.toml from the .difypkg (it's a plain zip).
    work="$TMP_WHEEL_WORK/${fname//[:@\/]/_}"
    mkdir -p "$work"
    if ! unzip -j -o -q "$pkg_path" pyproject.toml -d "$work" 2>/dev/null; then
        printf "  ⚠ %-45s  no pyproject.toml, skip\n" "$spec"
        continue
    fi

    # uv pip compile reads pyproject.toml directly and resolves the full
    # transitive dep set with exact pins.
    req="$work/req.txt"
    if ! uv pip compile "$work/pyproject.toml" \
            --output-file "$req" \
            --python-version "$TARGET_PYTHON_VERSION" \
            --quiet 2>"$work/compile.err"; then
        printf "  ✗ %-45s  uv pip compile failed (%s)\n" "$spec" "$(head -1 "$work/compile.err")"
        wheel_fail=$((wheel_fail + 1))
        continue
    fi

    # pip download with multi-platform tags. --no-deps because the
    # resolved requirements.txt already pins every transitive dep.
    if ! $PIP download \
            --requirement "$req" \
            --dest "$WHEELS_DIR" \
            --python-version "${TARGET_PYTHON_VERSION//./}" \
            "${PLATFORM_FLAGS[@]}" \
            --only-binary=:all: \
            --no-deps \
            >"$work/dl.log" 2>&1; then
        printf "  ✗ %-45s  pip download failed (see %s)\n" "$spec" "$work/dl.log"
        wheel_fail=$((wheel_fail + 1))
        continue
    fi
    printf "  ✓ %-45s  cached\n" "$spec"
    wheel_ok=$((wheel_ok + 1))
done

# Generate PEP 503 simple index from the flat wheel pool so uv can use
# UV_INDEX_URL=file:///app/storage/wheels/simple/ if find-links proves
# flaky. Cheap to build; just symlinks + tiny index.html shards.
python3 - "$WHEELS_DIR" <<'PY'
import os, re, sys
from collections import defaultdict
from pathlib import Path

wheels_dir = Path(sys.argv[1])
simple = wheels_dir / "simple"
simple.mkdir(exist_ok=True)

# Normalize package name per PEP 503: lowercase + collapse [-_.]
def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()

groups = defaultdict(list)
for whl in wheels_dir.glob("*.whl"):
    # filename: <name>-<version>-<py>-<abi>-<platform>.whl
    name = whl.name.split("-", 1)[0]
    groups[normalize(name)].append(whl.name)

for pkg, files in groups.items():
    pkg_dir = simple / pkg
    pkg_dir.mkdir(exist_ok=True)
    rows = "\n".join(f'    <a href="../../{f}">{f}</a><br/>' for f in sorted(files))
    (pkg_dir / "index.html").write_text(
        f"<!DOCTYPE html>\n<html><body>\n{rows}\n</body></html>\n", encoding="utf-8"
    )

# Top-level index.
top = "\n".join(f'  <a href="{p}/">{p}</a><br/>' for p in sorted(groups))
(simple / "index.html").write_text(
    f"<!DOCTYPE html>\n<html><body>\n{top}\n</body></html>\n", encoding="utf-8"
)

print(f"  built PEP-503 index for {len(groups)} distinct package(s)")
PY

echo
echo "→ wheel cache: ok=$wheel_ok failed=$wheel_fail"
echo "  size: $(du -sh "$WHEELS_DIR" 2>/dev/null | cut -f1)"

if [[ $wheel_fail -gt 0 ]]; then
    echo "✗ some wheel resolutions failed; check $TMP_WHEEL_WORK/*/dl.log" >&2
    # Keep tmp dir for inspection.
    trap - EXIT
    exit 1
fi

echo
echo "→ next: restart plugin_daemon so it picks up new packages + wheels"
echo "    docker compose -f $(realpath --relative-to="$PWD" "$SCRIPT_DIR/../docker-compose.yaml" 2>/dev/null || echo dify/docker/docker-compose.yaml) restart plugin_daemon"
