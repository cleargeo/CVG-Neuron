#!/bin/bash
# =============================================================================
# CVG Neuron — Container Entrypoint (v1.0.0 — Hive Edition)
# (c) Clearview Geographic LLC — Proprietary
#
# Startup sequence:
#   1. Wait for Ollama to be ready at primary node
#   2. Pull base model (llama3.1:8b) if not present
#   3. Create/update the cvg-neuron model from Modelfile (bakes CVG identity)
#   4. Launch the FastAPI intelligence server on port 8095
#   5. Hive node probing happens in-process after startup
# =============================================================================

set -euo pipefail

# Accept OLLAMA_HOST (current docker-compose var) or legacy OLLAMA_URL
OLLAMA_URL="${OLLAMA_HOST:-${OLLAMA_URL:-http://host.docker.internal:11434}}"
MODEL_NAME="${OLLAMA_MODEL:-llama3.1:8b}"
BASE_MODEL="${OLLAMA_MODEL:-llama3.1:8b}"
MODELFILE="/app/Modelfile"
DATA_DIR="${NEURON_DATA_DIR:-/app/data}"
MAX_WAIT=120
INTERVAL=5

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║         CVG Neuron — Artificial Intelligence Engine          ║"
echo "║         Clearview Geographic LLC  —  Port 8095              ║"
echo "║         Hive-0 Cluster: Queens + Forges + Edge Nodes        ║"
echo "║         NOT a wrapper. NOT a model hub. An intelligence.    ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "[neuron] OLLAMA_URL:  ${OLLAMA_URL}"
echo "[neuron] MODEL:       ${MODEL_NAME}"
echo "[neuron] BASE MODEL:  ${BASE_MODEL}"
echo "[neuron] DATA_DIR:    ${DATA_DIR}"
echo ""

# ─── Ensure data directory exists ─────────────────────────────────────────────
mkdir -p "${DATA_DIR}"

# ─── Wait for Ollama ─────────────────────────────────────────────────────────
echo "[neuron] ── Waiting for Ollama at ${OLLAMA_URL} ..."
elapsed=0
while true; do
    if curl -sf "${OLLAMA_URL}/api/tags" > /dev/null 2>&1; then
        echo "[neuron] ✓ Ollama is available at ${OLLAMA_URL}"
        break
    fi
    if [ "$elapsed" -ge "$MAX_WAIT" ]; then
        echo "[neuron] ⚠ Ollama not reachable after ${MAX_WAIT}s"
        echo "[neuron]   Starting in degraded mode (hive will be probed at runtime)"
        break
    fi
    echo "[neuron]   ... waiting (${elapsed}s / ${MAX_WAIT}s)"
    sleep "$INTERVAL"
    elapsed=$((elapsed + INTERVAL))
done

