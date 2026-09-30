import base64
import hmac
import html
import json
import os
import re
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
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


CSS = """
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#202722;background:#f6f7f4;font-synthesis:none}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0}button,input{font:inherit}a{color:inherit;text-decoration:none}a:hover{text-decoration:none;color:#1d6444}
.shell{max-width:1120px;margin:auto;padding:0 28px 72px}.top{height:76px;display:flex;align-items:center;justify-content:space-between;gap:24px;border-bottom:1px solid #e1e6df}.brand{display:flex;gap:11px;align-items:center;font-weight:750;letter-spacing:-.04em;font-size:19px;white-space:nowrap}.mark{height:34px;width:34px;border-radius:10px;background:#1d5139;color:white;display:grid;place-items:center;font-size:18px}.nav{display:flex;align-items:center;gap:5px}.nav a{padding:9px 13px;color:#657069;font-size:13px;font-weight:650;border-radius:8px}.nav a:hover,.nav a:focus-visible{background:#e9eee8;color:#1d5139;outline:none}
.hero{padding:49px 0 32px}.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.15em;color:#62806d;font-weight:800}.hero h1{font-size:clamp(34px,5vw,50px);letter-spacing:-.055em;line-height:1.1;margin:10px 0}.hero p{color:#68736b;margin:0;font-size:15px}.grid{display:grid;grid-template-columns:1.55fr .75fr;gap:18px}.card,.list{background:white;border:1px solid #e0e6df;border-radius:15px;box-shadow:0 2px 12px #172f2106}.card{padding:26px}.card h2{font-size:17px;letter-spacing:-.025em;margin:0 0 8px}.muted{color:#758077;font-size:13px;line-height:1.5}.field{display:flex;gap:9px;margin-top:22px}input{width:100%;min-width:0;border:1px solid #d8ded6;border-radius:9px;padding:12px 14px;outline:none;background:#fcfdfa;color:#202722}input:focus{border-color:#28734e;box-shadow:0 0 0 3px #28734e20}button{border:0;background:#205b40;color:white;border-radius:9px;padding:12px 17px;font-weight:650;white-space:nowrap;cursor:pointer}button:hover{background:#184b34}.hint{font-size:12px;color:#89948b;margin-top:10px}.stat{font-size:38px;letter-spacing:-.05em;font-weight:750;line-height:1;margin:26px 0 7px}.section{margin-top:38px;scroll-margin-top:20px}.sectionhead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.sectionhead h2{font-size:21px;letter-spacing:-.035em;margin:0}.sectionhead .muted{margin:3px 0 0}.list{overflow:hidden}.row{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:17px 21px;border-top:1px solid #edf0ec}.row:first-child{border-top:0}.row>div:first-child{min-width:0}.row strong{display:block;font-size:14px;font-weight:650;overflow-wrap:anywhere}.row .muted{margin-top:5px}.right{display:flex;align-items:center;gap:16px;flex-shrink:0}.badge{border-radius:100px;padding:6px 10px;font-size:11px;font-weight:750;background:#eaf3ed;color:#276444;white-space:nowrap}.badge.waiting{background:#f5f0e5;color:#876b35}.badge.failed{background:#faecea;color:#9c4b43}.empty{padding:38px 22px;color:#87928a;text-align:center;font-size:14px}.link{font-size:12px;font-weight:700;color:#256242;white-space:nowrap}.error{background:#fff0ed;color:#a0443c;border-radius:9px;padding:12px 14px;margin:15px 0}.crumbs{display:flex;align-items:center;gap:7px;flex-wrap:wrap;color:#77837a;font-size:13px}.crumbs a{color:#286344;font-weight:650}.transfer{display:block}.transfer-top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}.transfer .muted{margin-top:5px}.progress{height:7px;background:#e9eee9;border-radius:99px;overflow:hidden;margin-top:15px}.progress span{display:block;height:100%;background:#2e7953;border-radius:99px}.transfer-bottom{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:8px;color:#78837a;font-size:12px}.refresh{font-size:12px;color:#89948b}.file-icon{display:inline-grid;place-items:center;width:29px;height:29px;margin-right:8px;border-radius:8px;background:#edf3ee;color:#3b7051;font-size:14px}.filename{display:flex;align-items:center}.filename strong{display:inline}
.danger{background:transparent;color:#a54b45;padding:6px 2px;font-size:12px;font-weight:700}.danger:hover{background:transparent;color:#7e342f;text-decoration:underline}.count{color:#8a958d;font-weight:500;font-size:12px}.inline-form{display:inline;margin:0}
@media(max-width:720px){.shell{padding:0 17px 42px}.top{height:auto;min-height:72px;align-items:flex-start;flex-wrap:wrap;padding:16px 0}.nav{width:100%;overflow:auto}.nav a{padding:8px 10px}.hero{padding:35px 0 27px}.grid{grid-template-columns:1fr}.card{padding:22px}.field{flex-direction:column}.row{padding:15px;align-items:flex-start}.right{gap:10px;flex-wrap:wrap;justify-content:flex-end}.transfer-top{align-items:flex-start}.section{margin-top:31px}}
"""


