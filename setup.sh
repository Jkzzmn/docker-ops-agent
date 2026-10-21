#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 -m venv "$ROOT_DIR/.venv"
"$ROOT_DIR/.venv/bin/pip" install -r "$ROOT_DIR/requirements.lock"
npm --prefix "$ROOT_DIR/frontend" ci
echo "Setup complete. Start with ./run_ollama.sh or ./run_openai.sh"

