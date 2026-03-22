#!/usr/bin/env bash
# CVG Neuron -- Hive Push Agent Deployment Script
# (c) Clearview Geographic, LLC -- Proprietary and PRIVATE
#
# Deploys the cvg_push_agent.py to ALL Hive-0 nodes and installs
# it as a systemd timer (preferred) or cron job (fallback).
#
# Usage:
#   chmod +x scripts/deploy_hive_agents.sh
#   bash scripts/deploy_hive_agents.sh
#
# Requirements:
#   - SSH key auth configured for each target node
#   - Python 3 installed on each target node
#   - Network access to all 10.10.10.x nodes from this machine
#
# Target nodes (edit _NODES array below to match your environment):
#   - VMs on cvg-stormsurge-01: vm-451, vm-454, vm-455
#   - QUEEN nodes: QUEEN-11 Proxmox, QUEEN-21 Terra, QUEEN-10 TrueNAS
#   - Containers: CT-104
#   - Audit VM: 10.10.10.220
#   - NAS: QUEEN-12, QUEEN-20, QUEEN-30 (Synology, SSH must be enabled)

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_SRC="${SCRIPT_DIR}/cvg_push_agent.py"
REMOTE_PATH="/opt/cvg/push_agent.py"
NEURON_HOST="${CVG_NEURON_HOST:-10.10.10.200}"
NEURON_PORT="${CVG_NEURON_PORT:-8095}"
NEURON_KEY="${CVG_INTERNAL_KEY:-cvg-internal-2026}"
CAPTURE_PORT="${CVG_CAPTURE_PORT:-8098}"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=8 -o BatchMode=yes -o LogLevel=ERROR"
DRY_RUN="${DRY_RUN:-0}"

# ALL Hive-0 nodes — format: "user@host:description"
_NODES=(
    "root@10.10.10.200:vm-451/cvg-stormsurge-01 (PRIMARY Ollama)"
    "root@10.10.10.204:vm-454"
    "root@10.10.10.205:vm-455"
    "root@10.10.10.56:QUEEN-11 Proxmox (Dell R820)"
    "root@10.10.10.57:QUEEN-21 Terra"
    "root@10.10.10.100:QUEEN-10 TrueNAS"
    "root@10.10.10.104:CT-104 (LXC)"
    "root@10.10.10.220:Audit VM (Ubuntu 22.04)"
    "admin@10.10.10.53:QUEEN-12 Synology DS1823+"
    "admin@10.10.10.67:QUEEN-20 Synology DS3622xs+"
    "admin@10.10.10.71:QUEEN-30 Synology DS418"
)

# ── Colors ────────────────────────────────────────────────────────────────────

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

log_ok()   { echo -e "${GREEN}  [OK]${NC} $*"; }
log_warn() { echo -e "${YELLOW}  [WARN]${NC} $*"; }
log_err()  { echo -e "${RED}  [ERR]${NC} $*"; }
log_info() { echo -e "${CYAN}  [INFO]${NC} $*"; }

# ── Preflight ─────────────────────────────────────────────────────────────────

if [[ ! -f "$AGENT_SRC" ]]; then
    log_err "Agent script not found: $AGENT_SRC"
    exit 1
fi

echo ""
echo "================================================="
echo " CVG Neuron Hive Push Agent Deployment"
echo "================================================="
echo " Agent:      $AGENT_SRC"
echo " Deploy to:  $REMOTE_PATH"
echo " Neuron:     $NEURON_HOST:$NEURON_PORT"
echo " Nodes:      ${#_NODES[@]}"
echo "================================================="
echo ""

if [[ "$DRY_RUN" == "1" ]]; then
    log_warn "DRY RUN MODE — no changes will be made"
    echo ""
fi

# Counters
ok_count=0
fail_count=0
skip_count=0
declare -A results

# ── Deploy function ───────────────────────────────────────────────────────────

