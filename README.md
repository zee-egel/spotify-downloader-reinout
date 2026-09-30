# Playlist desk

A small personal dashboard for Spotify playlist submissions, slskd transfers, and completed files. It uses Python's standard library and stores recent submissions in one JSON file. The UI is protected with HTTP Basic authentication. Serve it through an HTTPS reverse proxy or a private VPN; the Compose port is bound to localhost by default.

## Deploy on the Pi

1. Pull this repo. Copy `.env.example` to `.env` on the Pi and keep it private. Set `APP_PASSWORD`, `SLSKD_API_KEY`, `N8N_WEBHOOK_URL`, and `N8N_WEBHOOK_TOKEN`. The webhook URL and token were generated in the development machine's ignored `.env`; transfer those two values privately to the Pi. The webhook URL uses `http://n8n-n8n-1:5678` on the shared `n8n_default` Docker network.
2. Confirm slskd stores completed files at `/downloads`, backed by `/home/admin/slskd/downloads`. The reported host mount and Docker network are prefilled in `.env.example`.
3. Run `docker compose up -d --build`. Open `http://localhost:8080` through an SSH tunnel (`ssh -L 8080:127.0.0.1:8080 pi`) or configure an authenticated HTTPS reverse proxy. The browser chooses where downloads land on your Mac.

The n8n workflow `rxn40wQETIoCjwZj` now has an authenticated production POST webhook connected to `Playlist Input`, with the original manual URL retained only for manual runs. Its existing Spotify, search, scoring, and queue nodes were preserved. A successful webhook response is recorded as **submitted**, not completed. The Transfers section polls slskd every 12 seconds for file state, percentage, bytes, speed, and errors; the Files section uses folder breadcrumbs and refreshes on request.

## Verification limits

The workflow was fetched before and after the edit, and its active state and webhook authentication were verified through the n8n API. No real playlist search or download was started. Automatic approval review rejected a live webhook POST check because a misconfiguration could have triggered a playlist; this endpoint remains untested end to end. The app's local smoke checks pass with a mocked webhook and safe file fixtures (`python test_local.py`). Pi deployment and per-run n8n outcome tracking remain unverified. A read-only Pi check confirmed slskd returns the expected user and directory grouping, but no live transfer was available for progress verification.
