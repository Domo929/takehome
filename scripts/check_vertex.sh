#!/usr/bin/env bash
# Verify Vertex AI access end to end, before spending anything.
#
# Checks in order, stopping at the first failure with a specific remedy, because a
# generic "permission denied" from the SDK three layers down is a bad way to find out
# the API was never enabled.
set -uo pipefail

GCLOUD="${GCLOUD:-$HOME/google-cloud-sdk/bin/gcloud}"
PROJECT="${GOOGLE_CLOUD_PROJECT:-}"
LOCATION="${GOOGLE_CLOUD_LOCATION:-global}"
MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"

pass() { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; }
info() { printf '        %s\n' "$1"; }

echo
echo "Vertex AI access check"
echo

# 1. CLI present
if [ ! -x "$GCLOUD" ]; then
  fail "gcloud not found at $GCLOUD"
  info "install: https://cloud.google.com/sdk/docs/install"
  exit 1
fi
pass "gcloud $("$GCLOUD" --version 2>/dev/null | head -1 | awk '{print $NF}')"

# 2. Logged in
ACCOUNT=$("$GCLOUD" auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | head -1)
if [ -z "$ACCOUNT" ]; then
  fail "no active gcloud account"
  info "run: gcloud auth login"
  exit 1
fi
pass "signed in as $ACCOUNT"

# 3. Application Default Credentials. Distinct from the CLI login above: the SDK uses
#    ADC, and having one without the other is the most common way this fails.
ADC="$HOME/.config/gcloud/application_default_credentials.json"
if [ ! -f "$ADC" ]; then
  fail "no Application Default Credentials"
  info "run: gcloud auth application-default login"
  exit 1
fi
pass "ADC present"

# 4. A project to bill against
if [ -z "$PROJECT" ]; then
  PROJECT=$("$GCLOUD" config get-value project 2>/dev/null | grep -v '^(unset)$' || true)
fi
if [ -z "$PROJECT" ]; then
  fail "no project set"
  info "list what you can see:  gcloud projects list"
  info "then:                   export GOOGLE_CLOUD_PROJECT=<project-id>"
  echo
  info "projects visible to $ACCOUNT:"
  "$GCLOUD" projects list --format="table(projectId,name)" 2>/dev/null | sed 's/^/        /' || info "  (none readable)"
  exit 1
fi
pass "project: $PROJECT"

# 5. Vertex API enabled on that project
if "$GCLOUD" services list --enabled --project="$PROJECT" --format="value(config.name)" 2>/dev/null | grep -q aiplatform; then
  pass "aiplatform.googleapis.com enabled"
else
  fail "aiplatform.googleapis.com not enabled (or not visible to this account)"
  info "enable: gcloud services enable aiplatform.googleapis.com --project=$PROJECT"
  info "if that is denied, ask Evertune to enable it or grant roles/serviceusage.serviceUsageAdmin"
fi

# 6. Can we mint a token with cloud-platform scope?
TOKEN=$("$GCLOUD" auth application-default print-access-token 2>/dev/null || true)
if [ -z "$TOKEN" ]; then
  fail "could not mint an access token"
  info "run: gcloud auth application-default login"
  exit 1
fi
pass "access token minted (${#TOKEN} chars)"

# 7. One real generateContent call. Output is capped and thinking disabled, so this
#    costs a fraction of a cent.
if [ "$LOCATION" = "global" ]; then
  HOST="https://aiplatform.googleapis.com"
else
  HOST="https://${LOCATION}-aiplatform.googleapis.com"
fi
URL="$HOST/v1/projects/$PROJECT/locations/$LOCATION/publishers/google/models/${MODEL}:generateContent"

RESP=$(curl -s -w '\n%{http_code}' -X POST "$URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"Name one robot vacuum brand."}]}],"generationConfig":{"maxOutputTokens":24,"thinkingConfig":{"thinkingBudget":0}}}')
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')

if [ "$CODE" = "200" ]; then
  pass "generateContent 200 from $LOCATION"
  echo "$BODY" | python3 -c "
import json,sys
d=json.load(sys.stdin); u=d.get('usageMetadata',{})
print(f\"        model={d.get('modelVersion')} traffic={u.get('trafficType')}\")
print(f\"        tokens in={u.get('promptTokenCount')} out={u.get('candidatesTokenCount')} thinking={u.get('thoughtsTokenCount',0)}\")
" 2>/dev/null
  echo
  echo "  Vertex is reachable. Next:"
  echo "    export GOOGLE_CLOUD_PROJECT=$PROJECT GEMINI_BACKEND=vertex"
  echo "    make preflight"
else
  fail "generateContent returned HTTP $CODE"
  echo "$BODY" | head -c 500 | sed 's/^/        /'
  echo
  case "$CODE" in
    403) info "permission denied - the account likely needs roles/aiplatform.user on $PROJECT" ;;
    404) info "model or location not found - try GOOGLE_CLOUD_LOCATION=us-central1" ;;
    429) info "quota exhausted already - worth knowing before a load test" ;;
  esac
  exit 1
fi
