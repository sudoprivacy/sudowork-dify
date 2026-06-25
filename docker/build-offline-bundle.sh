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
# Customer-facing top-level directory the .zip extracts into. We split this
# from BUNDLE_NAME so the released archive filename still carries the
# version+sha (useful for support tickets), but the on-disk path ops type
# all day stays short and stable: /data/deploy/dify/dify-deploy.
EXTRACT_DIR_NAME="dify-deploy"
STAGING_DIR="$(mktemp -d -t sudowork-bundle-XXXXXX)"
trap '[[ $KEEP_TEMP -eq 0 ]] && rm -rf "$STAGING_DIR"' EXIT

log() { printf "\033[36m[bundle]\033[0m %s\n" "$*" >&2; }
err() { printf "\033[31m[error]\033[0m %s\n" "$*" >&2; }

log "version       : $VERSION"
log "short sha     : $SHORT_SHA"
log "image tag     : $IMAGE_TAG"
log "bundle name   : $BUNDLE_NAME"
log "extract dir   : $EXTRACT_DIR_NAME"

# ---- 1. save fork images -----------------------------------------------
log "step 1/3  populate images/ in staging dir"
mkdir -p "$STAGING_DIR/$EXTRACT_DIR_NAME/images"
MANIFEST="$STAGING_DIR/$EXTRACT_DIR_NAME/images/manifest.txt"
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
        dst="$STAGING_DIR/$EXTRACT_DIR_NAME/images/$(safe_name "$ref").tar"
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
        tar="$STAGING_DIR/$EXTRACT_DIR_NAME/images/${base}.tar"
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
    "$SCRIPT_DIR/" "$STAGING_DIR/$EXTRACT_DIR_NAME/docker/"

# ---- 2.5. transform compose for production / older Docker Compose ------
# The dev docker-compose.yaml uses three patterns that break on Docker Compose
# v2.17 (commonly shipped with Docker 20.10 on customer hosts):
#   1. `env_file: [{path: X, required: false}, …]` — object form was added in
#      Compose v2.20. We collapse it to plain string form and drop entries
#      whose underlying file is not present in the bundle (the `required: false`
#      semantics).
#   2. `depends_on: { svc: { required: false, … } }` — also v2.20+. Strip
#      `required:` lines so the syntax is accepted.
#   3. Because (2) strips the "tolerate missing service" flag, also strip
#      depends_on entries of active services that point at services whose
#      profiles are NOT in the default-active set (weaviate / postgresql /
#      collaboration). Otherwise Compose v2.17 errors with
#      `no such service: db_mysql` etc.
#
# Plus: the dev compose bind-mounts sudowork patches into the api / worker
# containers for hot reload. The production image already has these files
# baked in, and the mount points (`../api/controllers/sudowork`) don't exist
# in the bundle layout. We strip those mounts so the bundled image's own
# code is what runs.
log "step 2.5/3  transform compose.yaml for production / older compose"
PROD_COMPOSE="$STAGING_DIR/$EXTRACT_DIR_NAME/docker/docker-compose.yaml"

python3 - "$PROD_COMPOSE" <<'PY'
import os, re, sys, yaml

path = sys.argv[1]
docker_dir = os.path.dirname(path)
with open(path) as f: src = f.read()

# (1) env_file: { path: X, required: B } -> "X", drop if file missing
def env_file_repl(m):
    indent, file_path = m.group(2), m.group(3)
    abs_path = os.path.join(docker_dir, file_path)
    if not os.path.exists(abs_path):
        return ""
    return f"{indent}- {file_path}\n"

env_pat = re.compile(
    r"(^([ \t]*)- path: (\S+)\s*\n\2  required: (?:true|false)\s*\n)",
    re.MULTILINE,
)
n_env = len(env_pat.findall(src))
src = env_pat.sub(env_file_repl, src)

# (2) strip any residual "required: <bool>" lines (depends_on)
req_pat = re.compile(r"^[ \t]*required:[ \t]+(?:true|false)[ \t]*\n", re.MULTILINE)
n_req = len(req_pat.findall(src))
src = req_pat.sub("", src)

# (extra) strip dev-only sudowork overlay volume mounts (image has them baked in)
dev_mount_patterns = [
    r"\n[ \t]*- \./sudowork-patches/feature_init\.py:[^\n]*",
    r"\n[ \t]*- \./sudowork-patches/ext_blueprints\.py:[^\n]*",
    r"\n[ \t]*- \./sudowork-patches/feature_service\.py:[^\n]*",
    r"\n[ \t]*- \.\./api/controllers/sudowork:[^\n]*",
    r"\n[ \t]*- \.\./api/services/sudowork:[^\n]*",
]
n_mounts = 0
for pat in dev_mount_patterns:
    n_mounts += len(re.findall(pat, src))
    src = re.sub(pat, "", src)

