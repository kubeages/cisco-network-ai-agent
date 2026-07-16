#!/usr/bin/env bash
# Deploy gbaia (NAIA) + ND-MCP + Intersight-MCP to an OpenShift cluster.
#
#   Usage:  ./deploy.sh [path/to/.env]   (default: ./.env)
#
# Pre-reqs:
#   - `oc` logged in (`oc whoami`).
#   - .env present (copy from .env.example and fill in).
#   - Intersight PEM at $INTERSIGHT_PRIVATE_KEY_FILE if you want Intersight ingestion.
#
# Idempotent: safe to re-run. Existing secrets/manifests are updated in place;
# builds restart unless --skip-builds is passed.

set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="${1:-.env}"
SKIP_BUILDS=0
[[ "${2:-}" == "--skip-builds" ]] && SKIP_BUILDS=1

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Copy .env.example to .env first." >&2
  exit 1
fi
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

NAMESPACE="${NAMESPACE:-gbaia}"

say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }

# --- 1. namespace ---
say "Ensure namespace $NAMESPACE"
oc get ns "$NAMESPACE" >/dev/null 2>&1 || oc create namespace "$NAMESPACE"
ok "namespace ready"

# --- 2. secrets ---
say "Create/update Secrets"

# 2a. gbaia-secrets (APIC, ND, OpenAI, Neo4j, etc.)
oc -n "$NAMESPACE" create secret generic gbaia-secrets \
    --dry-run=client -o yaml \
    --from-literal=apic-url="${APIC_URL:-}" \
    --from-literal=apic-user="${APIC_USER:-}" \
    --from-literal=apic-password="${APIC_PASSWORD:-}" \
    --from-literal=nd-url="${ND_URL:-}" \
    --from-literal=nd-user="${ND_USER:-}" \
    --from-literal=nd-password="${ND_PASSWORD:-}" \
    --from-literal=neo4j-password="${NEO4J_PASSWORD:-changeme}" \
    --from-literal=neo4j-auth="neo4j/${NEO4J_PASSWORD:-changeme}" \
    --from-literal=openai-api-key="${OPENAI_API_KEY:-}" \
    --from-literal=local-llm-token="${LOCAL_LLM_TOKEN:-}" \
    --from-literal=ai-defense-api-key="${AI_DEFENSE_API_KEY:-}" \
    --from-literal=model-proxy-api-key="${MODEL_PROXY_API_KEY:-}" \
    --from-literal=splunk-access-token="${SPLUNK_ACCESS_TOKEN:-}" \
  | oc apply -f -
ok "gbaia-secrets"

# 2b. gbaia-nd-mcp-secrets — derive DATABASE_URL, mint Fernet key if missing
if [[ -z "${ENCRYPTION_KEY:-}" ]]; then
  ENCRYPTION_KEY="$(python3 -c 'import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
  warn "ENCRYPTION_KEY was empty — generated a fresh Fernet key (save it if you need to decrypt existing data)"
fi
DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
oc -n "$NAMESPACE" create secret generic gbaia-nd-mcp-secrets \
    --dry-run=client -o yaml \
    --from-literal=POSTGRES_DB="$POSTGRES_DB" \
    --from-literal=POSTGRES_USER="$POSTGRES_USER" \
    --from-literal=POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
    --from-literal=POSTGRES_HOST="$POSTGRES_HOST" \
    --from-literal=POSTGRES_PORT="$POSTGRES_PORT" \
    --from-literal=DATABASE_URL="$DATABASE_URL" \
    --from-literal=MCP_API_TOKEN="${MCP_API_TOKEN:-}" \
    --from-literal=ENCRYPTION_KEY="$ENCRYPTION_KEY" \
    --from-literal=ND_URL="${ND_URL:-}" \
    --from-literal=ND_USER="${ND_USER:-}" \
    --from-literal=ND_PASSWORD="${ND_PASSWORD:-}" \
  | oc apply -f -
ok "gbaia-nd-mcp-secrets"

# 2c. gbaia-intersight-secrets — both URL variants (Python SDK without /api/v1, MCP with)
INTERSIGHT_REGION_HOST="${INTERSIGHT_REGION_HOST:-eu-central-1.intersight.com}"
INTERSIGHT_BASE_URL_SDK="https://${INTERSIGHT_REGION_HOST}"
INTERSIGHT_BASE_URL_MCP="https://${INTERSIGHT_REGION_HOST}/api/v1"
PEM_FILE="${INTERSIGHT_PRIVATE_KEY_FILE:-./intersight-private-key.pem}"
if [[ -n "${INTERSIGHT_API_KEY_ID:-}" && -f "$PEM_FILE" ]]; then
  oc -n "$NAMESPACE" create secret generic gbaia-intersight-secrets \
      --dry-run=client -o yaml \
      --from-literal=api-key-id="$INTERSIGHT_API_KEY_ID" \
      --from-file=private-key="$PEM_FILE" \
      --from-literal=base-url="$INTERSIGHT_BASE_URL_SDK" \
      --from-literal=base-url-mcp="$INTERSIGHT_BASE_URL_MCP" \
    | oc apply -f -
  ok "gbaia-intersight-secrets"
