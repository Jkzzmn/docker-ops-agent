#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"
[[ -x .venv/bin/python ]] || { echo "Run ./setup.sh first"; exit 1; }
if [[ -f .env && "${DOCKER_OPS_ENV_LOADED:-0}" != "1" ]]; then
  export DOCKER_OPS_ENV_LOADED=1
  exec .venv/bin/python -c 'import os; from dotenv import load_dotenv; load_dotenv(".env"); os.execvpe("bash", ["bash", "./run_openai.sh"], os.environ)'
fi
[[ -n "${OPENAI_API_KEY:-}" ]] || { echo "OPENAI_API_KEY is required (usage may incur charges)."; exit 1; }
export LLM_BACKEND=openai
export OPENAI_FAST_MODEL="${OPENAI_FAST_MODEL:-gpt-4.1-mini}" OPENAI_QUALITY_MODEL="${OPENAI_QUALITY_MODEL:-gpt-4.1}"
export DOCKER_MODE="${DOCKER_MODE:-live}"
.venv/bin/uvicorn backend.server:app --host 127.0.0.1 --port 8100 & BACK_PID=$!
npm --prefix frontend run dev & FRONT_PID=$!
cleanup(){ kill "$BACK_PID" "$FRONT_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
echo "Docker Ops Agent (OpenAI) → http://127.0.0.1:5173"
while kill -0 "$BACK_PID" 2>/dev/null && kill -0 "$FRONT_PID" 2>/dev/null; do sleep 1; done
if kill -0 "$BACK_PID" 2>/dev/null; then wait "$FRONT_PID"; else wait "$BACK_PID"; fi