# (3) strip depends_on entries of active services that point at inactive-profile
#     services. We parse YAML to discover the profile graph but apply the edit
#     via line-level surgery so we preserve anchors / comments / formatting.
data = yaml.safe_load(src)
DEFAULT_ACTIVE = {"weaviate", "postgresql", "collaboration"}
def is_active(svc_name):
    profs = data["services"][svc_name].get("profiles", [])
    return (not profs) or any(p in DEFAULT_ACTIVE for p in profs)
active_services = {n for n in data["services"] if is_active(n)}
inactive_services = set(data["services"]) - active_services

# State machine: walk lines, track current top-level service + whether we're
# inside that service's depends_on block, remove inactive refs.
lines = src.split("\n")
out, i = [], 0
state = "scan"
service_indent = -1
current_service = None
in_depends_on = False
depends_indent = -1
n_deps = 0
while i < len(lines):
    line = lines[i]
    stripped = line.lstrip()
    indent = len(line) - len(stripped)

    if state == "scan":
        if indent == 2 and stripped.endswith(":") and not stripped.startswith("#"):
            name = stripped[:-1]
            if name in data["services"]:
                current_service = name
                service_indent = indent
                state = "in_service"
                in_depends_on = False
                out.append(line); i += 1; continue
        out.append(line); i += 1; continue

    # state == "in_service"
    if stripped and indent <= service_indent:
        state = "scan"; current_service = None; in_depends_on = False
        continue   # reprocess

    if not in_depends_on and stripped == "depends_on:" and current_service in active_services:
        in_depends_on = True; depends_indent = indent
        out.append(line); i += 1; continue

    if in_depends_on:
        if stripped and indent <= depends_indent:
            in_depends_on = False
            continue   # reprocess
        if stripped.startswith("- "):
            ref = stripped[2:].strip()
            if ref in inactive_services:
                n_deps += 1; i += 1; continue
        elif stripped.endswith(":"):
            ref = stripped[:-1]
            if ref in inactive_services:
                n_deps += 1; i += 1
                while i < len(lines):
                    ns = lines[i].lstrip(); ni = len(lines[i]) - len(ns)
                    if ns and ni <= indent: break
                    i += 1
                continue

    out.append(line); i += 1

src = "\n".join(out)
with open(path, "w") as f: f.write(src)
print(f"  env_file object-form -> string: {n_env}")
print(f"  required: lines stripped:       {n_req}")
print(f"  dev overlay mounts stripped:    {n_mounts}")
print(f"  inactive depends_on stripped:   {n_deps}")
PY

# ---- 3. emit install.sh + bundle README --------------------------------
log "step 3/3  emit install.sh + bundle README"

# Unquoted heredoc on the head — $SUDO_DIFY_* expands at bundle-build
# time so the customer host knows exactly which refs were bundled.
# Quoted body — customer-host shell vars stay literal.
cat > "$STAGING_DIR/$EXTRACT_DIR_NAME/install.sh" <<INSTALL_SH_HEAD
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

cat >> "$STAGING_DIR/$EXTRACT_DIR_NAME/install.sh" <<'INSTALL_SH'

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

    # Only randomize *application-layer* secrets — these gate API endpoints
    # exposed beyond the docker network.
    randset SECRET_KEY 42
    randset PLUGIN_DAEMON_KEY 48
    randset SUDOWORK_SSO_SECRET 48
    randset SUDOWORK_SYSTEM_SECRET 48
    randset SUDOWORK_SYSTEM_TOKEN 64
    # Middleware passwords (DB / Redis / Weaviate) stay at the .env.example
    # defaults. Reasons:
    #   - They never leave the compose internal network (no host port for
    #     postgres, redis, weaviate — only nginx/api are exposed).
    #   - Randomizing breaks derived values (CELERY_BROKER_URL embeds
    #     REDIS_PASSWORD literally; same for any vector store hooks). The
    #     old script randset REDIS_PASSWORD but forgot to update
    #     CELERY_BROKER_URL, which then 500'd every async indexing call
    #     with kombu auth errors.
    #   - Random middleware passwords also wedge re-install on existing
    #     volumes: postgres bakes the password into pgdata initdb, so a
    #     fresh random password on second install can't unlock old data.
    # Customers who insist on rotating these can edit .env post-install
    # and grep for all places the password appears (esp. CELERY_BROKER_URL,
    # vector-store env). For most deployments the defaults are fine.

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
chmod +x "$STAGING_DIR/$EXTRACT_DIR_NAME/install.sh"

cat > "$STAGING_DIR/$EXTRACT_DIR_NAME/README.md" <<EOF
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
( cd "$STAGING_DIR" && zip -r -q "${OUTPUT_DIR}/${BUNDLE_NAME}.zip" "$EXTRACT_DIR_NAME" )
size=$(du -sh "${OUTPUT_DIR}/${BUNDLE_NAME}.zip" | cut -f1)
log "done. bundle: ${OUTPUT_DIR}/${BUNDLE_NAME}.zip ($size)"

if [[ $KEEP_TEMP -eq 1 ]]; then
    log "staging dir kept at: $STAGING_DIR"
fi