else
  warn "Intersight key or PEM missing — skipping gbaia-intersight-secrets (intersight-mcp + ingestor's Intersight phase will be disabled)"
  oc -n "$NAMESPACE" create secret generic gbaia-intersight-secrets \
      --dry-run=client -o yaml \
      --from-literal=api-key-id="" \
      --from-literal=private-key="" \
      --from-literal=base-url="https://intersight.com" \
      --from-literal=base-url-mcp="https://intersight.com/api/v1" \
    | oc apply -f -
fi

# --- 3. apply all manifests in alphabetical (== dependency) order ---
say "Apply manifests"
oc -n "$NAMESPACE" apply -R -f .
ok "manifests applied"

# --- 4. override cluster-specific env knobs ---
say "Wire cluster-specific env"
if [[ -n "${LOCAL_LLM_URL:-}" ]]; then
  oc -n "$NAMESPACE" set env deploy/gbaia-backend \
      LOCAL_LLM_URL="$LOCAL_LLM_URL" \
      LOCAL_LLM_MODEL="${LOCAL_LLM_MODEL:-qwen3.6-27b}" >/dev/null
  ok "backend LOCAL_LLM_URL → $LOCAL_LLM_URL"
fi
if [[ -n "${BACKEND_EXTERNAL_URL:-}" ]]; then
  oc -n "$NAMESPACE" set env deploy/gbaia-frontend \
      BACKEND_EXTERNAL_URL="$BACKEND_EXTERNAL_URL" >/dev/null
  ok "frontend BACKEND_EXTERNAL_URL → $BACKEND_EXTERNAL_URL"
fi

# Optional source-URI overrides on BuildConfigs (only patch if non-default)
if [[ -n "${BACKEND_GIT_URI:-}" ]]; then
  for bc in gbaia-backend gbaia-frontend gbaia-ingestor; do
    oc -n "$NAMESPACE" patch bc/$bc --type=json \
       -p="[{\"op\":\"replace\",\"path\":\"/spec/source/git/uri\",\"value\":\"$BACKEND_GIT_URI\"}]" >/dev/null 2>&1 || true
  done
fi
if [[ -n "${ND_MCP_GIT_URI:-}" ]]; then
  oc -n "$NAMESPACE" patch bc/gbaia-nd-mcp-server --type=json \
     -p="[{\"op\":\"replace\",\"path\":\"/spec/source/git/uri\",\"value\":\"$ND_MCP_GIT_URI\"}]" >/dev/null 2>&1 || true
fi
if [[ -n "${INTERSIGHT_MCP_GIT_URI:-}" ]]; then
  oc -n "$NAMESPACE" patch bc/gbaia-intersight-mcp --type=json \
     -p="[{\"op\":\"replace\",\"path\":\"/spec/source/git/uri\",\"value\":\"$INTERSIGHT_MCP_GIT_URI\"}]" >/dev/null 2>&1 || true
fi

# --- 5. kick builds (optional) ---
if [[ "$SKIP_BUILDS" == "0" ]]; then
  # 5a. Base images first — expensive dep layers. On a fresh cluster these
  # must exist before their app builds can succeed (the app BCs use
  # `strategy.from: ImageStreamTag <svc>-base:latest` — no base = no build).
  # After the base is built once, it's rarely rebuilt (only when
  # requirements.txt changes) — see the top of README.
  say "Seed base images (one-time; rebuilt manually when requirements change)"
  for bc in gbaia-backend-base gbaia-frontend-base gbaia-ingestor-base; do
    if oc -n "$NAMESPACE" get bc/$bc >/dev/null 2>&1; then
      # Skip if the base was already built successfully (idempotent re-runs)
      last_phase=$(oc -n "$NAMESPACE" get bc/$bc -o jsonpath='{.status.lastVersion}' 2>/dev/null)
      if [[ -n "$last_phase" && "$last_phase" != "0" ]]; then
        ok "$bc already built (v$last_phase) — skipping"
        continue
      fi
      oc -n "$NAMESPACE" start-build "$bc" --follow=false >/dev/null && ok "started $bc (this can take ~15 min the first time)" || warn "could not start $bc"
    fi
  done

  # 5b. App-side builds — these are fast (~30s) once the base image exists.
  # ImageChange triggers will also auto-rebuild the app when the base is refreshed.
  say "Trigger app builds"
  for bc in gbaia-backend gbaia-frontend gbaia-ingestor gbaia-nd-mcp-server gbaia-intersight-mcp; do
    if oc -n "$NAMESPACE" get bc/$bc >/dev/null 2>&1; then
      oc -n "$NAMESPACE" start-build "$bc" --follow=false >/dev/null && ok "started $bc" || warn "could not start $bc"
    fi
  done
else
  warn "--skip-builds set; skipping start-build calls"
fi

say "Deploy complete"
cat <<EOF
Next steps:
  1. Watch the builds: oc -n $NAMESPACE get builds -w
  2. After builds finish, deployments roll automatically (image triggers wired).
  3. Get the routes: oc -n $NAMESPACE get route
  4. Open the gbaia-frontend route in a browser.

To rotate ND password (it lives in nd-mcp's postgres, not just the secret):
  oc -n $NAMESPACE exec deploy/gbaia-backend -- python3 -c "
import httpx
httpx.put('http://gbaia-nd-mcp-server:7443/api/clusters/default',
    json={'password': 'NEW_PW'}, timeout=15)
"
  Then patch gbaia-secrets.nd-password and restart gbaia-apic-ingestor.
EOF
