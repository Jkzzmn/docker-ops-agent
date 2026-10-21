#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"
[[ -x .venv/bin/python ]] || { echo "Run ./setup.sh first"; exit 1; }
if [[ -f .env && "${DOCKER_OPS_ENV_LOADED:-0}" != "1" ]]; then
  export DOCKER_OPS_ENV_LOADED=1
  exec .venv/bin/python -c 'import os; from dotenv import load_dotenv; load_dotenv(".env"); os.execvpe("bash", ["bash", "./run_ollama.sh"], os.environ)'
fi
command -v ollama >/dev/null || { echo "Ollama is not installed."; exit 1; }
curl -fsS "${OLLAMA_BASE_URL:-http://127.0.0.1:11434}/api/tags" >/dev/null || { echo "Start Ollama first: ollama serve"; exit 1; }
FAST="${OLLAMA_FAST_MODEL:-qwen3:4b-instruct}"; QUALITY="${OLLAMA_QUALITY_MODEL:-qwen3:8b}"
ollama list | awk 'NR>1 {print $1}' | grep -Fxq "$FAST" || { echo "Missing model: $FAST (ollama pull $FAST)"; exit 1; }
ollama list | awk 'NR>1 {print $1}' | grep -Fxq "$QUALITY" || { echo "Missing model: $QUALITY (ollama pull $QUALITY)"; exit 1; }
ROUTER="${OLLAMA_ROUTER_MODEL:-$FAST}"
if ! ollama list | awk 'NR>1 {print $1}' | grep -Fxq "$ROUTER"; then
  echo "Router model $ROUTER is not installed; using $FAST. No download performed."
  ROUTER="$FAST"
fi
export LLM_BACKEND=ollama OLLAMA_FAST_MODEL="$FAST" OLLAMA_QUALITY_MODEL="$QUALITY" OLLAMA_ROUTER_MODEL="$ROUTER"
export DOCKER_MODE="${DOCKER_MODE:-live}"
.venv/bin/uvicorn backend.server:app --host 127.0.0.1 --port 8100 & BACK_PID=$!
npm --prefix frontend run dev & FRONT_PID=$!
cleanup(){ kill "$BACK_PID" "$FRONT_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
echo "Docker Ops Agent (Ollama) → http://127.0.0.1:5173"
while kill -0 "$BACK_PID" 2>/dev/null && kill -0 "$FRONT_PID" 2>/dev/null; do sleep 1; done
if kill -0 "$BACK_PID" 2>/dev/null; then wait "$FRONT_PID"; else wait "$BACK_PID"; fi

