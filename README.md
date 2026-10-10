# Playlist desk

## Railway: dashboard and slskd together

`railway.json` selects `Dockerfile.railway`, which runs the dashboard and slskd 0.26.0 in one service. Both run as UID 1000 and use the same downloads directory. The original `Dockerfile` and Pi Compose deployment remain available. The Railway image also includes yt-dlp, Node 22, and ffprobe for YouTube fallback.

1. Attach the service's persistent volume at `/data`. Back up existing state before changing the mount path: `runs.json` and its sibling `profiles.sqlite3` must be moved to `/data/state/` to retain history, Spotify tokens, and Telegram settings. Existing songs on the Pi are not automatically copied. Do not run the old and new slskd instances simultaneously with the same Soulseek login.
2. Set the following Railway variables (the paths and port are also image defaults):

   ```env
   DOWNLOADS_ROOT=/data/downloads
   STATE_FILE=/data/state/runs.json
   SLSKD_APP_DIR=/data/slskd
   APP_DIR=/data/slskd
   SLSKD_DOWNLOADS_DIR=/data/downloads
   SLSKD_INCOMPLETE_DIR=/data/incomplete
   SLSKD_URL=http://127.0.0.1:5030
   PORT=8080
   SLSKD_SLSK_USERNAME=<Soulseek username>
   SLSKD_SLSK_PASSWORD=<Soulseek password>
   SLSKD_API_KEY=<16–255 character API key, matching the n8n slskd credential>
   SLSKD_USERNAME=<separate slskd web UI username>
   SLSKD_PASSWORD=<strong slskd web UI password>
   ```

   Keep `APP_USER`, `APP_PASSWORD`, `APP_PUBLIC_URL`, Spotify variables, `N8N_WEBHOOK_URL`, and `N8N_WEBHOOK_TOKEN`. `COMPLETED_DOWNLOADS_HOST_PATH` and `SLSKD_DOCKER_NETWORK` are Pi Compose settings and have no effect on Railway. Leave the Railway start command unset; the image entrypoint starts both processes and stops the service if either exits. Leave replicas at one. Startup creates the volume directories and assigns them to UID 1000; if migrating root-owned existing files, assign those files to UID 1000 too. Do not override `RAILWAY_RUN_UID` to a non-root UID: startup needs root to prepare Railway's root-owned mount, then drops privileges for both apps.
3. Keep `dj.reinout.dance` targeting port **8080**. Add a second HTTPS domain on the same service, such as `slskd.reinout.dance`, targeting port **5030**. This exposes slskd's authenticated UI/API to n8n on the Pi. Do not disable slskd authentication or use the default UI credentials. The dashboard uses localhost, not this public domain.
4. Export the latest n8n workflow and run `python3 workflow_railway.py before.json after.json https://slskd.reinout.dance`. Review the resulting URLs, apply the update to n8n, and publish. The patch preserves existing credentials and replaces literal slskd `/api/v0` origins. If URLs use variables or expressions for their host, update those separately. Keep dashboard callbacks pointed at `https://dj.reinout.dance`. It also replaces an installed YouTube Execute Command node with a POST to `/youtube-download` using the existing `X-Playlist-Token` callback credential and a five-minute HTTP timeout. If YouTube nodes are not installed yet, apply `workflow_youtube.py` first, then this patch. Running downloads retain their old workflow; switch only when existing work is finished.
5. Configure a Railway TCP proxy for slskd peer traffic and verify actual Soulseek connectivity before switching playlists. Railway assigns a public proxy address/port; slskd's advertised peer address and port must route back to the listener. Exposing port 50300 alone does not guarantee this because the outbound address may differ from the TCP proxy address. If peers cannot connect, use a compatible forwarded-port VPN/SOCKS setup or keep the downloader on a host with controllable peer networking; this image does not solve that networking limitation.
6. After deploying, `/health` must return 200, the authenticated slskd UI must report a Soulseek connection, and a small import must produce a file visible in Library. Restart the service and confirm both history and the file survive. Allocate enough volume storage for your music library.

The YouTube endpoint accepts JSON `{ "profileId": "owner", "submissionId": "...", "track": { ... } }` with `X-Playlist-Token: <N8N_WEBHOOK_TOKEN>`, validates track metadata, and permits one active fallback download at a time. It is synchronous; a disconnected request may still finish on the server, so do not automatically retry uncertain requests. The endpoint returns the same `completed`, `no_match`, or `failed` JSON as the command-line downloader.