# ─── Pull base model if needed ────────────────────────────────────────────────
if curl -sf "${OLLAMA_URL}/api/tags" > /dev/null 2>&1; then

    BASE_PRESENT=$(curl -sf "${OLLAMA_URL}/api/tags" | python3 -c "
import sys, json
data = json.load(sys.stdin)
models = [m['name'] for m in data.get('models', [])]
base = '${BASE_MODEL}'.split(':')[0]
print('yes' if any(m.startswith(base) for m in models) else 'no')
" 2>/dev/null || echo "no")

    if [ "$BASE_PRESENT" = "no" ]; then
        echo "[neuron] ── Pulling base model ${BASE_MODEL} from Ollama registry..."
        curl -s -X POST "${OLLAMA_URL}/api/pull" \
            -H "Content-Type: application/json" \
            -d "{\"name\":\"${BASE_MODEL}\",\"stream\":false}" > /dev/null
        echo "[neuron] ✓ Base model ${BASE_MODEL} pulled"
    else
        echo "[neuron] ✓ Base model ${BASE_MODEL} already available"
    fi

    # ─── Create/update cvg-neuron model ───────────────────────────────────────

    if [ -f "$MODELFILE" ]; then
        echo "[neuron] ── Registering ${MODEL_NAME} model with Ollama..."

        # Check if evolved Modelfile exists (from previous run — smarter version)
        EVOLVED_MODELFILE="${DATA_DIR}/Modelfile.evolved"
        if [ -f "$EVOLVED_MODELFILE" ]; then
            echo "[neuron]   Found evolved Modelfile at ${EVOLVED_MODELFILE} — using evolved version"
            ACTUAL_MODELFILE="$EVOLVED_MODELFILE"
        else
            echo "[neuron]   Using base Modelfile at ${MODELFILE}"
            ACTUAL_MODELFILE="$MODELFILE"
        fi

        python3 - <<PYEOF
import os, json, sys, urllib.request, urllib.error

ollama_url    = os.environ.get("OLLAMA_HOST", os.environ.get("OLLAMA_URL", "http://10.10.10.200:11434"))
model_name    = os.environ.get("OLLAMA_MODEL", "cvg-neuron")
modelfile_path = "${ACTUAL_MODELFILE}"

try:
    with open(modelfile_path, "r", encoding="utf-8") as f:
        modelfile_content = f.read()
except FileNotFoundError:
    print(f"[neuron] ✗ Modelfile not found at {modelfile_path}")
    sys.exit(0)

payload = json.dumps({
    "name":      model_name,
    "modelfile": modelfile_content,
    "stream":    False,
}).encode()

req = urllib.request.Request(
    f"{ollama_url}/api/create",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read().decode())
        status = result.get("status", "unknown")
        print(f"[neuron] ✓ {model_name} model created/updated — status: {status}")
        print(f"[neuron]   This model IS CVG Neuron's intelligence identity")
        print(f"[neuron]   Run 'ollama run {model_name}' to interact directly")
except urllib.error.URLError as e:
    print(f"[neuron] ✗ Failed to create {model_name} model: {e}")
    print(f"[neuron]   Will fall back to {os.environ.get('OLLAMA_ALT_MODEL', 'llama3.1:8b')}")
PYEOF

    else
        echo "[neuron] ✗ Modelfile not found at ${MODELFILE} — skipping identity registration"
    fi

    # ─── Hive: quick probe of DFORGE-100 for secondary Ollama ─────────────────
    echo "[neuron] ── Probing Hive-0 secondary nodes..."
    python3 - <<PYEOF
import urllib.request, json, sys

# Quick check for DFORGE-100 (developer forge — most likely to have extra Ollama capacity)
forge_url = "http://10.10.10.59:11434"
try:
    with urllib.request.urlopen(f"{forge_url}/api/tags", timeout=3) as r:
        data = json.loads(r.read())
        models = [m['name'] for m in data.get('models', [])]
        print(f"[hive]   DFORGE-100 ONLINE — {len(models)} models: {', '.join(models[:3])}")
except:
    print("[hive]   DFORGE-100 offline (will be probed at runtime)")

# Quick check for QUEEN-11 Ollama
queen_url = "http://10.10.10.56:11434"
try:
    with urllib.request.urlopen(f"{queen_url}/api/tags", timeout=3) as r:
        data = json.loads(r.read())
        models = [m['name'] for m in data.get('models', [])]
        print(f"[hive]   QUEEN-11    ONLINE — {len(models)} models {', '.join(models[:3])}")
except:
    print("[hive]   QUEEN-11 Ollama not available (compute on primary node only)")

print("[hive]   Full topology probe will run 5s after FastAPI startup")
PYEOF

else
    echo "[neuron] ⚠ Ollama unavailable — starting in degraded mode"
    echo "[neuron]   Hive probe will attempt all nodes at runtime"
fi

echo ""
echo "[neuron] ────────────────────────────────────────────────────────"
echo "[neuron] Launching CVG Neuron FastAPI Intelligence Engine"
echo "[neuron] Host:    0.0.0.0:8095"
echo "[neuron] Model:   ${MODEL_NAME} (via ${OLLAMA_URL})"
echo "[neuron] Hive:    10 nodes registered — probing async"
echo "[neuron] Tunnel:  Blockchain chain initializing"
echo "[neuron] Memory:  JSON tiers at ${DATA_DIR}/memory/"
echo "[neuron] ────────────────────────────────────────────────────────"
echo ""

# ─── Launch FastAPI ───────────────────────────────────────────────────────────
exec uvicorn neuron.web_api:app \
    --host 0.0.0.0 \
    --port 8095 \
    --workers 1 \
    --log-level info