def page(body, title='Playlist desk'):
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{CSS}</style><div class="shell"><header class="top"><a class="brand" href="/"><span class="mark">♪</span>Playlist desk</a><nav class="nav" aria-label="Main navigation"><a href="/#start">Start</a><a href="/#activity">Activity</a><a href="/#transfers">Transfers</a><a href="/#files">Files</a></nav></header>{body}</div></html>'''.encode()


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
        if not self.auth():
            return
        if self.path not in ('/submit', '/clear-history', '/delete'):
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
                raise ValueError('N8N_WEBHOOK_URL is not configured')
            headers = {'Content-Type': 'application/json'}
            if os.environ.get('N8N_WEBHOOK_TOKEN'):
                headers['Authorization'] = 'Bearer ' + os.environ['N8N_WEBHOOK_TOKEN']
            req = urllib.request.Request(webhook, json.dumps({'url': url, 'playlistId': ident}).encode(), headers)
            with urllib.request.urlopen(req, timeout=15) as response:
                if response.status >= 300:
                    raise ValueError('Workflow rejected submission')
            name = playlist_name(ident) or 'Spotify playlist ' + ident
            with STATE_LOCK:
                runs = read_runs()
                runs.insert(0, {'id': ident, 'name': name, 'url': url, 'submitted': datetime.now(timezone.utc).isoformat(), 'status': 'submitted'})
                save_runs(runs[:100])
            self.send_response(303)
            self.send_header('Location', '/')
            self.end_headers()
        except urllib.error.HTTPError as error:
            self.log_error('n8n webhook returned HTTP %d', error.code)
            message = 'n8n rejected the webhook credentials (403); check N8N_WEBHOOK_TOKEN' if error.code == 403 else f'n8n webhook returned HTTP {error.code}; check N8N_WEBHOOK_URL'
            self.send(page('<div class="error">' + html.escape(message) + '</div><p><a href="/">Return to dashboard</a></p>'), status=502)
        except (ValueError, urllib.error.URLError, TimeoutError) as error:
            self.send(page('<div class="error">' + html.escape(str(error)) + '</div><p><a href="/">Return to dashboard</a></p>'), status=400)
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

    def transfer_panel(self):
        slskd = os.environ.get('SLSKD_URL', '').rstrip('/')
        if not slskd:
            return '<div class="empty">Set SLSKD_URL to show transfers.</div>'
        try:
            transfers = request_json(slskd + '/api/v0/transfers/downloads', os.environ.get('SLSKD_API_KEY'))
        except urllib.error.HTTPError as error:
            self.log_error('slskd transfers returned HTTP %d', error.code)
            return '<div class="empty">slskd rejected the API key.</div>' if error.code in (401, 403) else '<div class="empty">slskd is unavailable right now.</div>'
        except (urllib.error.URLError, ValueError, TimeoutError) as error:
            self.log_error('slskd transfers unavailable: %s', error)
            return '<div class="empty">slskd is unavailable right now.</div>'
        rows = []
        for user in transfers if isinstance(transfers, list) else []:
            if not isinstance(user, dict):
                continue
            for group in user.get('directories', []):
                if not isinstance(group, dict):
                    continue
                folder = str(group.get('directory') or 'Unknown folder').replace('\\', '/')
                for file in group.get('files', []):
                    if not isinstance(file, dict):
                        continue
                    name = str(file.get('filename') or 'Unknown file').replace('\\', '/')
                    state = str(file.get('state') or 'Unknown')
                    try:
                        size = max(0, float(file.get('size') or 0))
                        done = max(0, float(file.get('bytesTransferred') or 0))
                        speed = max(0, float(file.get('averageSpeed') or 0))
                        percent = float(file.get('percentComplete')) if file.get('percentComplete') is not None else (done / size * 100 if size else 0)
                        percent = max(0, min(100, percent))
                    except (TypeError, ValueError):
                        size = done = speed = percent = 0
                    problem = str(file.get('error') or file.get('failureReason') or '')
                    kind = 'failed' if problem or any(word in state.lower() for word in ('error', 'fail', 'reject', 'cancel', 'timeout')) else 'waiting' if any(word in state.lower() for word in ('queue', 'wait', 'pending')) else ''
                    detail = f'{size_label(done)} of {size_label(size)}'
                    if speed:
                        detail += f' · {size_label(speed)}/s'
                    if problem:
                        detail += ' · ' + problem
                    label = html.escape(Path(name).name)
                    rows.append(f'<div class="row transfer"><div class="transfer-top"><div><strong>{label}</strong><div class="muted">{html.escape(folder)}</div></div><span class="badge {kind}">{html.escape(state)}</span></div><div class="progress" role="progressbar" aria-label="{label}" aria-valuenow="{percent:.0f}" aria-valuemin="0" aria-valuemax="100"><span style="width:{percent:.1f}%"></span></div><div class="transfer-bottom"><span>{html.escape(detail)}</span><strong>{percent:.0f}%</strong></div></div>')
        return ''.join(rows[:80]) or '<div class="empty">No transfers to show yet.</div>'

    def dashboard(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        folder = query.get('folder', [''])[0]
        try:
            directory = inside(folder)
            if not directory.is_dir():
                raise ValueError('Folder not found')
            items = sorted((p for p in directory.iterdir() if p.resolve().is_relative_to(ROOT) and not p.is_symlink()), key=lambda p: (not p.is_dir(), p.name.lower()))
        except (ValueError, OSError):
            self.send(page('<div class="error">Folder not found</div>'), status=404)
            return
        crumbs = ['<a href="/#files">Downloads</a>']
        current = Path()
        for part in Path(folder).parts if folder else []:
            current /= part
            crumbs.append('<span>›</span><a href="/?folder=' + urllib.parse.quote(str(current)) + '#files">' + html.escape(part) + '</a>')
        rows = []
        for item in items:
            rel = str(item.relative_to(ROOT))
            quoted = urllib.parse.quote(rel)
            delete = '<form class="inline-form" method="post" action="/delete" onsubmit="return confirm(\'Delete this item permanently?\')"><input type="hidden" name="path" value="' + html.escape(rel, quote=True) + '"><button class="danger" type="submit">Delete</button></form>'
            if item.is_dir():
                count = sum(1 for child in item.iterdir() if not child.is_symlink() and child.resolve().is_relative_to(ROOT))
                rows.append(f'<div class="row"><div><a class="filename" href="/?folder={quoted}#files"><span class="file-icon">▣</span><strong>{safe_name(item)}</strong></a><div class="muted">{count} item{"s" if count != 1 else ""}</div></div><div class="right"><a class="link" href="/?folder={quoted}#files">Open</a><a class="link" href="/zip?path={quoted}">Download ZIP</a>{delete}</div></div>')
            elif item.is_file():
                rows.append(f'<div class="row"><div><div class="filename"><span class="file-icon">♪</span><strong>{safe_name(item)}</strong></div><div class="muted">{size_label(item.stat().st_size)}</div></div><div class="right"><a class="link" href="/file?path={quoted}">Download</a>{delete}</div></div>')
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
        runrows = []
        for run in runs:
            runrows.append(f'<div class="row"><div><strong>{html.escape(run.get("name") or "Spotify playlist " + run["id"])}</strong><div class="muted">Submitted {html.escape(run["submitted"][:16].replace("T", " "))} UTC</div></div><span class="badge waiting">Submitted</span></div>')
        body = f'''<section class="hero" id="start"><div class="eyebrow">Personal music library</div><h1>Playlist downloads.</h1><p>Submit a playlist, follow transfers, and collect finished files.</p></section><div class="grid"><section class="card"><h2>New playlist</h2><div class="muted">Paste a Spotify playlist link to start processing.</div><form method="post" action="/submit" class="field"><input name="url" aria-label="Spotify playlist URL" placeholder="https://open.spotify.com/playlist/..." required><button type="submit">Submit playlist</button></form><div class="hint">Spotify playlist URLs and URIs are supported.</div></section><section class="card"><h2>Completed files</h2><div class="muted">Browse or download what is already on the Pi.</div><div class="stat">{len(items)}</div><div class="muted">items in this folder</div></section></div>'''
        body += '<section class="section" id="activity"><div class="sectionhead"><div><h2>Recent submissions</h2><p class="muted">Accepted by n8n; transfer progress appears below.</p></div><form class="inline-form" method="post" action="/clear-history" onsubmit="return confirm(\'Clear recent submissions? This will not delete downloaded files.\')"><button class="danger" type="submit">Clear history</button></form></div><div class="list">' + (''.join(runrows) or '<div class="empty">No playlists submitted yet.</div>') + '</div></section>'
        body += '<section class="section" id="transfers"><div class="sectionhead"><div><h2>Transfers</h2><p class="muted">Live download progress from slskd.</p></div><span class="refresh">Updates every 12 seconds</span></div><div class="list" id="transfer-list">' + self.transfer_panel() + '</div></section>'
        body += '<section class="section" id="files"><div class="sectionhead"><div><h2>Completed downloads</h2><div class="crumbs">' + ''.join(crumbs) + '</div></div><a class="link" href="' + html.escape(self.path, quote=True) + '#files">Refresh files</a></div><div class="list">' + (''.join(rows) or '<div class="empty">No completed files here yet.</div>') + '</div></section>'
        body += '''<script>setInterval(async()=>{if(document.hidden)return;try{let r=await fetch('/transfers',{cache:'no-store'});if(r.ok)document.getElementById('transfer-list').innerHTML=await r.text()}catch(e){}},12000)</script>'''
        self.send(page(body))

if __name__ == '__main__':
    assert playlist_id('spotify:playlist:37i9dQZF1DXcBWIGoYBM5M') == '37i9dQZF1DXcBWIGoYBM5M'
    assert playlist_id('https://evil.example/playlist/37i9dQZF1DXcBWIGoYBM5M') is None
    ROOT.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
