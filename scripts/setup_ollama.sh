#!/usr/bin/env bash
set -euo pipefail

OLLAMA_BASE="${OLLAMA_HOST:-http://localhost:11434}"

echo "==> Checking Ollama availability at ${OLLAMA_BASE}..."
for i in $(seq 1 30); do
  if curl -sf "${OLLAMA_BASE}/api/tags" > /dev/null 2>&1; then
    echo "    Ollama is up."
    break
  fi
  echo "    Waiting for Ollama ($i/30)..."
  sleep 2
done

if ! curl -sf "${OLLAMA_BASE}/api/tags" > /dev/null 2>&1; then
  echo "ERROR: Ollama not reachable at ${OLLAMA_BASE}. Start it first: ollama serve"
  exit 1
fi

pull_model() {
  local model="$1"
  echo "==> Pulling ${model}..."
  ollama pull "${model}"
  echo "    Done: ${model}"
}

# Primary reasoning model (~18GB VRAM)
pull_model "qwen3:32b-q4_K_M"

# Fast sub-agent model (~5GB VRAM)
pull_model "qwen3:8b-q4_K_M"

# Embedding model
pull_model "nomic-embed-text:v1.5"

echo ""
echo "==> All models ready. Verifying..."
ollama list

echo ""
echo "==> Model setup complete."
echo "    Primary model:   qwen3:32b-q4_K_M  (used for CrashInvestigator + ReportWriter)"
echo "    Fast model:      qwen3:8b-q4_K_M   (used for domain agents)"
echo "    Embedding model: nomic-embed-text:v1.5 (used for Qdrant vector DB)"
