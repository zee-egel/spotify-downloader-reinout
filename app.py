import base64
import hashlib
import hmac
import html
import json
import os
import re
import shutil
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

ROOT = Path(os.environ.get('DOWNLOADS_ROOT', './downloads')).resolve()
STATE = Path(os.environ.get('STATE_FILE', './state.json'))
PORT = int(os.environ.get('PORT', '8080'))
STATE_LOCK = threading.Lock()


def playlist_id(value):
    value = value.strip()
    match = re.fullmatch(r'spotify:playlist:([A-Za-z0-9]{22})', value)
    if not match:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme != 'https' or parsed.hostname not in ('open.spotify.com', 'play.spotify.com'):
            return None
        match = re.fullmatch(r'/playlist/([A-Za-z0-9]{22})/?', parsed.path)
    return match.group(1) if match else None


def inside(relative):
    target = (ROOT / relative).resolve()
    if not target.is_relative_to(ROOT):
        raise ValueError('Path outside downloads')
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
        return [], 'Set SLSKD_URL to show transfers.'
    try:
        users = request_json(slskd + '/api/v0/transfers/downloads?includeRemoved=true', os.environ.get('SLSKD_API_KEY'))
    except (urllib.error.URLError, ValueError, TimeoutError) as error:
        return [], 'slskd is unavailable right now.' if not isinstance(error, urllib.error.HTTPError) or error.code not in (401, 403) else 'slskd rejected the API key.'
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
                    'name': str(file.get('filename') or 'Unknown file').replace('\\', '/').split('/')[-1],
                    'filename': str(file.get('filename') or ''), 'folder': str(group.get('directory') or ''), 'username': str(user.get('username') or ''),
                    'state': state, 'kind': transfer_kind(raw_state, error), 'error': error,
                    'size': size, 'done': done, 'speed': speed, 'percent': max(0, min(100, percent)),
                    'date': str(file.get('requestedAt') or file.get('queuedAt') or file.get('startedAt') or ''), 'order': len(rows)})
    return rows, None


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
        # An exact selected peer + filename is known before slskd assigns the batch ID.
        if not batch and track.get('status') == 'matching':
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
        tracks.append({'batchId': batch or next((file['batchId'] for file in files), ''),
            'spotifyId': track.get('spotifyId') or '', 'artist': track.get('artist') or '', 'title': track.get('title') or '',
            'status': status, 'fallback': bool(track.get('fallbackAttempted')),
            'source': track.get('fallbackSource') if track.get('fallbackAttempted') else next((f['username'] for f in files), track.get('username') or ''),
            'percent': max((file['percent'] for file in files), default=0)})
    counts = {kind: sum(track['status'] == kind for track in tracks) for kind in ('completed', 'downloading', 'queued locally', 'queued remotely', 'failed', 'search timeout', 'search error', 'unknown')}
    counts['fallback'] = sum(track['fallback'] for track in tracks)
    counts['no match'] = max(state.get('noMatch') or 0, sum(track['status'] == 'no match' for track in tracks))
    counts['needs review'] = max(state.get('reviewRequired') or 0, sum(track['status'] == 'needs review' for track in tracks))
    return {'id': run.get('id'), 'submissionId': run.get('submissionId'), 'name': state.get('playlistName') or run.get('name'), 'submitted': run.get('submitted'),
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


def library_view(folder='', sort='name', direction='asc'):
    sort = sort if sort in ('name', 'type', 'size', 'modified') else 'name'
    direction = direction if direction in ('asc', 'desc') else 'asc'
    sort_query = '&sort=' + sort + '&dir=' + direction
    try:
        directory = inside(folder)
        if not directory.is_dir():
            raise ValueError('Folder not found')
        items = sorted((p for p in directory.iterdir() if p.resolve().is_relative_to(ROOT) and not p.is_symlink()), key=lambda p: file_sort_key(p, sort), reverse=direction == 'desc')
        rows = []
        for item in items:
            rel = str(item.relative_to(ROOT))
            quoted = urllib.parse.quote(rel)
            delete = '<form class="inline-form" method="post" action="/delete" onsubmit="return confirm(\'Delete this item permanently?\')"><input type="hidden" name="path" value="' + html.escape(rel, quote=True) + '"><button class="danger" type="submit">Delete</button></form>'
            if item.is_dir():
                count = sum(1 for child in item.iterdir() if not child.is_symlink() and child.resolve().is_relative_to(ROOT))
                rows.append(f'<div class="row library-row folder" data-path="{html.escape(rel, quote=True)}"><div><a class="filename" href="/?folder={quoted}{sort_query}#files"><span class="file-icon">▣</span><strong>{safe_name(item)}</strong></a><div class="muted">{count} item{"s" if count != 1 else ""} · Modified {datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d")}</div></div><div class="right"><a class="link" href="/?folder={quoted}{sort_query}#files">Open</a><a class="link" href="/zip?path={quoted}">Download ZIP</a>{delete}</div></div>')
            elif item.is_file():
                rows.append(f'<div class="row library-row" data-path="{html.escape(rel, quote=True)}"><div><div class="filename"><span class="file-icon">♪</span><strong>{safe_name(item)}</strong></div><div class="muted">{size_label(item.stat().st_size)} · Modified {datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d")}</div></div><div class="right"><a class="link" href="/file?path={quoted}">Download</a>{delete}</div></div>')
        return {'html': ''.join(rows) or '<div class="empty">No files in this folder.</div>', 'error': None}
    except (ValueError, OSError):
        return {'html': None, 'error': 'This folder is unavailable. It may have moved or been removed.'}


CSS = Path(__file__).with_name('ui.css').read_text()
LIVE_JS = Path(__file__).with_name('ui.js').read_text()


def page(body, title='Playlist desk'):
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>{html.escape(title)}</title><style>{CSS}</style></head><body><a class="skip" href="#main">Skip to content</a><div class="shell"><header class="top"><a class="brand" href="/"><span class="mark" aria-hidden="true">♫</span>Playlist desk</a><nav class="nav" aria-label="Main navigation"><a href="/#start">Import</a><a href="/#transfers">Downloads</a><a href="/#files">Library</a></nav><span class="connection" id="live-status" role="status"></span></header><main id="main" tabindex="-1">{body}</main></div></body></html>'''.encode()


class Handler(BaseHTTPRequestHandler):
    def auth(self):
        expected = os.environ.get('APP_PASSWORD', '')
        if not expected:
            self.send_error(503, 'Set APP_PASSWORD before serving the app')
            return False
        header = self.headers.get('Authorization', '')
        try:
            scheme, value = header.split(' ', 1)
            credentials = base64.b64decode(value, validate=True).decode()
            user, password = credentials.split(':', 1)
            ok = scheme.lower() == 'basic' and hmac.compare_digest(user, os.environ.get('APP_USER', 'admin')) and hmac.compare_digest(password, expected)
        except (ValueError, UnicodeError):
            ok = False
        if not ok:
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="Playlist desk"')
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
        if self.path == '/workflow-status':
            self.workflow_status()
            return
        if not self.auth():
            return
        if self.path not in ('/submit', '/clear-history', '/delete', '/workflow-status'):
            self.send_error(404)
            return
        origin = self.headers.get('Origin')
        host = self.headers.get('Host', '')
        if (origin and urllib.parse.urlparse(origin).netloc != host) or (self.path != '/submit' and not origin):
            self.send_error(403, 'Invalid origin')
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > 4096:
                raise ValueError('Input too large')
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
            if self.path == '/clear-history':
                with STATE_LOCK:
                    save_runs([])
                self.send_response(303)
                self.send_header('Location', '/#activity')
                self.end_headers()
                return
            if self.path == '/delete':
                relative = form.get('path', [''])[0]
                parts = Path(relative)
                target = inside(relative)
                if not relative or parts.is_absolute() or '..' in parts.parts or target == ROOT:
                    raise ValueError('Invalid delete path')
                check = ROOT
                for part in parts.parts:
                    check /= part
                    if check.is_symlink():
                        raise ValueError('Symlinks cannot be deleted here')
                if target.is_dir():
                    if not shutil.rmtree.avoids_symlink_attacks:
                        raise ValueError('Safe folder deletion is unavailable')
                    shutil.rmtree(target)
                elif target.is_file():
                    target.unlink()
                else:
                    raise ValueError('File or folder not found')
                parent = str(parts.parent)
                self.send_response(303)
                self.send_header('Location', '/?folder=' + urllib.parse.quote('' if parent == '.' else parent) + '#files')
                self.end_headers()
                return
            url = form.get('url', [''])[0].strip()
            ident = playlist_id(url)
            if not ident:
                raise ValueError('Enter a Spotify playlist URL or URI')
            webhook = os.environ.get('N8N_WEBHOOK_URL', '')
            if not webhook:
                raise ValueError('The download service is not configured yet.')
            headers = {'Content-Type': 'application/json'}
            if os.environ.get('N8N_WEBHOOK_TOKEN'):
                headers['Authorization'] = 'Bearer ' + os.environ['N8N_WEBHOOK_TOKEN']
            submission_id = uuid.uuid4().hex
            with STATE_LOCK:
                runs = read_runs()
                runs.insert(0, {'id': ident, 'submissionId': submission_id, 'name': 'Spotify playlist ' + ident, 'url': url, 'submitted': datetime.now(timezone.utc).isoformat(), 'status': 'submitting'})
                save_runs(runs[:100])
            req = urllib.request.Request(webhook, json.dumps({'url': url, 'playlistId': ident, 'submissionId': submission_id}).encode(), headers)
            try:
                with urllib.request.urlopen(req, timeout=15) as response:
                    if response.status >= 300:
                        raise ValueError('Workflow rejected submission')
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
            name = playlist_name(ident) or 'Spotify playlist ' + ident
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
            self.log_error('n8n webhook returned HTTP %d', error.code)
            message = 'n8n rejected the webhook credentials (403); check N8N_WEBHOOK_TOKEN' if error.code == 403 else f'n8n webhook returned HTTP {error.code}; check N8N_WEBHOOK_URL'
            self.send(page('<div class="error-page"><div class="error">The download service could not accept this playlist. Check the connection and try again.</div><details class="track-details"><summary>Technical details</summary>' + html.escape(message) + '</details><a href="/">Back to import</a></div>'), status=502)
        except (ValueError, urllib.error.URLError, TimeoutError) as error:
            message = 'Connection lost. Check recent imports before submitting again.' if isinstance(error, (urllib.error.URLError, TimeoutError)) else str(error)
            self.send(page('<div class="error">' + html.escape(message) + '</div><p><a href="/">Return to dashboard</a></p>'), status=400)
        except OSError as error:
            self.log_error('file operation failed: %s', error)
            self.send(page('<div class="error">Could not delete this item; check the downloads directory permissions.</div><p><a href="/">Return to dashboard</a></p>'), status=500)

    def do_GET(self):
        if not self.auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ('/file', '/zip'):
            self.download(parsed)
        elif parsed.path == '/transfers':
            self.send(self.transfer_panel().encode(), kind='text/html; charset=utf-8')
        elif parsed.path == '/events':
            self.events()
        elif parsed.path == '/':
            self.dashboard()
        else:
            self.send_error(404)

    def download(self, parsed):
        try:
            relative = urllib.parse.parse_qs(parsed.query).get('path', [''])[0]
            if not relative:
                raise ValueError('Missing path')
            target = inside(relative)
            if not target.exists():
                raise FileNotFoundError(relative)
            if parsed.path == '/file':
                if not target.is_file():
                    raise ValueError('Not a file')
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
                    raise ValueError('Not a folder')
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(target.name + '.zip'))
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                # ponytail: ZipFile writes directly to the socket, avoiding a playlist-sized buffer.
                with zipfile.ZipFile(self.wfile, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    for base, dirs, files in os.walk(target):
                        dirs[:] = [d for d in dirs if inside(str((Path(base) / d).relative_to(ROOT))).is_dir()]
                        for name in files:
                            file = Path(base) / name
                            if file.is_file() and file.resolve().is_relative_to(ROOT):
                                archive.write(file, file.relative_to(target))
        except (ValueError, FileNotFoundError) as error:
            self.send(page('<div class="error">' + html.escape(str(error)) + '</div>'), status=400)

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
                for key in ('spotifyId', 'spotifyUrl', 'artist', 'title', 'status', 'username', 'filename', 'batchId'):
                    if key in track:
                        if track[key] is not None and not isinstance(track[key], str):
                            raise ValueError('Invalid track field')
                        result[key] = track[key] or ''
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
                        save_runs(runs)
            self.send(b'{}', kind='application/json')
        except (ValueError, TypeError) as error:
            self.send(page('<div class="error">' + html.escape(str(error)) + '</div>'), status=400)

    def transfer_panel(self):
        transfers, error = transfer_snapshot()
        if error:
            return '<div class="empty">' + html.escape(error) + '</div>'
        rows = []
        for file in transfers[:80]:
            label = html.escape(file['name'])
            detail = f"{size_label(file['done'])} of {size_label(file['size'])}"
            if file['speed']:
                detail += f" · {size_label(file['speed'])}/s"
            rows.append(f'<div class="row transfer"><div class="transfer-top"><div><strong>{label}</strong><div class="muted">{html.escape(file["username"])} · {html.escape(file["folder"])}</div></div><span class="badge">{html.escape(file["state"])}</span></div><div class="progress" role="progressbar" aria-label="{label}" aria-valuenow="{file["percent"]:.0f}" aria-valuemin="0" aria-valuemax="100"><span style="width:{file["percent"]:.1f}%"></span></div><div class="transfer-bottom"><span>{html.escape(detail)}</span><strong>{file["percent"]:.0f}%</strong></div></div>')
        return ''.join(rows) or '<div class="empty">No transfers to show yet.</div>'

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
                transfers, error = transfer_snapshot()
                data = json.dumps({'transfers': transfers, 'runs': [run_view(run, transfers) for run in read_runs()[:8]], 'error': error, 'library': library_view(folder, sort, direction)}, ensure_ascii=False)
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
        library = library_view(folder, sort, direction)
        crumbs = ['<a href="/?' + sort_query.lstrip('&') + '#files">Library</a>']
        current = Path()
        for part in Path(folder).parts if folder else []:
            current /= part
            crumbs.append('<span>›</span><a href="/?folder=' + urllib.parse.quote(str(current)) + sort_query + '#files">' + html.escape(part) + '</a>')
        runs = read_runs()[:8]
        missing = {(run['id'], run['submitted']): playlist_name(run['id']) or 'Spotify playlist ' + run['id'] for run in runs if not run.get('name')}
        if missing:
            with STATE_LOCK:
                saved = read_runs()
                for run in saved:
                    key = (run['id'], run['submitted'])
                    if not run.get('name') and key in missing:
                        run['name'] = missing[key]
                save_runs(saved)
            runs = read_runs()[:8]
        body = '''<div data-view="start"><section class="import" id="start"><h1>Import a playlist</h1><form method="post" action="/submit" class="field" id="import-form"><input name="url" aria-label="Spotify playlist link" aria-describedby="import-hint import-feedback" placeholder="Paste a Spotify playlist link" autocomplete="off" spellcheck="false" required><button class="primary" type="submit">Import</button></form><div class="hint" id="import-hint">Spotify playlist links and URIs<kbd>⌘ / Ctrl K</kbd></div><div id="import-feedback" role="status"></div></section><section id="activity"><div class="sectionhead"><h2>Recent imports</h2><form id="clear-history" class="inline-form" method="post" action="/clear-history" onsubmit="return confirm('Clear import history? Downloaded files will be kept.')"><button class="danger" type="submit">Clear history</button></form></div><div class="list" id="run-list"></div></section></div>'''
        body += '''<section data-view="transfers" id="transfers"><h1>Downloads</h1><p class="queue-summary" id="queue-summary"></p><div class="notice" id="queue-notice" hidden><span id="queue-error"></span><button type="button" id="reconnect">Reconnect</button></div><div class="controls"><input id="transfer-search" type="search" placeholder="Search downloads" aria-label="Search downloads"><select id="transfer-status" aria-label="Filter download status"><option value="">All statuses</option><option value="downloading">Downloading</option><option value="queued">Queued</option><option value="failed">Failed</option><option value="completed">Complete</option><option value="unknown">Awaiting status</option></select><select id="transfer-sort" aria-label="Sort downloads"><option value="status">Active first</option><option value="newest">Newest first</option><option value="progress">Progress</option><option value="name">Name</option><option value="speed">Speed</option></select><button id="transfer-reverse" type="button" data-reverse="false" aria-pressed="false">Reverse order</button></div><div class="list" id="transfer-list"></div><button id="more-transfers" type="button" hidden>Show more</button></section>'''
        body += '<section data-view="files" id="files"><div class="sectionhead"><div><h1 style="margin-bottom:0">Library</h1><div class="crumbs">' + ''.join(crumbs) + '</div></div><a class="link" href="' + html.escape(self.path.split('#')[0], quote=True) + '#files">Refresh</a></div><form class="controls" method="get" action="/#files"><input id="library-search" type="search" placeholder="Search this folder" aria-label="Search this folder"><input type="hidden" name="folder" value="' + html.escape(folder, quote=True) + '"><select id="file-sort" name="sort" aria-label="Sort files"><option value="name"' + (' selected' if sort == 'name' else '') + '>Name</option><option value="type"' + (' selected' if sort == 'type' else '') + '>Type</option><option value="size"' + (' selected' if sort == 'size' else '') + '>Size</option><option value="modified"' + (' selected' if sort == 'modified' else '') + '>Modified</option></select><select name="dir" aria-label="File sort direction"><option value="asc"' + (' selected' if direction == 'asc' else '') + '>Ascending</option><option value="desc"' + (' selected' if direction == 'desc' else '') + '>Descending</option></select><button type="submit">Sort</button></form><section class="section" id="library-activity"><h2>Download activity</h2><div id="library-transfers" class="list"></div><button type="button" id="more-library-transfers" hidden>Show more</button></section><div class="list" id="library-list">' + (library['html'] or '') + '</div><p id="library-error" class="error" role="status"></p><p id="library-no-match" class="empty" hidden>No files match your search.</p></section>'
        initial = json.dumps({'runs': [run_view(run, []) for run in runs], 'transfers': [], 'error': None, 'library': library}).replace('<', '\\u003c')
        body += '<script type="application/json" id="initial-runs">' + initial + '</script>'
        body += '<script>' + LIVE_JS + '</script>'
        self.send(page(body))

if __name__ == '__main__':
    assert playlist_id('spotify:playlist:37i9dQZF1DXcBWIGoYBM5M') == '37i9dQZF1DXcBWIGoYBM5M'
    assert playlist_id('https://evil.example/playlist/37i9dQZF1DXcBWIGoYBM5M') is None
    ROOT.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
