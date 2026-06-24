#!/usr/bin/env bash
#
# Build a Sudowork-flavored Dify offline image bundle.
#
# The bundle contains ONLY the two forked images (dify-api and dify-web)
# tagged sudowork/dify-{api,web}:sudo-${VERSION}-${SHORT_SHA}, the
# docker/ tree (compose + configs), and an install.sh that loads them.
#
# Upstream dependency images (nginx, postgres, redis, weaviate,
# plugin-daemon, sandbox, ssrf-proxy) are NOT in this bundle — ops
# saves and loads those separately on the customer host before
# install.sh runs.
#
# Two ways to source the dify-api / dify-web image tars:
#   (1) Default: read from local docker (assumes a prior local build
#       tagged sudowork/dify-{api,web}:sudo-${VERSION}-${SHORT_SHA}).
#   (2) CI flow: `--prebuilt-images-dir <dir>` — script reuses
#       `<dir>/dify-api.tar` and `<dir>/dify-web.tar` produced by the
#       GitHub Actions build step.
#
# Usage:
#   ./build-offline-bundle.sh [--version v] [--short-sha s] [--output dir]
#                             [--prebuilt-images-dir d] [--keep-temp]
#                             [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---- defaults -----------------------------------------------------------
VERSION="${VERSION:-1.14.2}"
# Production target is always Linux amd64. The .zip filename reflects it.
ARCH="amd64"
# Auto-detect short sha if we're in a git checkout; the workflow passes
# it explicitly via --short-sha so this fallback is only for local use.
if SHORT_SHA="$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD 2>/dev/null)"; then
    :
else
    SHORT_SHA=""
fi
OUTPUT_DIR="${OUTPUT_DIR:-$REPO_ROOT}"
KEEP_TEMP=0
PREBUILT_IMAGES_DIR=""

# ---- args ---------------------------------------------------------------
usage() {
    cat <<EOF
Build offline image bundle for Sudowork (Dify) deployment.

Usage:
  $(basename "$0") [options]

Options:
  --version <v>                 dify upstream version this build tracks
                                (default: $VERSION)
  --short-sha <s>               12-char git short sha (default: auto-detect)
  --output <dir>                output directory (default: repo root)
  --prebuilt-images-dir <dir>   directory containing pre-built
                                dify-api.tar and dify-web.tar (CI flow);
                                skips docker save and reuses the tars
  --keep-temp                   keep the staging dir for inspection
  --help                        this help

Examples:
  $(basename "$0")
  $(basename "$0") --version 1.14.2 --short-sha abc123def456
  $(basename "$0") --prebuilt-images-dir /tmp/prebuilt --output /tmp/
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2;;
        --short-sha) SHORT_SHA="$2"; shift 2;;
        --output) OUTPUT_DIR="$(cd "$2" && pwd)"; shift 2;;
        --prebuilt-images-dir) PREBUILT_IMAGES_DIR="$(cd "$2" && pwd)"; shift 2;;
        --keep-temp) KEEP_TEMP=1; shift;;
        --help|-h) usage; exit 0;;
        *) echo "unknown option: $1" >&2; usage; exit 1;;
    esac
done

if [[ -z "$SHORT_SHA" ]]; then
    echo "error: SHORT_SHA not set and not in a git checkout — pass --short-sha" >&2
    exit 1
fi

IMAGE_TAG="sudo-${VERSION}-${SHORT_SHA}"
SUDO_DIFY_API_IMAGE="sudowork/dify-api:${IMAGE_TAG}"
SUDO_DIFY_WEB_IMAGE="sudowork/dify-web:${IMAGE_TAG}"

BUNDLE_NAME="sudowork-dify-offline-${VERSION}-${SHORT_SHA}-${ARCH}"
STAGING_DIR="$(mktemp -d -t sudowork-bundle-XXXXXX)"
trap '[[ $KEEP_TEMP -eq 0 ]] && rm -rf "$STAGING_DIR"' EXIT

log() { printf "\033[36m[bundle]\033[0m %s\n" "$*" >&2; }
err() { printf "\033[31m[error]\033[0m %s\n" "$*" >&2; }

log "version       : $VERSION"
log "short sha     : $SHORT_SHA"
log "image tag     : $IMAGE_TAG"
log "bundle name   : $BUNDLE_NAME"

