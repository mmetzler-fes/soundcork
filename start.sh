#!/usr/bin/env bash
# start.sh — Soundcork startup script
#
# Automates all setup needed to run Soundcork on any host:
#   1. Detects this server's LAN IP (the interface that can reach the speaker)
#   2. Updates BASE_URL in .env.shared
#   3. SSHes to speaker and updates OverrideSdkPrivateCfg.xml if IP changed
#   4. Writes /mnt/nv/rc.local on speaker (nc relay 127.0.0.1:30034 → server:8000)
#   5. Runs /mnt/nv/rc.local immediately (starts nc relay now)
#   6. Reboots speaker if XML config changed
#   7. Starts Soundcork
#
# Usage:
#   ./start.sh [SPEAKER_IP]
#
# SPEAKER_IP defaults to the value in .env.shared (SPEAKER_IP=...) or
# falls back to the last known device. Set it once in .env.shared:
#   SPEAKER_IP=192.168.178.139
#
# Requirements on the server:
#   - Python 3.x with soundcork venv at .venv/
#   - ssh key access to root@SPEAKER_IP (with HostKeyAlgorithms=+ssh-rsa)
#   - Port 8000 open in firewall (ufw allow 8000/tcp)
#
# Requirements on the speaker:
#   - SSH root access
#   - BusyBox nc with -e support (/usr/bin/nc)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Load .env.shared for SPEAKER_IP ────────────────────────────────────────
if [[ -f .env.shared ]]; then
    SPEAKER_IP_CONFIGURED=$(grep -E '^SPEAKER_IP=' .env.shared | cut -d= -f2 | tr -d '[:space:]' || true)
else
    SPEAKER_IP_CONFIGURED=""
fi

SPEAKER_IP="${1:-${SPEAKER_IP_CONFIGURED:-}}"
if [[ -z "$SPEAKER_IP" ]]; then
    echo "ERROR: Speaker IP unknown. Pass it as argument or set SPEAKER_IP=... in .env.shared"
    exit 1
fi
echo "Speaker IP: $SPEAKER_IP"

# ── SSH options ─────────────────────────────────────────────────────────────
SSH_OPTS="-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa -o ConnectTimeout=10 -o StrictHostKeyChecking=no"

# ── 1. Detect own LAN IP (the interface that routes to the speaker) ─────────
SERVER_IP=$(ip route get "$SPEAKER_IP" 2>/dev/null | grep -oP 'src \K[0-9.]+' | head -1)
if [[ -z "$SERVER_IP" ]]; then
    echo "ERROR: Could not determine local IP for reaching $SPEAKER_IP"
    exit 1
fi
SERVER_PORT=8000
BASE_URL="http://${SERVER_IP}:${SERVER_PORT}"
echo "Server IP:  $SERVER_IP  →  BASE_URL=$BASE_URL"

# ── 2. Update BASE_URL in .env.shared ───────────────────────────────────────
if grep -qE '^BASE_URL=' .env.shared 2>/dev/null; then
    sed -i "s|^BASE_URL=.*|BASE_URL=${BASE_URL}|" .env.shared
else
    echo "BASE_URL=${BASE_URL}" >> .env.shared
fi
echo "Updated .env.shared: BASE_URL=$BASE_URL"

# ── 3. Update OverrideSdkPrivateCfg.xml on speaker (only if IP changed) ────
TEMPLATE_FILE="soundcork/resources/OverrideSdkPrivateCfg.xml.template"
TARGET_XML="/mnt/nv/OverrideSdkPrivateCfg.xml"

EXPECTED_XML=$(sed "s|{SC_BASE_URL}|${BASE_URL}|g" "$TEMPLATE_FILE")
CURRENT_XML=$(ssh $SSH_OPTS "root@$SPEAKER_IP" "cat $TARGET_XML 2>/dev/null" || true)

if [[ "$EXPECTED_XML" != "$CURRENT_XML" ]]; then
    echo "Updating $TARGET_XML on speaker..."
    echo "$EXPECTED_XML" | ssh $SSH_OPTS "root@$SPEAKER_IP" "cat > $TARGET_XML"
    echo "Speaker config updated."
    REBOOT_NEEDED=true
else
    echo "Speaker config already up to date."
    REBOOT_NEEDED=false
fi

# ── 4. Write /mnt/nv/rc.local on speaker (nc relay, boot-persistent) ────────
RC_LOCAL_CONTENT="#!/bin/sh
# Soundcork: nc relay 127.0.0.1:30034 -> Soundcork server
# Written by start.sh — do not edit manually

# Kill any existing relay on port 30034
OLD_PID=\$(netstat -tlnp 2>/dev/null | grep :30034 | awk '{print \$7}' | cut -d/ -f1)
[ -n \"\$OLD_PID\" ] && kill \"\$OLD_PID\" 2>/dev/null
sleep 1

# Write relay helper (IP hardcoded at write time by start.sh)
cat > /tmp/relay.sh << 'RELAY'
#!/bin/sh
exec /usr/bin/nc ${SERVER_IP} ${SERVER_PORT}
RELAY
chmod +x /tmp/relay.sh

# Start persistent nc relay (accepts multiple connections via -l -l)
nohup /usr/bin/nc -l -l -p 30034 -e /tmp/relay.sh > /tmp/nc_relay.log 2>&1 &"

CURRENT_RC=$(ssh $SSH_OPTS "root@$SPEAKER_IP" "cat /mnt/nv/rc.local 2>/dev/null" || true)
if [[ "$RC_LOCAL_CONTENT" != "$CURRENT_RC" ]]; then
    echo "Writing /mnt/nv/rc.local on speaker (relay → ${SERVER_IP}:${SERVER_PORT})..."
    printf '%s\n' "$RC_LOCAL_CONTENT" | ssh $SSH_OPTS "root@$SPEAKER_IP" "cat > /mnt/nv/rc.local && chmod +x /mnt/nv/rc.local"
    echo "rc.local written."
else
    echo "rc.local already up to date."
fi

# ── 5. Run rc.local now to start nc relay immediately ───────────────────────
echo "Starting nc relay on speaker..."
ssh $SSH_OPTS "root@$SPEAKER_IP" "/mnt/nv/rc.local"
sleep 1
RELAY_STATUS=$(ssh $SSH_OPTS "root@$SPEAKER_IP" "netstat -tlnp 2>/dev/null | grep :30034 | awk '{print \$4, \$7}'" || true)
if [[ -n "$RELAY_STATUS" ]]; then
    echo "nc relay active: $RELAY_STATUS"
else
    echo "WARNING: nc relay may not have started. Check /tmp/nc_relay.log on speaker."
fi

# ── 6. Reboot speaker if config changed, wait for it ────────────────────────
if [[ "$REBOOT_NEEDED" == "true" ]]; then
    echo "Rebooting speaker to apply new XML config..."
    ssh $SSH_OPTS "root@$SPEAKER_IP" "reboot" || true
    echo "Waiting for speaker to come back online..."
    for i in $(seq 1 30); do
        sleep 5
        if curl -s --max-time 2 "http://$SPEAKER_IP:8090/info" > /dev/null 2>&1; then
            echo "Speaker is back online after ${i}*5s."
            break
        fi
        echo "  waiting... (${i}*5s)"
    done
fi

# ── 7. Start Soundcork ──────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Starting Soundcork on $BASE_URL ..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Activate venv if present
if [[ -f .venv/bin/activate ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

exec python3 -m uvicorn soundcork.main:app --host 0.0.0.0 --port "$SERVER_PORT"