Build locally with `docker build -f Dockerfile.railway -t playlist-desk-railway .`. Railway configuration references: [config as code](https://docs.railway.com/config-as-code/reference), [persistent volumes](https://docs.railway.com/volumes), [TCP proxy](https://docs.railway.com/networking/tcp-proxy). Live Railway deployment and peer connectivity require verification in your project.

## Sharing completed music on Soulseek

The combined Railway service shares completed audio recursively from `DOWNLOADS_ROOT` (default `/data/downloads`) under the Soulseek alias **Music**. This includes Soulseek and YouTube downloads and both profiles’ music. Dashboard authentication and profile isolation remain unchanged; Soulseek peers can browse and download the shared music.

The startup command configures the share explicitly using slskd’s native `--shared` option. Incomplete downloads remain in the separate incomplete directory, and the share filter excludes non-audio files and temporary staging files. slskd rescans every 60 minutes by default; set `SLSKD_SHARE_CACHE_RETENTION` to a different interval in minutes if needed (minimum 60), or trigger a manual share scan in the slskd UI. Values below 60 are raised to 60 so older environment settings cannot crash startup. These options take precedence over persisted YAML settings for the corresponding entries. Review any existing custom shares/filters after changing the startup command, since slskd merges list entries by index.

For the separate Pi slskd service (which this repository’s Compose file does not start), add this to **that service’s** `slskd.yml`, keeping its other settings and shares:

```yaml
shares:
  directories:
    - '[Music]/downloads'
  filters:
    - '^(?!.*\.(mp3|flac|m4a|aac|ogg|opus|wav|aiff|aif|alac|ape|wma|webm)$).*'
  cache:
    retention: 15
```

Use the completed-download path **inside the slskd container** if it differs from `/downloads`. After restarting/redeploying, verify **Music** and its file count in slskd’s Shares view. Sharing can satisfy peers’ catalog requirements, but incoming peer connectivity is still necessary for uploads. See [slskd share configuration](https://github.com/slskd/slskd/blob/0.26.0/docs/config.md#shares).

## Playlist completion updates

`workflow_completion.py` patches a fresh Railway workflow export to try YouTube immediately for unmatched, ambiguous, or timed-out searches. Apply it after the YouTube and Railway patches, publish the resulting workflow, and deploy the dashboard changes. Failed transfers can recover without the initial five-minute delay; active stalled transfers still require safe cancellation before replacement. Completion polling runs every ten seconds and immediately after a YouTube result.

Pass the submission ID to `/youtube-download` to display live search, match checking, download percentage, and audio validation stages on the homepage. The playlist bar counts tracks handled, including unavailable tracks, while the downloaded count remains separate. Unknown transfer state still uses the existing monitoring timeout.

## YouTube fallback (workflow installation required)

For the combined Railway service, use the HTTP endpoint and workflow patch described above. The Execute Command setup below is for downloads stored on the n8n host.

`workflow_youtube.py` patches a fresh export of the live workflow. Generate the update body with `python3 workflow_youtube.py before.json after.json`. Preserve current credentials and profile routing; do not apply an old export over newer edits. The dashboard app requires no YouTube endpoint or dependencies.

n8n owns the five-minute initial wait, alternate Soulseek selection, another five-minute allowance, progress monitoring, cancellation verification, fallback decisions and results. Transfers with advancing byte counts are preserved. Unmatched tracks go to YouTube after the initial wait; missing transfer state never authorizes a duplicate. The existing 48-hour monitoring ceiling remains for unknown transfer/cancellation state.

The new **Download YouTube Audio** Execute Command node invokes `/opt/n8n-scripts/youtube_fallback.py` once and reads its JSON result. Install Python 3.10+, `yt-dlp[default]`, a supported Node runtime and ffprobe in the n8n execution container/worker, mount the script there read-only, and mount the existing completed-downloads directory at `/downloads` with write access for the n8n user. `DOWNLOADS_ROOT` may override this path. Execute Command must be available in the existing n8n configuration; check before publishing. On Docker it runs inside the container, not on the host. These environment changes have not been applied.

The Python command only searches, validates and downloads one track; it runs no background jobs and owns no retry schedule. It checks artist/title/version, requires duration within 1% clamped to 2–5 seconds, and validates the saved audio with ffprobe. Metadata matching is conservative, not an audio-fingerprint guarantee. It retains the best available original audio stream without lossy conversion. Its overall deadline is four minutes, with a 90-second per-command limit. n8n records failure instead of looping forever.

Run `python3 test_youtube.py` for offline matching, audio validation and retry-state checks. Live deployment and real YouTube availability remain unverified.

A small personal dashboard for Spotify playlist submissions, slskd transfers, and completed files. It uses Python's standard library and stores recent submissions in one JSON file. The UI is protected with HTTP Basic authentication. Serve it through an HTTPS reverse proxy or a private VPN; the Compose port is bound to localhost by default.

## Deploy on the Pi

For Railway, use the public production webhook URL and set `N8N_WEBHOOK_TOKEN` to the value in n8n's **Header Auth account 5**. Set that credential's header name to `X-Playlist-Token`; use it on the webhook and all dashboard callback nodes. The app sends this header on submissions as well as the legacy `X-Webhook-Token` header, preserving the original Pi workflow. Redeploy the app after updating its code. Callback URLs must point to `https://dj.reinout.dance/workflow-status`.

1. Pull this repo. Copy `.env.example` to `.env` on the Pi and keep it private. Set `APP_PASSWORD`, `SLSKD_API_KEY`, `N8N_WEBHOOK_URL`, and `N8N_WEBHOOK_TOKEN`. The webhook URL and token were generated in the development machine's ignored `.env`; transfer those two values privately to the Pi. The webhook URL uses `http://n8n-n8n-1:5678` on the shared `n8n_default` Docker network.
2. Confirm slskd stores completed files at `/downloads`, backed by `/home/admin/slskd/downloads`. The reported host mount and Docker network are prefilled in `.env.example`. The app mounts this completed-downloads directory read-write for deletion. On the Pi, grant the app's UID 1000 access with `sudo setfacl -R -m u:1000:rwX /home/admin/slskd/downloads` and `sudo find /home/admin/slskd/downloads -type d -exec setfacl -m d:u:1000:rwx {} +`. Install the `acl` package if `setfacl` is missing.
3. Run `docker compose up -d --build`. Open `http://localhost:1111` through an SSH tunnel (`ssh -N -L 1111:127.0.0.1:1111 pi`) or configure an authenticated HTTPS reverse proxy. The browser chooses where downloads land on your Mac.

The n8n workflow `rxn40wQETIoCjwZj` has an authenticated production POST webhook connected to `Playlist Input`. A successful webhook response is recorded as **submitted**, not completed. n8n posts queue and progress summaries back to `http://playlist-desk:8080/workflow-status` on the shared Docker network, using the existing `N8N_WEBHOOK_TOKEN`. Rebuild the Pi app before starting another playlist so this callback exists. Older submissions have no track details; new submissions use a unique submission ID to match callbacks to the right run. The browser receives live transfer snapshots through an authenticated event stream; the app alone calls slskd. Transfers are grouped in collapsible playlist sections inside a scrollable list; transfers without a tracked playlist appear under **Other transfers**. Transfer search, filters, and sort controls stay in place as data changes. Clearing recent submissions only removes the app's history.

The live workflow runs one slskd search at a time: this slskd instance rejects overlapping searches with “Only one concurrent operation is permitted.” It checks the first result after 15 seconds, then every 2 seconds until the search completes, so it can start the next track promptly without overlapping search operations. Workflow edits apply to new executions; a playlist already running keeps its stored workflow logic. The app lists search timeouts and search errors separately from failed file transfers; rebuild the Pi app to see those labels.

After all tracks have been searched and queued, n8n sends a **queue summary** Telegram message using its first transfer snapshot. It lists completed and pending files, remote and local queues, unmatched tracks, review items, search timeouts, workflow errors, and failed downloads separately. A remotely queued transfer remains active. After five minutes, it can try a high confidence candidate from another peer with a free upload slot and no queue. As soon as every other transfer is completed or remotely queued, it can also try a high confidence alternate even if that peer has a queue. Before queueing an alternate, the workflow cancels the original and verifies cancellation to avoid duplicate files; a track with transferred bytes is left alone. If no suitable alternate exists, the original stays pending. A completed failure can also try one alternate. n8n sends a **one-time remote queue notice** when no fast alternate exists or the alternate also remains remotely queued. A **final update** follows when all current batches complete or after 48 hours. The Files section retains item counts, downloads, deletion, and sorting by name, type, size, or modified date.

## Verification limits

The app's local smoke checks pass with mocked webhook and slskd responses and safe file fixtures (`python test_local.py`). The faster concurrent search setting was exercised by a real playlist and caused 5 rejected search starts and 20 search timeouts; the sequential search path is active again. The remote queue fallback, Telegram queue summary, and callbacks have static and mocked checks only. Pi deployment and filesystem deletion permissions must be checked on the Pi.

## Interface

Edit markup in `templates/server/*.html` for pages, settings, file rows, and errors, or `templates/client/*.html` for live playlist and transfer content. Both use `${field}` placeholders and normal HTML formatting. `views.py` uses Python’s standard-library `string.Template`; `ui.js` fills client fragments embedded in the page. User-provided values must retain their existing `html.escape()` / `esc()` calls before substitution; nested rendered HTML is passed through. Profile interactions live in `profile.js`. Restart the app after template or script changes; Docker images include these files. No build step or new dependency is required.

## Two accounts and personal connections

The existing `APP_USER` / `APP_PASSWORD` login remains the owner account. To add one account, set `EXTRA_APP_USER` and `EXTRA_APP_PASSWORD` in Railway. There is no signup flow. The additional account has its own import history, transfer visibility, Spotify connection, Telegram settings, and downloads directory (`profiles/extra` under the completed-downloads root). The owner cannot browse or delete that directory through the app. When the second account is configured, untracked slskd transfers are hidden instead of exposing another user's downloads. Clearing history hides imports but retains ownership records, so transfers stay private.

Set `SPOTIFY_CLIENT_ID` to your Spotify developer application's client ID and `APP_PUBLIC_URL=https://dj.reinout.dance`. Add `https://dj.reinout.dance/spotify/callback` as an allowed redirect URI in Spotify's developer dashboard, and allowlist both Spotify users if the app is in development mode. No Spotify client secret is needed: the app uses authorization code with PKCE, short-lived user-bound state, and token refresh. Each person opens **Profile → Connect Spotify**, then authorizes their own account. Imports use that person's Spotify access, including private playlists; Spotify tokens stay in the app and are never passed to n8n.

In **Profile**, users can save a Telegram bot token and numeric chat ID. Create the bot through BotFather and start a chat with it first. The app verifies the bot and chat; **Send test message** checks delivery. Tokens are never shown back in HTML. Workflow messages are routed by the recorded submission owner rather than a user ID supplied in the callback. No configured bot means notifications are skipped. Personal bot connections replace the workflow's old shared Telegram credential for new runs.

Deployment order:

1. Deploy the app code with the second account variables unset initially. Mount persistent storage at `/state`; profile tokens and settings are stored in `/state/profiles.sqlite3` with mode 0600, alongside the import history. Run one app replica. The database contains secrets and must stay private, including its backups.
2. Set `SPOTIFY_CLIENT_ID` / `APP_PUBLIC_URL`, register the Spotify redirect URI, and connect the owner account in Profile.
3. Apply `workflow_profiles.py` to an export of the **slskd live** workflow (`t0BF6Lwkxmq6STOC`): `python3 workflow_profiles.py before.json after.json`. The result is a `PUT /api/v1/workflows/t0BF6Lwkxmq6STOC` body. Publish the updated workflow after deploying the app. It reads playlist metadata from the submission, directs both normal and fallback downloads to the user's directory, and sends notifications to the app's `/workflow-notify` endpoint using the existing callback credential. It requires linked Spotify accounts for new imports; existing executions retain their stored logic.
4. Set `EXTRA_APP_USER` / `EXTRA_APP_PASSWORD`, then have the second person log in and connect Spotify and Telegram. Do not enable their login before publishing the profile workflow: the old workflow does not route private download destinations. Browser Basic authentication caches logins, so use separate browser profiles for the two accounts when testing.

The app's downloads root must expose the same completed files as slskd for Library access. A Railway volume is not a mount of the Pi's filesystem; configuring `SLSKD_URL` alone provides transfer status, not local access to Pi files. The new profile folder must exist in the filesystem visible to the app. slskd must use its normal destination rules rather than the special `{}` setting that discards destination subdirectories.

The shared search service still permits one search at a time. While a playlist is resolving, another import returns a retry message; status callbacks renew a five-minute lease. A stalled run can outlive this lease, so this is for the current single-replica, two-account deployment, not a general job queue.

Run `python3 test_profiles.py` for isolated checks covering cross-account file/ZIP/delete access, history, event streams, transfer ownership, OAuth state/replay and refresh, private Telegram routing, and generated workflow destinations. These checks mock external services; live Spotify consent and slskd file placement require deployment verification.

The light music workbench has Import, Downloads, Library, and Profile views. A responsive sidebar, cobalt controls, and shared accessible components keep navigation and actions consistent. Imports show handled tracks, downloaded songs, and unavailable tracks separately; finished runs distinguish full completion, partial success, and no downloaded songs. Per-track details explain Soulseek search, queues, YouTube recovery, and audio validation. Available music is accessible before a run finishes. Import accepts Spotify playlist URLs or URIs; album and track imports are not supported by the workflow. Downloads shows live progress, filters, source details, and 100 files at a time. Library searches the current folder and retains sorting, individual downloads, ZIP downloads, and confirmed deletion. Cmd/Ctrl+K focuses the current view’s input; Escape clears search or closes the focused details section. No frontend dependencies or build step are required; Docker includes `ui.css` and `ui.js` alongside `app.py`.

Run `python3 test_local.py` for authenticated HTTP workflow checks and `node test_ui.js` for frontend rendering, routing, escaping, filtering, and pagination checks. These use isolated fixtures, not production downloads. The redesigned interface received browser visual and interaction QA with agent-browser using an isolated local preview and mock downloads, at desktop and mobile sizes. Authentication, profile isolation, and HTTP download checks pass. The existing JavaScript suite contains an obsolete “1 of 2 complete” copy assertion; its remaining checks pass when the extracted client templates are supplied to the harness and that assertion is adapted in memory to the separate downloaded-song count. Live Spotify consent, Telegram delivery, and real Soulseek/YouTube downloads still require integration verification. No live workflow or deployment was changed.

## Live playlist and Library updates

The workflow now reports normalized track metadata before searching, the current search, the selected source before queueing, and each queue result. The app registers imports before calling n8n, merges these partial callbacks, and uses the selected peer and exact filename to identify transfers until their batch ID arrives. This avoids waiting for the final queue summary to show tracks or group downloads. Older workflow executions keep their original callback timing.

The event stream also refreshes the current Library folder, preserving its sort and search, and displays live download activity above saved files. Updates normally arrive on the existing two-second polling interval; an unavailable slskd service can delay a cycle by its request timeout.

The early-callback workflow version was applied through the n8n API. **Rebuild the app on the Pi** (`docker compose up -d --build`) after transferring these code changes to receive the new metadata and live Library behavior. No running playlist was restarted. `workflow_live_updates.py` preserves the workflow patch as source: apply it to an export of the previous workflow to produce an API update body. It refuses to patch an already-updated workflow. `python3 test_workflow.py` verifies callback payloads and loop routing without API credentials.

## Download reliability and diagnosis

YouTube recovery now searches up to three query variants, deduplicates results, and tries up to six candidates within the existing four-minute budget. A broken extraction, timeout, or invalid candidate no longer aborts the other candidates. Featured-artist credits and remaster-year labels are normalized while live, remix, cover, edit and other version checks remain. Duration remains within 2–5 seconds for ordinary uploads; an exactly matching artist Topic channel allows 2% up to eight seconds. Downloaded audio must pass the same duration check before atomic publication to Library.

Results retain the existing `completed`, `no_match`, and `failed` contract and add a failure `code` and bounded candidate `diagnostics`. Reasons distinguish bot checks, rate limiting, denied access, unavailable videos, missing runtimes, validation and storage failures. Raw extractor output and credentials are never returned. The dashboard retains these diagnostics in its run state and shows the human-readable reason in track details.

The endpoint returns HTTP 409 with `retryable: true` and `retryAfterSeconds: 60` **only when no download was started because the worker was busy**. Workflow patches retry that explicit response up to eight total attempts with a one-minute cooldown. Network errors and disconnected requests are terminal because the original download may still be running. The waiting state is visible in the dashboard.

For an already configured workflow, export it and generate a reviewable upgrade locally:

```sh
python3 workflow_recovery.py before.json after.json
```

This command only writes a JSON file. It does not connect to n8n, publish, or deploy. New YouTube/Railway/completion patches include the same changes. Existing running workflows keep their original behavior until an updated export is applied by the operator.

Run the read-only diagnostic on the actual download host:

```sh
python3 download_doctor.py --slskd
```

It reports installed yt-dlp/EJS, Node and ffprobe versions, recorded recovery outcome codes, and API reachability without exposing credentials. Node must be at least version 22; keep yt-dlp and EJS updated together (`python3 -m pip install --upgrade 'yt-dlp[default]'` in the execution environment). Railway builds now verify that these runtime components are present. See the [official yt-dlp runtime instructions](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

From a separate machine/network, probe the configured public peer endpoint with `python3 download_doctor.py --peer-host HOST --peer-port PORT`. A successful TCP probe does **not** prove Soulseek advertised-address correctness; verify incoming peer connections in slskd. Railway peer routing, remote peers, private videos and YouTube bot blocks cannot be repaired by local matching code. Correct forwarding or a compatible download host is still necessary; consult [slskd networking configuration](https://github.com/slskd/slskd/blob/master/docs/config.md).

## Telegram recovery notifications

`workflow_notifications.py` keeps one short Telegram message when the playlist finishes: its name, downloaded/total count, unavailable count, and a link to Library. Queue, remote-pending, and per-track YouTube messages are removed. Recovery and detailed outcomes remain visible in the app. Monitoring timeouts describe missing downloads as unconfirmed.

Existing owner/private notification routing and credentials are preserved. Notification failures do not stop downloads.

Run `python3 workflow_notifications.py current-export.json updated-export.json` for an offline upgrade. Updating an already active workflow through this instance’s API automatically republishes it, so use the live API only when intending to publish the change. Existing executions retain their old workflow logic; notification improvements apply to new runs. The app/Railway deployment is separate: the new image configures completed-audio sharing and provides the YouTube runtime, but successful peer uploads and YouTube extraction still require functioning network access on the deployment host.

If YouTube rejects the download host with a bot/sign-in check, configure a working proxy supplied by your provider as the private Railway variable `YOUTUBE_PROXY` (for example `http://USER:PASSWORD@HOST:PORT` or `socks5://USER:PASSWORD@HOST:PORT`). Redeploy an image containing this setting's support. All YouTube search, metadata, and audio requests use the proxy; Soulseek and dashboard traffic do not. Percent-encode special characters in credentials. Do not put real proxy credentials in this repository. A proxy does not guarantee access; confirm a real recovery after configuration.

To use the Pi's internet connection instead, install Tailscale on the Pi, enable IP forwarding, advertise it with `sudo tailscale up --accept-dns=false --advertise-exit-node`, and approve it as an exit node in Tailscale's Machines page. In the Railway dashboard service, set `YOUTUBE_EXIT_NODE` to the Pi's Tailscale IPv4 address and `TS_AUTHKEY` to an auth key from the same tailnet. A one-use, pre-approved, non-ephemeral key works with the persisted state volume; create a fresh key if that state is replaced. Deploy the updated Railway image. Startup runs Tailscale in userspace with a localhost-only SOCKS proxy and sets `YOUTUBE_PROXY` automatically. Downloads stay on Railway, while YouTube requests exit through the Pi. Keep replicas at one; the Tailscale identity is stored privately beside the app state. Keep the Pi online and manage device key expiry in Tailscale to prevent authentication interruptions. Exit-node configuration takes precedence over a separately configured `YOUTUBE_PROXY`.

## Public workflow template

`after.json` is a sanitized, reusable workflow template. Set the `slskd.example.com` and `playlist.example.com` origins, bind the named credential references to your own n8n credentials, replace `YOUR_TELEGRAM_CHAT_ID`, and replace the manual `YOUR_PLAYLIST_ID` example before using it. Its webhook path is `playlist-import`; configure your dashboard webhook URL to match the imported workflow. Credential IDs, private service origins, the original webhook identifiers, and the original sample playlist have been removed. This template is not a drop-in replacement for an existing configured production workflow.

The original configured export is kept locally in `.private-workflows/after.live.json` when sanitizing; that directory, `.env` variants, and common workflow backup files are ignored by Git. The sanitizer does not alter the running n8n workflow. To update an existing workflow, patch a fresh private export and replace it through the API; importing onto an occupied editor canvas appends nodes and can produce duplicate graphs.
