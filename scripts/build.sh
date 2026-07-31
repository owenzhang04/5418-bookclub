#!/usr/bin/env bash
# Render Build Command should be exactly: bash scripts/build.sh
set -euo pipefail

pip install -r requirements.txt

curl -sL \
  "https://github.com/benbjohnson/litestream/releases/download/v0.5.15/litestream-0.5.15-linux-x86_64.tar.gz" \
  | tar xz

chmod +x litestream scripts/start.sh
./litestream version
