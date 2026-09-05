#!/bin/sh
# ---------------------------------------------------------------------------
# Runtime entrypoint. Reads secrets from environment variables (set in
# Render's dashboard, never committed to git) and injects them into a COPY
# of config.json before starting Freqtrade.
#
# IMPORTANT: Freqtrade has no CLI flag for api_server username/password/
# jwt_secret_key (verified against the current CLI reference — only db_url
# has a real flag, --db-url). Those three MUST come from config.json itself,
# so this script rewrites them into a runtime copy with sed rather than
# passing (nonexistent) flags.
#
# Required env vars (set these in Render's dashboard, NOT in this file):
#   DB_URL          — your Neon/Supabase Postgres connection string
#   API_USERNAME    — FreqUI / REST API login username
#   API_PASSWORD    — FreqUI / REST API login password
#   JWT_SECRET      — any long random string
#
# If DB_URL is unset, this falls back to the SQLite path already in
# config.json — that's fine for a quick test, but see RISK_AND_LIMITATIONS.md
# for why that means data loss on every Render restart.
#
# PORT: Render sets a $PORT env var at runtime (defaulting to 10000) and
# expects the service to bind there. config.json hardcodes listen_port:
# 8080 instead. Render's own docs say it can *usually* still detect and
# use a differently-bound port, but "usually" isn't "always" — so rather
# than leave the very first deploy dependent on that, this substitutes
# Render's $PORT into the runtime config copy whenever it's set, same
# placeholder-substitution pattern as the API credentials below. If
# $PORT is unset (e.g. running this outside Render), config.json's own
# 8080 is left untouched.
# ---------------------------------------------------------------------------

set -e

RUNTIME_CONFIG=/tmp/config.runtime.json
cp /freqtrade/user_data/config.json "$RUNTIME_CONFIG"

DB_URL_ARG=""
if [ -n "$DB_URL" ]; then
  echo "Using external database (persists across restarts)."
  DB_URL_ARG="--db-url $DB_URL"
else
  echo "WARNING: DB_URL not set — falling back to local SQLite."
  echo "Trade history WILL be lost on the next Render restart/sleep cycle."
  echo "See RISK_AND_LIMITATIONS.md, section 'Data persistence', to fix this."
fi

# Substitute API credentials into the runtime config copy. Only touches the
# placeholder strings, so an unset env var leaves the placeholder in place
# rather than blanking a working value — you'll notice quickly because the
# placeholder password won't match what you try to log in with.
if [ -n "$API_USERNAME" ]; then
  sed -i "s/\"username\": \"crown_admin\"/\"username\": \"$API_USERNAME\"/" "$RUNTIME_CONFIG"
fi
if [ -n "$API_PASSWORD" ]; then
  sed -i "s/\"password\": \"CHANGE_ME_BEFORE_DEPLOY\"/\"password\": \"$API_PASSWORD\"/" "$RUNTIME_CONFIG"
fi
if [ -n "$JWT_SECRET" ]; then
  sed -i "s/\"jwt_secret_key\": \"CHANGE_ME_BEFORE_DEPLOY_random_string_here\"/\"jwt_secret_key\": \"$JWT_SECRET\"/" "$RUNTIME_CONFIG"
fi

# Render's $PORT override (see comment block at the top of this file for
# why this exists). Only touches the specific "listen_port": 8080 line,
# so if you've already changed that value in config.json for some other
# reason, this substitution simply won't match and will silently do
# nothing rather than clobber your custom value.
if [ -n "$PORT" ]; then
  echo "Binding to Render's \$PORT=$PORT (overriding config.json's listen_port)."
  sed -i "s/\"listen_port\": 8080/\"listen_port\": $PORT/" "$RUNTIME_CONFIG"
else
  echo "No \$PORT set (not running on Render?) — using config.json's listen_port as-is."
fi

exec freqtrade trade \
  --config "$RUNTIME_CONFIG" \
  --strategy v12_Strategy \
  --logfile /freqtrade/user_data/logs/freqtrade.log \
  $DB_URL_ARG