# ---- 1. save fork images -----------------------------------------------
log "step 1/3  populate images/ in staging dir"
mkdir -p "$STAGING_DIR/$BUNDLE_NAME/images"
MANIFEST="$STAGING_DIR/$BUNDLE_NAME/images/manifest.txt"
: > "$MANIFEST"

safe_name() { echo "$1" | tr '/:' '__'; }

write_manifest_line() {
    local tar="$1" img="$2" sha
    sha=$(sha256sum "$tar" | cut -d' ' -f1)
    printf "%s  %s  %s\n" "$sha" "$(basename "$tar")" "$img" >> "$MANIFEST"
}

if [[ -n "$PREBUILT_IMAGES_DIR" ]]; then
    log "  using prebuilt fork-image tars from $PREBUILT_IMAGES_DIR"
    for svc in api web; do
        src="$PREBUILT_IMAGES_DIR/dify-${svc}.tar"
        [[ -f "$src" ]] || { err "missing prebuilt tar: $src"; exit 1; }
        case "$svc" in
            api) ref="$SUDO_DIFY_API_IMAGE" ;;
            web) ref="$SUDO_DIFY_WEB_IMAGE" ;;
        esac
        dst="$STAGING_DIR/$BUNDLE_NAME/images/$(safe_name "$ref").tar"
        cp "$src" "$dst"
        write_manifest_line "$dst" "$ref"
    done
else
    log "  docker save fork images from local docker"
    for ref in "$SUDO_DIFY_API_IMAGE" "$SUDO_DIFY_WEB_IMAGE"; do
        if ! docker image inspect "$ref" >/dev/null 2>&1; then
            err "image not found in local docker: $ref"
            err "build it first or use --prebuilt-images-dir"
            exit 1
        fi
        base=$(safe_name "$ref")
        tar="$STAGING_DIR/$BUNDLE_NAME/images/${base}.tar"
        log "    saving $ref"
        docker save -o "$tar" "$ref"
        write_manifest_line "$tar" "$ref"
    done
fi

# ---- 2. copy docker/ tree ----------------------------------------------
log "step 2/3  copy docker/ tree (excluding runtime data, keeping plugin_packages)"

# rsync excludes mirror .gitignore entries that point at *runtime* state
# (db data, redis dumps, etc.) but importantly LEAVE `plugin_packages/`
# alone — that's the pre-seeded offline plugin bundle.
rsync -a \
    --exclude='volumes/app/storage/' \
    --exclude='volumes/certbot/' \
    --exclude='volumes/db/data/' \
    --exclude='volumes/redis/data/' \
    --exclude='volumes/weaviate/' \
    --exclude='volumes/qdrant/' \
    --exclude='volumes/etcd/' \
    --exclude='volumes/minio/' \
    --exclude='volumes/milvus/' \
    --exclude='volumes/chroma/' \
    --exclude='volumes/opensearch/data/' \
    --exclude='volumes/myscale/data/' \
    --exclude='volumes/myscale/log/' \
    --exclude='volumes/pgvector/data/' \
    --exclude='volumes/pgvecto_rs/data/' \
    --exclude='volumes/couchbase/' \
    --exclude='volumes/oceanbase/[^i]*' \
    --exclude='volumes/matrixone/' \
    --exclude='volumes/mysql/' \
    --exclude='volumes/seekdb/' \
    --exclude='volumes/iris/' \
    --exclude='volumes/plugin_daemon/cwd/' \
    --exclude='volumes/plugin_daemon/plugin/' \
    --exclude='volumes/plugin_daemon/assets/' \
    --exclude='.env' \
    --exclude='nginx/conf.d/default.conf' \
    --exclude='nginx/ssl/dify.*' \
    --exclude='middleware.env' \
    --exclude='docker-compose.override.yaml' \
    --exclude='build-offline-bundle.sh' \
    "$SCRIPT_DIR/" "$STAGING_DIR/$BUNDLE_NAME/docker/"

# ---- 3. emit install.sh + bundle README --------------------------------
log "step 3/3  emit install.sh + bundle README"

