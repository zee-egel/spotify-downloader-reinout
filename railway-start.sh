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
gosu slskd /slskd/slskd --app-dir "$SLSKD_APP_DIR" &
pids+=("$!")
gosu slskd python app.py &
pids+=("$!")
echo 'Started dashboard and slskd with shared persistent downloads.'
# A failed child stops the service, letting Railway restart both processes.
set +e
wait -n "${pids[@]}"
status=$?
exit "$((status == 0 ? 1 : status))"
