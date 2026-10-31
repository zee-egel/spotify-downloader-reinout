import base64
import hashlib
import hmac
import html
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import profiles
from audio_metadata import spotify_metadata, tag_audio
from views import render_html, client_templates_json

ROOT = Path(os.environ.get('DOWNLOADS_ROOT', './downloads')).resolve()
STATE = Path(os.environ.get('STATE_FILE', './state.json'))
PORT = int(os.environ.get('PORT', '8080'))
STATE_LOCK = threading.Lock()
YOUTUBE_LOCK = threading.Lock()


def playlist_id(value):
    value = value.strip()
    match = re.fullmatch(r'spotify:playlist:([A-Za-z0-9]{22})', value)
    if not match:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme != 'https' or parsed.hostname not in ('open.spotify.com', 'play.spotify.com'):
            return None
        match = re.fullmatch(r'/playlist/([A-Za-z0-9]{22})/?', parsed.path)
    return match.group(1) if match else None


def inside(relative, root=None):
    root = ROOT if root is None else root
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError('Dit pad ligt buiten de downloadmap')
    return target


def request_json(url, token=None, data=None):
    headers = {'Accept': 'application/json'}
    if token:
        headers['X-API-Key'] = token
    if data is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, json.dumps(data).encode() if data is not None else None, headers)
    with urllib.request.urlopen(req, timeout=12) as response:
        return json.load(response)


def read_runs():
    try:
        return json.loads(STATE.read_text())
    except FileNotFoundError:
        return []


def save_runs(runs):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(runs))
    temp.replace(STATE)


def transfer_kind(state, error=''):
    if isinstance(state, int):
        if state & 16:
            return 'completed' if state & 32 else 'failed'
        return 'unknown'
    value = str(state).lower()
    if 'completed' in value and 'succeeded' in value:
        return 'completed'
    if 'queued' in value and 'remote' in value:
        return 'queued remotely'
    if any(word in value for word in ('queue', 'wait', 'pending')):
        return 'queued locally'
    if any(word in value for word in ('download', 'transfer', 'initializ', 'inprogress', 'requested')):
        return 'downloading'
    if error or any(word in value for word in ('error', 'fail', 'reject', 'cancel', 'timeout')):
        return 'failed'
    return 'unknown'


def transfer_snapshot():
    slskd = os.environ.get('SLSKD_URL', '').rstrip('/')
    if not slskd:
        return [], 'Stel SLSKD_URL in om downloads te tonen.'
    try:
        users = request_json(slskd + '/api/v0/transfers/downloads?includeRemoved=true', os.environ.get('SLSKD_API_KEY'))
    except (urllib.error.URLError, ValueError, TimeoutError) as error:
        return [], 'slskd is momenteel niet beschikbaar.' if not isinstance(error, urllib.error.HTTPError) or error.code not in (401, 403) else 'slskd heeft de API-sleutel geweigerd.'
    rows = []
    for user in users if isinstance(users, list) else []:
        for group in user.get('directories', []) if isinstance(user, dict) else []:
            for file in group.get('files', []) if isinstance(group, dict) else []:
                if not isinstance(file, dict):
                    continue
                try:
                    size = max(0, float(file.get('size') or 0))
                    done = max(0, float(file.get('bytesTransferred') or 0))
                    speed = max(0, float(file.get('averageSpeed') or 0))
                    percent = float(file['percentComplete']) if file.get('percentComplete') is not None else (done / size * 100 if size else 0)
                except (TypeError, ValueError):
                    size = done = speed = percent = 0
                raw_state = file.get('state')
                state = str(raw_state if raw_state is not None else 'Unknown')
                error = str(file.get('error') or file.get('failureReason') or '')
                rows.append({'id': str(file.get('id') or ''), 'batchId': str(file.get('batchId') or ''),
                    'name': str(file.get('filename') or 'Onbekend bestand').replace('\\', '/').split('/')[-1],
                    'filename': str(file.get('filename') or ''), 'folder': str(group.get('directory') or ''), 'username': str(user.get('username') or ''),
                    'localFilename': str(file.get('localFilename') or ''),
                    'state': state, 'kind': transfer_kind(raw_state, error), 'error': error,
                    'size': size, 'done': done, 'speed': speed, 'percent': max(0, min(100, percent)),
                    'date': str(file.get('requestedAt') or file.get('queuedAt') or file.get('startedAt') or ''), 'order': len(rows)})
    return rows, None