# Unquoted heredoc on the head — $SUDO_DIFY_* expands at bundle-build
# time so the customer host knows exactly which refs were bundled.
# Quoted body — customer-host shell vars stay literal.
cat > "$STAGING_DIR/$BUNDLE_NAME/install.sh" <<INSTALL_SH_HEAD
#!/usr/bin/env bash
# Sudowork (Dify) offline installer.
# Invoked on the customer host after unzipping the bundle.
#
# Prereq: ops must already have loaded the dependency images
# (nginx, postgres, redis, weaviate, plugin-daemon, sandbox, ssrf-proxy)
# into local docker before running this script.

BUNDLED_API_IMAGE="${SUDO_DIFY_API_IMAGE}"
BUNDLED_WEB_IMAGE="${SUDO_DIFY_WEB_IMAGE}"
INSTALL_SH_HEAD

cat >> "$STAGING_DIR/$BUNDLE_NAME/install.sh" <<'INSTALL_SH'

set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$BUNDLE_DIR/docker"
IMAGES_DIR="$BUNDLE_DIR/images"
UPGRADE=0
SKIP_DEP_CHECK=0

for arg in "$@"; do
    case "$arg" in
        --upgrade) UPGRADE=1 ;;
        --skip-dep-check) SKIP_DEP_CHECK=1 ;;
        --help|-h)
            cat <<EOF
Usage: $(basename "$0") [--upgrade] [--skip-dep-check]
  --upgrade          Skip secret generation and reuse existing docker/.env
  --skip-dep-check   Don't verify upstream dependency images are present
EOF
            exit 0 ;;
    esac
done

log() { printf "\033[36m[install]\033[0m %s\n" "$*"; }
err() { printf "\033[31m[error]\033[0m %s\n" "$*" >&2; }

# 0. prereqs
command -v docker >/dev/null || { err "docker not found, install Docker Engine first"; exit 1; }
docker compose version >/dev/null 2>&1 || { err "docker compose plugin missing (v2.20+)"; exit 1; }

