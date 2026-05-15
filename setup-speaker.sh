#!/usr/bin/env bash
# setup-speaker.sh — One-time speaker configuration for Soundcork
#
# Run this once whenever the server IP changes or after a fresh install.
# It does NOT start Soundcork — use docker compose for that.
#
# What it does:
#   1. Reads BASE_URL and SPEAKER_IP from .env
#   2. Writes OverrideSdkPrivateCfg.xml on the speaker (points BMX to this server)
#   3. Writes /mnt/nv/rc.local on the speaker (nc relay 127.0.0.1:30034 → server)
#   4. Starts the nc relay immediately
#   5. Reboots the speaker if the XML config changed
#
# Usage:
#   ./setup-speaker.sh
#   ./setup-speaker.sh 192.168.1.200          # override SPEAKER_IP
#   ./setup-speaker.sh 192.168.1.200 http://192.168.1.100:8001  # override both
#
# Requirements:
#   - SSH key access to root@SPEAKER_IP (legacy RSA key)
#   - .env file with BASE_URL and SPEAKER_IP (or pass as arguments)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Load .env ───────────────────────────────────────────────────────────────
if [[ -f .env ]]; then
    # shellcheck disable=SC1091
    set -o allexport; source .env; set +o allexport
fi

SPEAKER_IP="${1:-${SPEAKER_IP:-}}"
BASE_URL="${2:-${BASE_URL:-}}"

if [[ -z "$SPEAKER_IP" ]]; then
    echo "ERROR: SPEAKER_IP not set. Add it to .env or pass as first argument."
    exit 1
fi
if [[ -z "$BASE_URL" ]]; then
    echo "ERROR: BASE_URL not set. Add it to .env or pass as second argument."
    exit 1
fi

# Extract host:port from BASE_URL (strip http://)
SERVER_HOSTPORT="${BASE_URL#http://}"
SERVER_IP="${SERVER_HOSTPORT%%:*}"
# Port 8001 is the nginx-ETag proxy; BMX relay on device uses direct port 8000
# The relay target should be port 8000 (direct uvicorn), not 8001 (nginx)
SERVER_PORT=8000

echo "Speaker IP: $SPEAKER_IP"
echo "BASE_URL:   $BASE_URL  (relay target: $SERVER_IP:$SERVER_PORT)"

# ── SSH options ─────────────────────────────────────────────────────────────
SSH_OPTS="-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa -o ConnectTimeout=10 -o StrictHostKeyChecking=no"

# ── 1. Update OverrideSdkPrivateCfg.xml ─────────────────────────────────────
TEMPLATE_FILE="soundcork/resources/OverrideSdkPrivateCfg.xml.template"
TARGET_XML="/mnt/nv/OverrideSdkPrivateCfg.xml"

EXPECTED_XML=$(sed "s|{SC_BASE_URL}|${BASE_URL}|g" "$TEMPLATE_FILE")
CURRENT_XML=$(ssh $SSH_OPTS "root@$SPEAKER_IP" "cat $TARGET_XML 2>/dev/null" || true)

if [[ "$EXPECTED_XML" != "$CURRENT_XML" ]]; then
    echo "Updating $TARGET_XML on speaker..."
    echo "$EXPECTED_XML" | ssh $SSH_OPTS "root@$SPEAKER_IP" "cat > $TARGET_XML"
    echo "Speaker XML config updated."
    REBOOT_NEEDED=true
else
    echo "Speaker XML config already up to date."
    REBOOT_NEEDED=false
fi

# ── 2. Write /mnt/nv/rc.local (nc relay, survives reboots) ──────────────────
RC_LOCAL_CONTENT="#!/bin/sh
# Soundcork: nc relay 127.0.0.1:30034 -> Soundcork server
# Written by setup-speaker.sh — re-run that script if the server IP changes.

# Kill any existing relay on port 30034
OLD_PID=\$(netstat -tlnp 2>/dev/null | grep :30034 | awk '{print \$7}' | cut -d/ -f1)
[ -n \"\$OLD_PID\" ] && kill \"\$OLD_PID\" 2>/dev/null
sleep 1

# Write relay helper (IP set at write time by setup-speaker.sh)
cat > /tmp/relay.sh << 'RELAY'
#!/bin/sh
exec /usr/bin/nc ${SERVER_IP} ${SERVER_PORT}
RELAY
chmod +x /tmp/relay.sh

# Start persistent nc relay (-l -l = accept multiple connections)
nohup /usr/bin/nc -l -l -p 30034 -e /tmp/relay.sh > /tmp/nc_relay.log 2>&1 &"

CURRENT_RC=$(ssh $SSH_OPTS "root@$SPEAKER_IP" "cat /mnt/nv/rc.local 2>/dev/null" || true)
if [[ "$RC_LOCAL_CONTENT" != "$CURRENT_RC" ]]; then
    echo "Writing /mnt/nv/rc.local on speaker (relay → ${SERVER_IP}:${SERVER_PORT})..."
    printf '%s\n' "$RC_LOCAL_CONTENT" | ssh $SSH_OPTS "root@$SPEAKER_IP" \
        "cat > /mnt/nv/rc.local && chmod +x /mnt/nv/rc.local"
    echo "rc.local written."
else
    echo "rc.local already up to date."
fi

# ── 3. Start relay now (no reboot needed for relay itself) ───────────────────
echo "Starting nc relay on speaker..."
ssh $SSH_OPTS "root@$SPEAKER_IP" "/mnt/nv/rc.local"
sleep 1
RELAY_STATUS=$(ssh $SSH_OPTS "root@$SPEAKER_IP" \
    "netstat -tlnp 2>/dev/null | grep :30034 | awk '{print \$4, \$7}'" || true)
if [[ -n "$RELAY_STATUS" ]]; then
    echo "nc relay active: $RELAY_STATUS"
else
    echo "WARNING: nc relay may not have started. Check /tmp/nc_relay.log on speaker."
fi

# ── 4. Reboot speaker if XML config changed ──────────────────────────────────
if [[ "$REBOOT_NEEDED" == "true" ]]; then
    echo "Rebooting speaker to apply new XML config..."
    ssh $SSH_OPTS "root@$SPEAKER_IP" "reboot" || true
    echo "Waiting for speaker to come back online..."
    for i in $(seq 1 30); do
        sleep 5
        if curl -s --max-time 2 "http://$SPEAKER_IP:8090/info" > /dev/null 2>&1; then
            echo "Speaker is back online after $((i*5))s."
            break
        fi
        echo "  waiting... ($((i*5))s)"
    done
fi

echo ""
echo "Done. Speaker is configured for $BASE_URL"
echo "Start Soundcork with:  docker compose up -d"