def tag_completed_downloads():
    """Tag completed Soulseek selections without needing an open dashboard."""
    with STATE_LOCK:
        runs = read_runs()
    if not any(run.get('spotifyMetadata') for run in runs):
        return
    transfers, error = transfer_snapshot()
    if error:
        return
    for run in runs:
        state = run.get('workflow') or {}
        records = {}
        for track in (state.get('tracks') or []) + (state.get('downloads') or []):
            if track.get('spotifyId'):
                records.setdefault(track['spotifyId'], {}).update(track)
        owner = run.get('owner', 'owner')
        root = ROOT if owner == 'owner' else ROOT / 'profiles' / owner
        if owner not in ('owner', 'extra', 'extra2') or root.is_symlink() or (ROOT / 'profiles').is_symlink():
            continue
        for ident, track in records.items():
            metadata = (run.get('spotifyMetadata') or {}).get(ident)
            if not metadata or not track.get('username') or not track.get('filename') or track.get('youtubeStatus') == 'completed':
                continue
            selected = [f for f in transfers if f['kind'] == 'completed'
                        and f['username'] == track['username'] and f['filename'] == track['filename']
                        and (not track.get('batchId') or f['batchId'] == track['batchId'])
                        and (not f['date'] or f['date'] >= run.get('submitted', ''))]
            if len(selected) != 1:
                continue
            transfer = selected[0]
            key = ident + ':' + (transfer['id'] or transfer['batchId'] or track['filename'])
            previous = (run.get('tagging') or {}).get(key, {})
            if previous.get('status') in ('tagged', 'skipped') or previous.get('attempts', 0) >= 3 or time.time() < previous.get('retryAt', 0):
                continue
            local = transfer['localFilename']
            if local:
                local_path = Path(local)
                if local_path.is_absolute():
                    try:
                        relative = local_path.relative_to(Path(os.environ.get('SLSKD_DOWNLOADS_DIR', str(ROOT))))
                    except ValueError:
                        # Separate containers can expose the same volume at different paths.
                        try:
                            relative = local_path.relative_to('/downloads')
                        except ValueError:
                            continue
                    target = ROOT / relative
                else:
                    target = ROOT / local_path
            else:
                folder = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', state.get('playlistName') or run.get('name') or '').replace('..', '_').strip()
                if not folder:
                    continue
                target = root / folder / track['filename'].replace('\\', '/').split('/')[-1]
            if not target.resolve().is_relative_to(root.resolve()) or target.is_symlink() or not target.is_file():
                continue
            # A basename fallback cannot distinguish slskd's renamed duplicate files.
            if not local and target.stat().st_size not in (transfer['size'], previous.get('fileSize')):
                continue
            _, result = tag_audio(target, metadata)
            result.update(attempts=previous.get('attempts', 0) + 1, retryAt=time.time() + 60,
                          filename=str(target.relative_to(ROOT)), fileSize=target.stat().st_size)
            with STATE_LOCK:
                current = read_runs()
                match = next((r for r in current if r.get('submissionId') == run.get('submissionId') and r.get('submissionId')), None)
                if match:
                    match.setdefault('tagging', {})[key] = result
                    save_runs(current)


def metadata_worker():
    while True:
        try:
            tag_completed_downloads()
        except Exception:
            import logging
            logging.exception('Completed-download metadata scan failed')
        time.sleep(10)


def run_view(run, transfers):
    state = run.get('workflow') or {}
    by_batch = {}
    for transfer in transfers:
        by_batch.setdefault(transfer['batchId'], []).append(transfer)
    # Metadata arrives first; queue results and final summaries enrich the same tracks.
    records = [dict(track) for track in state.get('tracks') or []]
    by_id = {track['spotifyId']: track for track in records if track.get('spotifyId')}
    by_url = {track['spotifyUrl']: track for track in records if track.get('spotifyUrl')}
    by_title = {(track.get('artist'), track['title']): track for track in records if track.get('title')}
    for key in ('downloads', 'problems'):
        for result in state.get(key) or []:
            existing = by_id.get(result['spotifyId']) if result.get('spotifyId') else by_url.get(result.get('spotifyUrl')) or by_title.get((result.get('artist'), result.get('title')))
            if existing is None:
                existing = {}
                records.append(existing)
            existing.update(result, problem=key == 'problems')
            if existing.get('spotifyId'):
                by_id[existing['spotifyId']] = existing
            if existing.get('spotifyUrl'):
                by_url[existing['spotifyUrl']] = existing
            if existing.get('title'):
                by_title[(existing.get('artist'), existing['title'])] = existing
    tracks = []
    by_file = {}
    for transfer in transfers:
        by_file.setdefault((transfer.get('username'), transfer.get('filename')), []).append(transfer)
    for track in records:
        batch = str(track.get('batchId') or '')
        files = by_batch.get(batch, []) if batch else []
        # Match the selected file when the batch response differs from the transfer listing.
        if not files and track.get('username') and track.get('filename') and track.get('youtubeStatus') != 'completed':
            files = by_file.get((track.get('username'), track.get('filename')), [])
            files = [file for file in files if not file.get('date') or file['date'] >= (run.get('submitted') or '')]
        kinds = [file['kind'] for file in files]
        status = track.get('status') or 'unknown'
        status = {'error': 'search error', 'search_timeout': 'search timeout', 'queued': 'queued locally',
                  'no_match': 'no match', 'review': 'needs review'}.get(status, status)
        if not track.get('problem'):
            status = next((kind for kind in ('completed', 'downloading', 'queued remotely', 'queued locally', 'failed') if kind in kinds), status)
        if track.get('fallbackStatus') == 'queued' and status == 'unknown':
            status = 'trying another source'
        youtube = (run.get('youtubeProgress') or {}).get(track.get('spotifyId')) or {}
        if track.get('youtubeAttempted'):
            status = 'completed' if track.get('youtubeStatus') == 'completed' else 'unavailable'
        if youtube and not (youtube.get('status') == 'youtube waiting' and track.get('youtubeAttempted')):
            status = youtube['status']
        if state.get('phase') == 'final' and status not in ('completed', 'already/duplicate'):
            status = 'unavailable'
        tracks.append({'batchId': batch or next((file['batchId'] for file in files), ''),
            'spotifyId': track.get('spotifyId') or '', 'artist': track.get('artist') or '', 'title': track.get('title') or '',
            'status': status, 'reason': (((track.get('youtubeResult') or {}).get('reason') or track.get('reason')) if track.get('youtubeAttempted') else '') or youtube.get('reason') or (track.get('youtubeResult') or {}).get('reason') or track.get('reason') or '',
            'fallback': bool(track.get('fallbackAttempted') or youtube),
            'downloadSource': ('youtube' if youtube or track.get('youtubeStatus') == 'completed' or (track.get('fallbackSource') == 'YouTube' and not batch) else 'soulseek' if batch or track.get('username') else '') if status == 'completed' else '',
            'source': 'YouTube' if youtube else track.get('fallbackSource') if track.get('fallbackAttempted') else next((f['username'] for f in files), track.get('username') or ''),
            'percent': youtube.get('percent') if youtube else max((file['percent'] for file in files), default=0)})
    counts = {kind: sum(track['status'] == kind for track in tracks) for kind in ('completed', 'downloading', 'queued locally', 'queued remotely', 'failed', 'search timeout', 'search error', 'unknown', 'unavailable', 'already/duplicate')}
    counts['fallback'] = sum(track['fallback'] for track in tracks)
    counts['no match'] = sum(track['status'] == 'no match' for track in tracks)
    counts['needs review'] = sum(track['status'] == 'needs review' for track in tracks)
    handled = sum(track['status'] in ('completed', 'unavailable', 'already/duplicate') for track in tracks)
    total = state.get('totalSpotifyTracks')
    finished = state.get('phase') == 'final' or bool(total and handled >= total and not state.get('resolving'))
    return {'handled': total if finished and total is not None else handled, 'finished': finished, 'id': run.get('id'), 'submissionId': run.get('submissionId'), 'name': state.get('playlistName') or run.get('name'), 'submitted': run.get('submitted'),
        'phase': 'resolving' if state.get('resolving') else state.get('phase') or run.get('status') or 'submitted', 'total': state.get('totalSpotifyTracks'),
        'counts': counts, 'tracks': tracks}