deploy_to_node() {
    local node_spec="$1"
    local target="${node_spec%%:*}"
    local description="${node_spec#*:}"
    local user_host="$target"

    echo -e "${CYAN}>> $user_host${NC} ($description)"

    if [[ "$DRY_RUN" == "1" ]]; then
        log_warn "  DRY RUN: would deploy to $user_host"
        results[$user_host]="dry_run"
        return 0
    fi

    # 1. Test SSH connectivity
    if ! ssh $SSH_OPTS "$user_host" 'echo ok' &>/dev/null; then
        log_warn "  SSH not available — skipping (check key auth)"
        results[$user_host]="no_ssh"
        ((skip_count++)) || true
        return 0
    fi

    # 2. Check Python3 availability
    if ! ssh $SSH_OPTS "$user_host" 'command -v python3' &>/dev/null; then
        log_warn "  python3 not found — skipping"
        results[$user_host]="no_python3"
        ((skip_count++)) || true
        return 0
    fi

    # 3. Create remote directory and copy agent
    ssh $SSH_OPTS "$user_host" "mkdir -p /opt/cvg"
    if ! scp $SSH_OPTS "$AGENT_SRC" "$user_host:$REMOTE_PATH" &>/dev/null; then
        log_err "  SCP failed"
        results[$user_host]="scp_failed"
        ((fail_count++)) || true
        return 0
    fi
    ssh $SSH_OPTS "$user_host" "chmod +x $REMOTE_PATH"
    log_ok "  Agent deployed"

    # 4. Set environment config on remote node
    ssh $SSH_OPTS "$user_host" "cat > /opt/cvg/push_agent_env.sh << 'ENVEOF'
export CVG_NEURON_HOST=\"${NEURON_HOST}\"
export CVG_NEURON_PORT=\"${NEURON_PORT}\"
export CVG_INTERNAL_KEY=\"${NEURON_KEY}\"
export CVG_CAPTURE_PORT=\"${CAPTURE_PORT}\"
export CVG_NODE_ID=\"\$(hostname)\"
ENVEOF
chmod 600 /opt/cvg/push_agent_env.sh"
    log_ok "  Environment config written"

    # 5. Update the cron/systemd to source env before running
    local full_cmd="bash -c 'source /opt/cvg/push_agent_env.sh && python3 ${REMOTE_PATH} --push'"

    # 6. Install (systemd preferred, cron fallback)
    local install_result
    install_result=$(ssh $SSH_OPTS "$user_host" "
        python3 ${REMOTE_PATH} --install 2>&1
        echo exit=\$?
    " 2>&1)

    if echo "$install_result" | grep -q 'installed'; then
        log_ok "  Timer/cron installed"
    else
        log_warn "  Install may have issues: $(echo "$install_result" | tail -1)"
    fi

    # 7. Run a quick test push
    local test_result
    test_result=$(ssh $SSH_OPTS "$user_host" "
        source /opt/cvg/push_agent_env.sh
        python3 ${REMOTE_PATH} --test 2>&1
    " 2>&1)

    if echo "$test_result" | grep -qi 'OK'; then
        log_ok "  Connectivity test: Neuron reached"
        results[$user_host]="ok"
        ((ok_count++)) || true
    else
        log_warn "  Connectivity test: could not reach Neuron (agent will retry)"
        log_warn "  Output: $(echo "$test_result" | tail -1)"
        results[$user_host]="deployed_no_reach"
        ((ok_count++)) || true  # Still deployed successfully
    fi

    echo ""
}

# ── Main deployment loop ──────────────────────────────────────────────────────

for node_spec in "${_NODES[@]}"; do
    deploy_to_node "$node_spec"
done

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "================================================="
echo " Deployment Summary"
echo "================================================="
echo " Deployed:  $ok_count"
echo " Skipped:   $skip_count"
echo " Failed:    $fail_count"
echo ""

for node in "${!results[@]}"; do
    status="${results[$node]}"
    case "$status" in
        ok|deployed_no_reach)
            echo -e "  ${GREEN}+ ${node}${NC}: $status"
            ;;
        no_ssh|no_python3)
            echo -e "  ${YELLOW}~ ${node}${NC}: $status"
            ;;
        scp_failed)
            echo -e "  ${RED}X ${node}${NC}: $status"
            ;;
        *)
            echo -e "  ${CYAN}? ${node}${NC}: $status"
            ;;
    esac
done

echo ""
if [[ $ok_count -gt 0 ]]; then
    echo -e "${GREEN}Push agents deployed to $ok_count node(s).${NC}"
    echo "Each agent will push AI history every 5 minutes to Neuron ($NEURON_HOST)."
    echo ""
    echo "Verify from any node:"
    echo "  ssh root@10.10.10.200 'cat /var/log/cvg-push-agent.log 2>/dev/null | tail -20'"
    echo ""
    echo "Or via Neuron API:"
    echo "  curl -H 'X-CVG-Key: $NEURON_KEY' http://$NEURON_HOST:$NEURON_PORT/api/memory/captures"
fi
