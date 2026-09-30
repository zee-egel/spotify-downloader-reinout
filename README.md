# Playlist desk

A small personal dashboard for Spotify playlist submissions, slskd transfers, and completed files. It uses Python's standard library and stores recent submissions in one JSON file. The UI is protected with HTTP Basic authentication. Serve it through an HTTPS reverse proxy or a private VPN; the Compose port is bound to localhost by default.

## Deploy on the Pi

1. Pull this repo. Copy `.env.example` to `.env` on the Pi and keep it private. Set `APP_PASSWORD`, `SLSKD_API_KEY`, `N8N_WEBHOOK_URL`, and `N8N_WEBHOOK_TOKEN`. The webhook URL and token were generated in the development machine's ignored `.env`; transfer those two values privately to the Pi. The webhook URL uses `http://n8n-n8n-1:5678` on the shared `n8n_default` Docker network.
2. Confirm slskd stores completed files at `/downloads`, backed by `/home/admin/slskd/downloads`. The reported host mount and Docker network are prefilled in `.env.example`. The app mounts this completed-downloads directory read-write for deletion. On the Pi, grant the app's UID 1000 access with `sudo setfacl -R -m u:1000:rwX /home/admin/slskd/downloads` and `sudo find /home/admin/slskd/downloads -type d -exec setfacl -m d:u:1000:rwx {} +`. Install the `acl` package if `setfacl` is missing.
3. Run `docker compose up -d --build`. Open `http://localhost:8080` through an SSH tunnel (`ssh -L 8080:127.0.0.1:8080 pi`) or configure an authenticated HTTPS reverse proxy. The browser chooses where downloads land on your Mac.

The n8n workflow `rxn40wQETIoCjwZj` has an authenticated production POST webhook connected to `Playlist Input`, with the original manual URL retained only for manual runs. A successful webhook response is recorded as **submitted**, not completed. The Transfers section polls slskd every 12 seconds for file state, percentage, bytes, speed, and errors. Recent submissions show Spotify playlist titles when Spotify oEmbed provides them; clearing that list only removes the app's history. The Files section shows item counts for each folder and allows permanent file or folder deletion after browser confirmation.

## Verification limits

The webhook and Pi deployment were exercised by the user. A 34-track run revealed that an empty slskd search response stopped the loop after 18 tracks; `Get responses` now has **Always Output Data** enabled, but that fix has not been exercised by a second live run. The app's local smoke checks pass with mocked webhook and slskd responses and safe file fixtures (`python test_local.py`). Playlist execution status from n8n and deletion permissions on the Pi remain unverified.