def file_sort_key(path, sort):
    stat = path.stat()
    if sort == 'size':
        return stat.st_size if path.is_file() else 0
    if sort == 'modified':
        return stat.st_mtime
    if sort == 'type':
        return (0 if path.is_dir() else 1, path.suffix.casefold(), path.name.casefold())
    return path.name.casefold()


def safe_name(path):
    return html.escape(path.name)


def size_label(size):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024


def playlist_name(ident):
    url = 'https://open.spotify.com/oembed?' + urllib.parse.urlencode({'url': 'https://open.spotify.com/playlist/' + ident})
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Playlist desk/1.0'})
        with urllib.request.urlopen(req, timeout=4) as response:
            title = json.load(response).get('title')
        return title.strip() if isinstance(title, str) and title.strip() else None
    except (urllib.error.URLError, ValueError, TimeoutError):
        return None


def library_view(folder='', sort='name', direction='asc', root=None):
    root = ROOT if root is None else root
    sort = sort if sort in ('name', 'type', 'size', 'modified') else 'name'
    direction = direction if direction in ('asc', 'desc') else 'asc'
    sort_query = '&sort=' + sort + '&dir=' + direction
    try:
        directory = inside(folder, root)
        if not directory.is_dir():
            raise ValueError('Map niet gevonden')
        items = sorted((p for p in directory.iterdir() if p.resolve().is_relative_to(root) and not p.is_symlink()), key=lambda p: file_sort_key(p, sort), reverse=direction == 'desc')
        rows = []
        for item in items:
            rel = str(item.relative_to(root))
            quoted = urllib.parse.quote(rel)
            delete = render_html('delete-form', path=html.escape(rel, quote=True))
            if item.is_dir():
                count = sum(1 for child in item.iterdir() if not child.is_symlink() and child.resolve().is_relative_to(root))
                rows.append(render_html(
                    'library-folder',
                    rel=html.escape(rel, quote=True),
                    folder_path=quoted,
                    sort_query=sort_query,
                    name=safe_name(item),
                    count=count,
                    plural="s" if count != 1 else "",
                    modified=datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d"),
                    delete=delete,
                ))
            elif item.is_file():
                rows.append(render_html(
                    'library-file',
                    rel=html.escape(rel, quote=True),
                    name=safe_name(item),
                    size=size_label(item.stat().st_size),
                    modified=datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d"),
                    quoted=quoted,
                    delete=delete,
                ))
        return {'html': ''.join(rows) or render_html('library-empty'), 'error': None}
    except (ValueError, OSError):
        return {'html': None, 'error': 'Deze map is niet beschikbaar. Hij is mogelijk verplaatst of verwijderd.'}


