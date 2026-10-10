#!/bin/bash
set -euo pipefail

: "${APP_PASSWORD:?Set APP_PASSWORD}"
: "${SLSKD_SLSK_USERNAME:?Set SLSKD_SLSK_USERNAME}"
: "${SLSKD_SLSK_PASSWORD:?Set SLSKD_SLSK_PASSWORD}"
: "${SLSKD_API_KEY:?Set SLSKD_API_KEY}"
# These credentials protect the separately exposed slskd UI/API.
: "${SLSKD_USERNAME:?Set SLSKD_USERNAME for the slskd web UI}"
: "${SLSKD_PASSWORD:?Set SLSKD_PASSWORD for the slskd web UI}"
if (( ${#SLSKD_API_KEY} < 16 || ${#SLSKD_API_KEY} > 255 )); then
    echo 'SLSKD_API_KEY must contain 16–255 characters.' >&2
    exit 1
fi
if [[ "$SLSKD_DOWNLOADS_DIR" != "$DOWNLOADS_ROOT" ]]; then
    echo 'SLSKD_DOWNLOADS_DIR and DOWNLOADS_ROOT must be identical.' >&2
    exit 1
fi
if [[ "$SLSKD_URL" != "http://127.0.0.1:${SLSKD_HTTP_PORT}" ]]; then
    echo "Set SLSKD_URL=http://127.0.0.1:${SLSKD_HTTP_PORT} for the combined Railway service." >&2
    exit 1
fi

# slskd accepts share-cache intervals of at least 60 minutes.
share_cache_retention="${SLSKD_SHARE_CACHE_RETENTION:-60}"
if [[ ! "$share_cache_retention" =~ ^[0-9]+$ ]] || (( ${#share_cache_retention} > 10 )); then
    echo 'SLSKD_SHARE_CACHE_RETENTION must be an integer between 60 and 2147483647 (minutes).' >&2
    exit 1
fi
share_cache_retention=$((10#$share_cache_retention))
if (( share_cache_retention < 60 )); then
    echo 'Share-cache retention is below slskd’s minimum; using 60 minutes.' >&2
    share_cache_retention=60
elif (( share_cache_retention > 2147483647 )); then
    echo 'SLSKD_SHARE_CACHE_RETENTION must not exceed 2147483647 minutes.' >&2
    exit 1
fi

# Railway mounts its volume as root; both applications run as UID 1000.
directories=("$(dirname "$STATE_FILE")" "$DOWNLOADS_ROOT" "$SLSKD_APP_DIR" "$SLSKD_INCOMPLETE_DIR" "$DOWNLOADS_ROOT/profiles" "$DOWNLOADS_ROOT/profiles/extra")
mkdir -p "${directories[@]}"
chown slskd:slskd "${directories[@]}"

pids=()
stop() {
    trap '' TERM INT
    for pid in "${pids[@]}"; do kill -TERM "$pid" 2>/dev/null || true; done
    wait || true
}
trap 'stop; exit 0' TERM INT
trap stop EXIT
umask "$SLSKD_UMASK"
if [[ -n "${YOUTUBE_EXIT_NODE:-}" ]]; then
    : "${TS_AUTHKEY:?Set TS_AUTHKEY to join the Tailscale network}"
    tailscale_state="$(dirname "$STATE_FILE")/tailscale"
    tailscale_socket=/tmp/playlist-tailscaled.sock
    install -d -m 700 "$tailscale_state"
    # ponytail: userspace SOCKS routes only yt-dlp through the Pi; no system VPN routing.
    tailscaled --tun=userspace-networking --state="$tailscale_state/tailscaled.state" \
        --socket="$tailscale_socket" --socks5-server=127.0.0.1:1055 &
    pids+=("$!")
    for ((attempt=0; attempt<30; attempt++)); do
        [[ -S "$tailscale_socket" ]] && break
        sleep 1
    done
    tailscale --socket="$tailscale_socket" up --auth-key="$TS_AUTHKEY" \
        --hostname=disc-situation-railway --accept-dns=false \
        --exit-node="$YOUTUBE_EXIT_NODE" --timeout=60s
    export YOUTUBE_PROXY=socks5://127.0.0.1:1055
    echo 'YouTube proxy connected through the configured Tailscale exit node.'
fi
# Share completed audio from both profiles; keep staging files and non-audio out.
# CLI options ensure a persisted slskd.yml cannot silently disable this share.
gosu slskd /slskd/slskd --app-dir "$SLSKD_APP_DIR" \
    --shared "[Music]$DOWNLOADS_ROOT" \
    --share-filter '^(?!.*\.(mp3|flac|m4a|aac|ogg|opus|wav|aiff|aif|alac|ape|wma|webm)$).*' \
    --share-cache-retention "$share_cache_retention" &
pids+=("$!")
gosu slskd python app.py &
pids+=("$!")
echo 'Started dashboard and slskd with shared persistent downloads.'
# A failed child stops the service, letting Railway restart both processes.
set +e
wait -n "${pids[@]}"
status=$?
exit "$((status == 0 ? 1 : status))"
