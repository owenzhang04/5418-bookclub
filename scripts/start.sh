#!/usr/bin/env bash
# Render start entrypoint: restore from B2 (if any), then run the app under
# Litestream so SIGTERM triggers a final sync before the free tier spins down.
set -euo pipefail

: "${BOOKCLUB_DB_PATH:?BOOKCLUB_DB_PATH must be set}"
: "${PORT:?PORT must be set}"
: "${B2_KEY_ID:?B2_KEY_ID must be set in the Render Environment tab}"
: "${B2_APPLICATION_KEY:?B2_APPLICATION_KEY must be set in the Render Environment tab}"

if [[ ! -x ./litestream ]]; then
  echo "ERROR: ./litestream binary missing. Build Command must download it." >&2
  ls -la . >&2 || true
  exit 1
fi

mkdir -p "$(dirname "$BOOKCLUB_DB_PATH")"

echo "Litestream: restore (if replica exists) → $BOOKCLUB_DB_PATH"
./litestream restore -if-replica-exists -config litestream.yml "$BOOKCLUB_DB_PATH"

echo "Litestream: replicate + uvicorn on :$PORT"
exec ./litestream replicate -config litestream.yml \
  -exec "uvicorn app:app --host 0.0.0.0 --port ${PORT}"