def page(body, title='The Disc Situation'):
    is_profile = 'class="profile-page"' in body
    return render_html(
        'base',
        title=html.escape(title),
        profile='page' if is_profile else 'false',
        connection_status='Persoonlijke instellingen' if is_profile else 'Verbinden…',
        body=body,
        ui_templates=client_templates_json(),
    ).encode()


class Handler(BaseHTTPRequestHandler):
    def log_request(self, code='-', size='-'):
        # OAuth query strings contain one-time codes; never write them to access logs.
        self.log_message('%s %s %s', self.command, self.path.split('?')[0], code)

    def runs(self):
        return [run for run in read_runs() if run.get('owner', 'owner') == self.user and not run.get('hidden')]

    def download_root(self):
        if self.user == 'owner':
            return ROOT
        target = ROOT / 'profiles' / self.user
        if (ROOT / 'profiles').is_symlink() or target.is_symlink():
            raise ValueError('De downloadmap van dit profiel is niet beschikbaar')
        target.mkdir(parents=True, exist_ok=True)
        return target

    def transfers(self):
        transfers, error = transfer_snapshot()
        runs = read_runs()
        batches = {}
        selections = {}
        for run in runs:
            state = run.get('workflow') or {}
            for track in (state.get('tracks') or []) + (state.get('downloads') or []):
                if track.get('batchId'):
                    batches.setdefault(track['batchId'], set()).add(run.get('owner', 'owner'))
                if track.get('username') and track.get('filename') and track.get('youtubeStatus') != 'completed':
                    selections.setdefault((track['username'], track['filename']), []).append(run)
        visible = []
        for transfer in transfers:
            owners = batches.get(transfer['batchId'], set()).copy()
            for run in selections.get((transfer['username'], transfer['filename']), []):
                if transfer.get('date') and transfer['date'] >= run['submitted']:
                    owners.add(run.get('owner', 'owner'))
            if self.user == 'owner' or self.user in owners:
                visible.append(transfer)
        return visible, error

    def redirect(self, url):
        self.send_response(303)
        self.send_header('Location', url)
        self.end_headers()

    def profile_action(self, form):
        if self.path == '/spotify/connect':
            self.redirect(profiles.spotify_start(self.user))
            return
        if self.path == '/spotify/disconnect':
            profiles.update(self.user, spotify=None, oauth=None)
        elif self.path == '/profile/telegram':
            profiles.save_telegram(self.user, form.get('token', [''])[0], form.get('chat', [''])[0].strip())
        elif self.path == '/profile/telegram-test':
            settings = profiles.load(self.user).get('telegram') or {}
            profiles.telegram(self.user, 'sendMessage', {'chat_id': settings.get('chat'), 'text': 'Je bot is gekoppeld aan The Disc Situation.'})
        elif self.path == '/profile/telegram-disconnect':
            profiles.update(self.user, telegram=None)
        self.redirect('/profile')

    def workflow_notify(self):
        expected = os.environ.get('N8N_WEBHOOK_TOKEN', '')
        if not expected or not hmac.compare_digest(self.headers.get('X-Playlist-Token', ''), expected):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                raise ValueError('Invalid notification size')
            payload = json.loads(self.rfile.read(length))
            message = payload.get('message')
            if not isinstance(message, str) or not 0 < len(message) <= 4096:
                raise ValueError('Invalid notification')
            with STATE_LOCK:
                runs = read_runs()
                run = next((r for r in runs if r.get('submissionId') and r['submissionId'] == payload.get('submissionId')), None)
                if not run:
                    self.send_error(404)
                    return
                owner = run.get('owner', 'owner')
                settings = profiles.load(owner).get('telegram') or {}
                digest = hashlib.sha256(message.encode()).hexdigest()
                if settings and digest not in run.get('notifications', []):
                    profiles.telegram(owner, 'sendMessage', {'chat_id': settings['chat'], 'text': message})
                    run.setdefault('notifications', []).append(digest)
                    save_runs(runs)
            self.send(b'{}', kind='application/json')
        except (ValueError, TypeError, urllib.error.URLError, TimeoutError, KeyError):
            self.send(b'{"error":"Notification could not be delivered"}', kind='application/json', status=502)

    def auth(self):
        try:
            accounts = profiles.accounts()
        except ValueError:
            self.send_error(503, 'Accountnamen moeten verschillen')
            return False
        if not os.environ.get('APP_PASSWORD'):
            self.send_error(503, 'Stel APP_PASSWORD in voordat je de app start')
            return False
        header = self.headers.get('Authorization', '')
        try:
            scheme, value = header.split(' ', 1)
            credentials = base64.b64decode(value, validate=True).decode()
            user, password = credentials.split(':', 1)
            account = accounts.get(user)
            ok = scheme.lower() == 'basic' and bool(account and account[1]) and hmac.compare_digest(password.encode(), account[1].encode())
            if ok:
                self.user, self.username = account[0], user
        except (ValueError, UnicodeError):
            ok = False
        if not ok:
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="The Disc Situation"')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
        return ok

    def send(self, data, kind='text/html; charset=utf-8', status=200, filename=None):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Cache-Control', 'no-store')
        if filename:
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path == '/youtube-download':
            self.youtube_download()
            return
        if self.path in ('/workflow-status', '/workflow-notify'):
            self.workflow_status() if self.path == '/workflow-status' else self.workflow_notify()
            return
        if not self.auth():
            return
        if self.path not in ('/submit', '/clear-history', '/delete', '/spotify/connect', '/spotify/disconnect', '/profile/telegram', '/profile/telegram-test', '/profile/telegram-disconnect'):
            self.send_error(404)
            return
        origin = self.headers.get('Origin')
        host = self.headers.get('Host', '')
        if (origin and urllib.parse.urlparse(origin).netloc != host) or (self.path != '/submit' and not origin):
            self.send_error(403, 'Ongeldige herkomst')
            return
        try:
            submission_id = None
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= 4096:
                raise ValueError('Invoer te groot')
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
            if self.path.startswith(('/profile/', '/spotify/')):
                self.profile_action(form)
                return
            if self.path == '/clear-history':
                with STATE_LOCK:
                    runs = read_runs()
                    for run in runs:
                        if run.get('owner', 'owner') == self.user:
                            run['hidden'] = True
                    save_runs(runs)
                self.send_response(303)
                self.send_header('Location', '/#activity')
                self.end_headers()
                return
            if self.path == '/delete':
                relative = form.get('path', [''])[0]
                parts = Path(relative)
                target = inside(relative, self.download_root())
                if not relative or parts.is_absolute() or '..' in parts.parts or target == self.download_root() or target == ROOT / 'profiles':
                    raise ValueError('Ongeldig pad om te verwijderen')
                check = self.download_root()
                for part in parts.parts:
                    check /= part
                    if check.is_symlink():
                        raise ValueError('Symbolische links kunnen hier niet worden verwijderd')
                if target.is_dir():
                    if not shutil.rmtree.avoids_symlink_attacks:
                        raise ValueError('Veilig verwijderen van mappen is niet beschikbaar')
                    shutil.rmtree(target)
                elif target.is_file():
                    target.unlink()
                else:
                    raise ValueError('Bestand of map niet gevonden')
                parent = str(parts.parent)
                self.send_response(303)
                self.send_header('Location', '/?folder=' + urllib.parse.quote('' if parent == '.' else parent) + '#files')
                self.end_headers()
                return
            url = form.get('url', [''])[0].strip()
            ident = playlist_id(url)
            if not ident:
                raise ValueError('Vul een Spotify-playlistlink of -URI in')
            webhook = os.environ.get('N8N_WEBHOOK_URL', '')
            if not webhook:
                raise ValueError('De downloaddienst is nog niet ingesteld.')
            headers = {'Content-Type': 'application/json'}
            if os.environ.get('N8N_WEBHOOK_TOKEN'):
                headers['X-Webhook-Token'] = os.environ['N8N_WEBHOOK_TOKEN']
                headers['X-Playlist-Token'] = os.environ['N8N_WEBHOOK_TOKEN']
            playlist, items = None, None
            if os.environ.get('SPOTIFY_CLIENT_ID') or self.user != 'owner':
                playlist, items = profiles.spotify_playlist(self.user, ident)
            submission_id = uuid.uuid4().hex
            with STATE_LOCK:
                runs = read_runs()
                # ponytail: one app replica and a five-minute callback lease; use a durable queue for multiple replicas.
                if len(profiles.accounts()) > 1 and any(
                    run.get('status') in ('submitting', 'submitted', 'unconfirmed')
                    and ((run.get('workflow') or {}).get('resolving') or not run.get('workflow'))
                    and time.time() - datetime.fromisoformat(run.get('updated') or run['submitted']).timestamp() < 300
                    for run in runs):
                    raise ValueError('Er wordt al een andere playlist uitgelezen. Probeer het opnieuw zodra die in de wachtrij staat.')
                runs.insert(0, {'id': ident, 'submissionId': submission_id, 'name': 'Spotify-playlist ' + ident, 'url': url, 'submitted': datetime.now(timezone.utc).isoformat(), 'status': 'submitting', 'owner': self.user, 'spotifyMetadata': spotify_metadata(items)})
                save_runs(runs)
            req = urllib.request.Request(webhook, json.dumps({'url': url, 'playlistId': ident, 'submissionId': submission_id, 'profileId': self.user, 'downloadDirectory': '' if self.user == 'owner' else 'profiles/' + self.user, 'playlist': playlist, 'playlistItems': items}).encode(), headers)
            try:
                with urllib.request.urlopen(req, timeout=15) as response:
                    if response.status >= 300:
                        raise ValueError('De workflow heeft de import geweigerd')
            except (urllib.error.URLError, TimeoutError) as error:
                # Keep uncertain submissions: the workflow may already be running.
                with STATE_LOCK:
                    runs = read_runs()
                    if isinstance(error, urllib.error.HTTPError) and error.code in (400, 401, 403, 404):
                        runs = [run for run in runs if run.get('submissionId') != submission_id]
                    else:
                        for run in runs:
                            if run.get('submissionId') == submission_id:
                                run['status'] = 'unconfirmed'
                    save_runs(runs)
                raise
            name = (playlist or {}).get('name') or playlist_name(ident) or 'Spotify-playlist ' + ident
            with STATE_LOCK:
                runs = read_runs()
                for run in runs:
                    if run.get('submissionId') == submission_id:
                        run.update(name=name, status='submitted')
                save_runs(runs)
            self.send_response(303)
            self.send_header('Location', '/')
            self.end_headers()
        except urllib.error.HTTPError as error:
            if self.path.startswith(('/profile/', '/spotify/')) or (self.path == '/submit' and os.environ.get('SPOTIFY_CLIENT_ID') and not submission_id):
                self.send(page(render_html('service-error')), status=502)
                return
            self.log_error('n8n webhook returned HTTP %d', error.code)
            message = 'n8n heeft de webhook-inloggegevens geweigerd (403); controleer N8N_WEBHOOK_TOKEN' if error.code == 403 else f'De n8n-webhook gaf HTTP {error.code} terug; controleer N8N_WEBHOOK_URL'
            self.send(page(render_html('submit-error', details=html.escape(message))), status=502)
        except (ValueError, urllib.error.URLError, TimeoutError) as error:
            message = 'Verbinding verbroken. Controleer je recente imports voordat je de playlist opnieuw indient.' if isinstance(error, (urllib.error.URLError, TimeoutError)) else str(error)
            self.send(page(render_html('error-page', message=html.escape(message))), status=400)
        except OSError as error:
            self.log_error('file operation failed: %s', error)
            self.send(page(render_html('delete-error')), status=500)

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path == '/favicon.ico':
            self.send(Path(__file__).with_name('favicon.ico').read_bytes(), kind='image/vnd.microsoft.icon')
            return
        if self.path == '/health':
            try:
                with urllib.request.urlopen(os.environ.get('SLSKD_URL', '').rstrip('/') + '/health', timeout=3) as response:
                    self.send(b'OK', kind='text/plain', status=200 if response.status == 200 else 503)
            except (ValueError, OSError):
                self.send(b'Download service unavailable', kind='text/plain', status=503)
            return
        if not self.auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ('/ui.css', '/ui.js', '/profile.js'):
            asset = Path(__file__).with_name(parsed.path[1:])
            kind = 'text/css' if asset.suffix == '.css' else 'text/javascript'
            self.send(asset.read_bytes(), kind=kind + '; charset=utf-8')
        elif parsed.path == '/profile':
            self.send(page(profiles.settings_html(self.user, self.username)))
        elif parsed.path == '/spotify/callback':
            try:
                profiles.spotify_finish(self.user, urllib.parse.parse_qs(parsed.query))
                self.redirect('/profile')
            except (ValueError, urllib.error.URLError, TimeoutError, KeyError):
                self.send(page(render_html('spotify-error')), status=400)
        elif parsed.path in ('/file', '/zip'):
            self.download(parsed)
        elif parsed.path == '/transfers':
            self.send(self.transfer_panel().encode(), kind='text/html; charset=utf-8')
        elif parsed.path == '/events':
            self.events()
        elif parsed.path == '/':
            self.dashboard()
        else:
            self.send_error(404)

    def youtube_download(self):
        expected = os.environ.get('N8N_WEBHOOK_TOKEN', '')
        if not expected or not hmac.compare_digest(self.headers.get('X-Playlist-Token', ''), expected):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536:
                raise ValueError('Invalid request size')
            payload = json.loads(self.rfile.read(length))
            from youtube_fallback import main, validate
            if not isinstance(payload, dict) or payload.get('profileId', 'owner') not in ('owner', 'extra', 'extra2'):
                raise ValueError('Invalid profile')
            validate(payload.get('track'))
            with STATE_LOCK:
                run = next((r for r in read_runs() if r.get('submissionId') == payload.get('submissionId')
                            and r.get('submissionId') and r.get('owner', 'owner') == payload.get('profileId', 'owner')), None)
                if run:
                    payload['track'].update((run.get('spotifyMetadata') or {}).get(payload['track']['spotifyId'], {}))
        except (ValueError, TypeError, UnicodeError):
            self.send(b'{"status":"failed","reason":"Invalid YouTube request"}', kind='application/json', status=400)
            return
        last_update = [0, None]
        def report(**update):
            now = time.monotonic()
            if update['status'] == last_update[1] and now - last_update[0] < 1:
                return
            with STATE_LOCK:
                runs = read_runs()
                run = next((r for r in runs if r.get('submissionId') == payload.get('submissionId')
                            and r.get('submissionId') and r.get('owner', 'owner') == payload.get('profileId', 'owner')), None)
                if run:
                    run.setdefault('youtubeProgress', {})[payload['track']['spotifyId']] = update
                    save_runs(runs)
            last_update[:] = [now, update['status']]
        # ponytail: one download at a time; move to a durable worker if more accounts need it.
        if not YOUTUBE_LOCK.acquire(blocking=False):
            report(status='youtube waiting', percent=None, reason='Wachten op de actieve YouTube-download. Het zoeken wordt binnenkort opnieuw geprobeerd.', source='YouTube')
            self.send(b'{"status":"failed","reason":"YouTube downloader busy; waiting to retry","retryable":true,"retryAfterSeconds":60}', kind='application/json', status=409)
            return
        try:
            result = main(urllib.parse.quote(json.dumps(payload)), progress=report)
            report(status='completed' if result['status'] == 'completed' else 'unavailable',
                   percent=100 if result['status'] == 'completed' else None,
                   reason=result.get('reason', ''), source='YouTube', diagnostics=result.get('diagnostics', []), code=result.get('code', ''))
        finally:
            YOUTUBE_LOCK.release()
        self.send(json.dumps(result).encode(), kind='application/json')

    def download(self, parsed):
        try:
            relative = urllib.parse.parse_qs(parsed.query).get('path', [''])[0]
            if not relative:
                raise ValueError('Pad ontbreekt')
            target = inside(relative, self.download_root())
            if not target.exists():
                raise FileNotFoundError(relative)
            if parsed.path == '/file':
                if not target.is_file():
                    raise ValueError('Dit is geen bestand')
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(target.name))
                self.send_header('Content-Length', str(target.stat().st_size))
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                with target.open('rb') as source:
                    while chunk := source.read(1024 * 256):
                        self.wfile.write(chunk)
            else:
                if not target.is_dir():
                    raise ValueError('Dit is geen map')
                # ponytail: temporary disk space buys an exact size for native browser progress.
                with tempfile.TemporaryFile() as source:
                    try:
                        # Audio is already compressed; storing it makes ZIP preparation faster.
                        with zipfile.ZipFile(source, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                            for base, dirs, files in os.walk(target):
                                dirs[:] = [d for d in dirs if inside(str((Path(base) / d).relative_to(self.download_root())), self.download_root()).is_dir()]
                                for name in files:
                                    file = Path(base) / name
                                    if file.is_file() and file.resolve().is_relative_to(self.download_root()):
                                        archive.write(file, file.relative_to(target))
                    except OSError:
                        self.send_error(503, 'Het ZIP-bestand kon niet worden gemaakt. Probeer het later opnieuw.')
                        return
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/zip')
                    self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(target.name + '.zip'))
                    self.send_header('Content-Length', str(source.tell()))
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    source.seek(0)
                    shutil.copyfileobj(source, self.wfile, length=256 * 1024)
        except (ValueError, FileNotFoundError) as error:
            self.send(page(render_html('error', message=html.escape(str(error)))), status=400)

    def workflow_status(self):
        expected = os.environ.get('N8N_WEBHOOK_TOKEN', '')
        if not expected or not hmac.compare_digest(self.headers.get('X-Playlist-Token', ''), expected):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2 * 1024 * 1024:
                raise ValueError('Invalid status size')
            payload = json.loads(self.rfile.read(length))
            ident = payload.get('playlistId') if isinstance(payload, dict) else None
            ident = ident if isinstance(ident, str) and re.fullmatch(r'[A-Za-z0-9]{22}', ident) else None
            if not ident or not isinstance(payload.get('state'), dict):
                raise ValueError('Invalid workflow status')
            state = payload['state']
            clean = {key: state[key] for key in ('playlistName', 'totalSpotifyTracks', 'queuedDownloads', 'downloads', 'problems', 'completedFiles', 'failedFiles', 'pendingFiles', 'missingBatches', 'fallbackAttempts', 'timedOut', 'noMatch', 'reviewRequired', 'errors', 'resolving') if key in state}
            def clean_track(track):
                if not isinstance(track, dict) or not isinstance(track.get('spotifyId'), str) or not track['spotifyId']:
                    raise ValueError('Invalid track update')
                result = {}
                for key in ('spotifyId', 'spotifyUrl', 'artist', 'title', 'status', 'username', 'filename', 'batchId', 'reason', 'youtubeStatus', 'fallbackSource', 'album', 'albumArtist', 'releaseDate', 'coverUrl'):
                    if key in track:
                        if track[key] is not None and not isinstance(track[key], str):
                            raise ValueError('Invalid track field')
                        result[key] = track[key] or ''
                for key in ('trackNumber', 'discNumber'):
                    if key in track:
                        if track[key] is not None and (type(track[key]) is not int or track[key] < 1):
                            raise ValueError('Invalid track number')
                        result[key] = track[key]
                for key in ('youtubeAttempted', 'fallbackAttempted'):
                    if key in track:
                        result[key] = bool(track[key])
                return result
            if 'tracks' in state:
                if not isinstance(state['tracks'], list):
                    raise ValueError('Invalid tracks')
                clean['tracks'] = [clean_track(track) for track in state['tracks']]
            track_update = clean_track(state['track']) if 'track' in state else None
            if 'downloads' in state:
                clean['resolving'] = False
            clean['phase'] = payload.get('phase') if payload.get('phase') in ('queued', 'progress', 'final') else 'progress'
            with STATE_LOCK:
                runs = read_runs()
                run = next((item for item in runs if item.get('id') == ident and item.get('submissionId') == payload.get('submissionId') and item.get('submissionId')), None)
                if run:
                    for track in clean.get('tracks', []) + ([track_update] if track_update else []):
                        if track.get('coverUrl') or track.get('album'):
                            metadata = {key: track[key] for key in ('title', 'artist', 'album', 'albumArtist', 'releaseDate', 'coverUrl', 'trackNumber', 'discNumber', 'spotifyUrl') if key in track}
                            run.setdefault('spotifyMetadata', {}).setdefault(track['spotifyId'], metadata)
                    phases = {'queued': 0, 'progress': 1, 'final': 2}
                    previous = run.get('workflow') or {}
                    if phases[clean['phase']] >= phases.get(previous.get('phase'), -1) and (clean.get('fallbackAttempts', previous.get('fallbackAttempts')) or 0) >= (previous.get('fallbackAttempts') or 0):
                        if track_update:
                            tracks = [dict(track) for track in previous.get('tracks') or []]
                            matches = [track for track in tracks if track['spotifyId'] == track_update['spotifyId']]
                            for track in matches:
                                track.update(track_update)
                            if not matches:
                                tracks.append(track_update)
                            clean['tracks'] = tracks
                        run['workflow'] = {**previous, **clean}
                        run['updated'] = datetime.now(timezone.utc).isoformat()
                        save_runs(runs)
            self.send(b'{}', kind='application/json')
        except (ValueError, TypeError) as error:
            self.send(page(render_html('error', message=html.escape(str(error)))), status=400)

    def transfer_panel(self):
        transfers, error = self.transfers()
        if error:
            return render_html('empty', message=html.escape(error))
        rows = []
        for file in transfers[:80]:
            label = html.escape(file['name'])
            detail = f"{size_label(file['done'])} van {size_label(file['size'])}"
            if file['speed']:
                detail += f" · {size_label(file['speed'])}/s"
            rows.append(render_html(
                'transfer-row',
                label=label,
                username=html.escape(file["username"]),
                folder=html.escape(file["folder"]),
                status=html.escape(file["state"]),
                percent=format(file["percent"], '.0f'),
                width=format(file["percent"], '.1f'),
                detail=html.escape(detail),
            ))
        return ''.join(rows) or render_html('transfers-empty')

    def events(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        folder, sort, direction = (query.get(key, [default])[0] for key, default in (('folder', ''), ('sort', 'name'), ('dir', 'asc')))
        previous = None
        try:
            while True:
                transfers, error = self.transfers()
                data = json.dumps({'transfers': transfers, 'runs': [run_view(run, transfers) for run in self.runs()[:8]], 'error': error, 'library': library_view(folder, sort, direction, self.download_root())}, ensure_ascii=False)
                digest = hashlib.sha256(data.encode()).digest()
                self.wfile.write(b'data: ' + data.encode() + b'\n\n' if digest != previous else b': keepalive\n\n')
                self.wfile.flush()
                previous = digest
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def dashboard(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        folder = query.get('folder', [''])[0]
        sort = query.get('sort', ['name'])[0]
        sort = sort if sort in ('name', 'type', 'size', 'modified') else 'name'
        direction = query.get('dir', ['asc'])[0]
        direction = direction if direction in ('asc', 'desc') else 'asc'
        sort_query = '&sort=' + sort + '&dir=' + direction
        library = library_view(folder, sort, direction, self.download_root())
        crumbs = [render_html('library-root-crumb', sort_query=sort_query.lstrip('&'))]
        current = Path()
        for part in Path(folder).parts if folder else []:
            current /= part
            crumbs.append(render_html(
                'library-crumb',
                folder_path=urllib.parse.quote(str(current)),
                sort_query=sort_query,
                part=html.escape(part),
            ))
        runs = self.runs()[:8]
        missing = {(run['id'], run['submitted']): playlist_name(run['id']) or 'Spotify-playlist ' + run['id'] for run in runs if not run.get('name')}
        if missing:
            with STATE_LOCK:
                saved = read_runs()
                for run in saved:
                    key = (run['id'], run['submitted'])
                    if run.get('owner', 'owner') == self.user and not run.get('name') and key in missing:
                        run['name'] = missing[key]
                save_runs(saved)
            runs = self.runs()[:8]
        body = render_html('import')
        body += render_html('downloads')
        body += render_html(
            'library',
            crumbs=''.join(crumbs),
            refresh_url=html.escape(self.path.split('#')[0], quote=True),
            folder=html.escape(folder, quote=True),
            sort_name=sort == 'name',
            sort_type=sort == 'type',
            sort_size=sort == 'size',
            sort_modified=sort == 'modified',
            direction_asc=direction == 'asc',
            direction_desc=direction == 'desc',
            files=library['html'] or '',
        )
        initial = json.dumps({'runs': [run_view(run, []) for run in runs], 'transfers': [], 'error': None, 'library': library}).replace('<', '\\u003c')
        body += render_html('initial-state', state=initial)
        body += render_html('script', source='/ui.js')
        self.send(page(body))

if __name__ == '__main__':
    assert playlist_id('spotify:playlist:37i9dQZF1DXcBWIGoYBM5M') == '37i9dQZF1DXcBWIGoYBM5M'
    assert playlist_id('https://evil.example/playlist/37i9dQZF1DXcBWIGoYBM5M') is None
    ROOT.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=metadata_worker, daemon=True, name='audio-metadata').start()
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