# 1. load our fork images
log "loading docker images from $IMAGES_DIR ..."
for tar in "$IMAGES_DIR"/*.tar; do
    log "  $(basename "$tar")"
    docker load -i "$tar" >/dev/null
done

# 2. checksum verify
log "verifying image manifest..."
( cd "$IMAGES_DIR" && sha256sum -c <(awk '{print $1"  "$2}' manifest.txt) >/dev/null )

# 3. sanity-check that ops has loaded the dependency images
if [[ $SKIP_DEP_CHECK -eq 0 ]]; then
    log "checking required dependency images are present locally..."
    REQUIRED=(
        "langgenius/dify-plugin-daemon:0.6.1-local"
        "langgenius/dify-sandbox:0.2.15"
        "nginx:latest"
        "redis:6-alpine"
        "postgres:15-alpine"
        "semitechnologies/weaviate:1.27.0"
        "ubuntu/squid:latest"
    )
    MISSING=()
    for img in "${REQUIRED[@]}"; do
        if ! docker image inspect "$img" >/dev/null 2>&1; then
            MISSING+=("$img")
        fi
    done
    if [[ ${#MISSING[@]} -gt 0 ]]; then
        err "the following dependency images are NOT loaded locally:"
        for m in "${MISSING[@]}"; do err "  - $m"; done
        err "ask ops to docker load them, then rerun. (Or pass --skip-dep-check"
        err "if you know what you're doing.)"
        exit 1
    fi
    log "  all dependency images present."
fi

# 4. prepare .env
cd "$DOCKER_DIR"
if [[ ! -f .env ]]; then
    log "generating .env from template..."
    cp .env.example .env

    randset() {
        local key="$1" val
        val=$(openssl rand -base64 "${2:-48}" | tr -d '\n=' | head -c "${2:-48}")
        sed -i.bak "s#^${key}=.*#${key}=${val}#" .env && rm -f .env.bak
    }

    randset SECRET_KEY 42
    randset PLUGIN_DAEMON_KEY 48
    randset SUDOWORK_SSO_SECRET 48
    randset SUDOWORK_SYSTEM_SECRET 48
    randset SUDOWORK_SYSTEM_TOKEN 64
    randset DB_PASSWORD 24
    randset REDIS_PASSWORD 24
    randset WEAVIATE_API_KEY 32

    # Pin image refs to whatever this bundle actually contains.
    sed -i.bak "s#^SUDO_DIFY_API_IMAGE=.*#SUDO_DIFY_API_IMAGE=${BUNDLED_API_IMAGE}#" .env
    sed -i.bak "s#^SUDO_DIFY_WEB_IMAGE=.*#SUDO_DIFY_WEB_IMAGE=${BUNDLED_WEB_IMAGE}#" .env
    rm -f .env.bak

    log ".env created. Review CONSOLE_API_URL / APP_API_URL / NGINX_SERVER_NAME"
    log "  and any provider keys before this is exposed to real users."
else
    if [[ $UPGRADE -eq 1 ]]; then
        log "--upgrade specified, keeping existing .env"
        # Still re-pin image refs to the bundle being installed.
        sed -i.bak "s#^SUDO_DIFY_API_IMAGE=.*#SUDO_DIFY_API_IMAGE=${BUNDLED_API_IMAGE}#" .env
        sed -i.bak "s#^SUDO_DIFY_WEB_IMAGE=.*#SUDO_DIFY_WEB_IMAGE=${BUNDLED_WEB_IMAGE}#" .env
        rm -f .env.bak
    else
        log "existing .env detected, leaving alone"
    fi
fi

# 5. up
log "docker compose up -d ..."
docker compose up -d

# 6. wait
log "waiting for api to report healthy (timeout 180s)..."
for i in $(seq 1 60); do
    s=$(docker inspect -f '{{.State.Health.Status}}' docker-api-1 2>/dev/null || echo "missing")
    if [[ "$s" == "healthy" ]]; then
        log "  api healthy"
        break
    fi
    sleep 3
done

# 7. summary
log "containers:"
docker compose ps

cat <<EOF

------------------------------------------------------------------
Sudowork is now running. Quick sanity checks:
  curl -I http://localhost:6808/
  docker compose logs --tail 50 api
  docker compose logs --tail 50 nginx

Browse to: http://<this-host>:6808/
Sudowork-server should point at:
  DIFY_BASE_URL=http://<this-host>:6808/
  DIFY_SYSTEM_TOKEN=\$(grep SUDOWORK_SYSTEM_TOKEN docker/.env | cut -d= -f2)
------------------------------------------------------------------
EOF
INSTALL_SH
chmod +x "$STAGING_DIR/$BUNDLE_NAME/install.sh"

cat > "$STAGING_DIR/$BUNDLE_NAME/README.md" <<EOF
# Sudowork (Dify) Offline Bundle

Built: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
Architecture: ${ARCH}
Dify version: ${VERSION}
Commit: ${SHORT_SHA}
Image tag: ${IMAGE_TAG}

Bundled images:
  - $SUDO_DIFY_API_IMAGE
  - $SUDO_DIFY_WEB_IMAGE

## Prereq

Ops must \`docker load\` the dependency images on the customer host
BEFORE running install.sh:

  - langgenius/dify-plugin-daemon:0.6.1-local
  - langgenius/dify-sandbox:0.2.15
  - nginx:latest
  - redis:6-alpine
  - postgres:15-alpine
  - semitechnologies/weaviate:1.27.0
  - ubuntu/squid:latest

## Install

\`\`\`bash
sudo ./install.sh
\`\`\`

See \`docker/.env\` after install for the generated secrets. Review
CONSOLE_API_URL / APP_API_URL before exposing to users.

Full deployment manual: docs/OFFLINE-DEPLOYMENT.md (in the source repo).
EOF

# ---- pack as zip -------------------------------------------------------
log "packing → ${OUTPUT_DIR}/${BUNDLE_NAME}.zip"
( cd "$STAGING_DIR" && zip -r -q "${OUTPUT_DIR}/${BUNDLE_NAME}.zip" "$BUNDLE_NAME" )
size=$(du -sh "${OUTPUT_DIR}/${BUNDLE_NAME}.zip" | cut -f1)
log "done. bundle: ${OUTPUT_DIR}/${BUNDLE_NAME}.zip ($size)"

if [[ $KEEP_TEMP -eq 1 ]]; then
    log "staging dir kept at: $STAGING_DIR"
fi
