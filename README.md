# Playlist desk

A small personal dashboard for Spotify playlist submissions, slskd transfers, and completed files. It uses Python's standard library and stores recent submissions in one JSON file. The UI is protected with HTTP Basic authentication. Serve it through an HTTPS reverse proxy or a private VPN; the Compose port is bound to localhost by default.

## Deploy on the Pi

1. Find the host path that backs slskd's **completed** downloads directory and the Docker network that reaches slskd. Check the actual Compose mounts; slskd's container path may differ from the host path.
2. Copy `.env.example` to `.env`. Set `APP_PASSWORD` to a long random value, `COMPLETED_DOWNLOADS_HOST_PATH`, `SLSKD_DOCKER_NETWORK`, `SLSKD_API_KEY`, and `N8N_WEBHOOK_URL`. Keep `.env` private. Set `N8N_WEBHOOK_TOKEN` if the webhook requires a bearer token.
3. Run `docker compose up -d --build`. Open `http://localhost:8080` through an SSH tunnel (`ssh -L 8080:127.0.0.1:8080 pi`) or configure an authenticated HTTPS reverse proxy. The browser chooses where downloads land on your Mac.

The n8n production webhook must accept `POST` JSON with `url` and `playlistId` and pass `url` into the existing Playlist Input path. The workflow must be active for a production webhook. Preserve the manual trigger as a second path. A successful webhook response is recorded as **submitted**, not completed; the current app cannot know execution outcome until the live workflow is inspected and wired for status updates.

## Current integration status

The local app and Compose template are ready for review. The reported slskd mount is `/home/admin/slskd/downloads:/downloads` and the shared Docker network is `n8n_default`; these values are filled into `.env.example`. Whether `/downloads` is slskd’s configured completed directory, the installed slskd API schema, n8n workflow, and public app URL remain unverified because Pi SSH authentication failed and Cloudflare returned 1010 for the n8n API request. Do not deploy with placeholder `.env` values. No playlist search or download was started during development.
