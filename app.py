import base64
import hmac
import html
import io
import json
import os
import re
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


CSS = '''
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#202522;background:#f5f6f3;font-synthesis:none}
*{box-sizing:border-box}body{margin:0}button,input{font:inherit}a{color:inherit;text-decoration:none}a:hover{text-decoration:underline}
.shell{max-width:1060px;margin:auto;padding:0 28px 64px}.top{height:82px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #e3e6e0}.brand{display:flex;gap:11px;align-items:center;font-weight:750;letter-spacing:-.04em;font-size:20px}.mark{height:32px;width:32px;border-radius:9px;background:#1d4638;color:white;display:grid;place-items:center;font-size:18px}.top small{color:#7b827c;font-weight:600;letter-spacing:.05em;text-transform:uppercase}
.hero{padding:54px 0 38px}.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.15em;color:#64806f;font-weight:800}.hero h1{font-size:clamp(34px,5vw,52px);letter-spacing:-.055em;line-height:1.1;margin:12px 0}.hero p{color:#69716a;margin:0;font-size:16px}
.grid{display:grid;grid-template-columns:1.15fr .85fr;gap:20px}.card{background:#fff;border:1px solid #e3e7e1;border-radius:17px;padding:28px;box-shadow:0 2px 12px #172f2108}.card h2{font-size:18px;letter-spacing:-.025em;margin:0 0 8px}.muted{color:#737d75;font-size:13px;line-height:1.5}.field{display:flex;gap:10px;margin-top:25px}input{width:100%;border:1px solid #d8ddd6;border-radius:9px;padding:13px 15px;outline:none;background:#fbfcfa;color:#202522}input:focus{border-color:#28734e;box-shadow:0 0 0 3px #28734e22}button,.button{border:0;background:#1f593e;color:white;border-radius:9px;padding:13px 18px;font-weight:650;white-space:nowrap;cursor:pointer;display:inline-block}button:hover,.button:hover{background:#17452f;text-decoration:none}.section{margin-top:35px}.sectionhead{display:flex;align-items:center;justify-content:space-between;margin-bottom:15px}.sectionhead h2{font-size:22px;letter-spacing:-.035em;margin:0}.list{background:white;border:1px solid #e3e7e1;border-radius:15px;overflow:hidden}.row{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:18px 22px;border-top:1px solid #edf0ec}.row:first-child{border-top:0}.row strong{font-size:14px;font-weight:650;overflow-wrap:anywhere}.row .muted{margin-top:4px}.badge{border-radius:100px;padding:6px 10px;font-size:11px;font-weight:750;background:#edf4ee;color:#2b6947;white-space:nowrap}.badge.waiting{background:#f5f1e8;color:#8b6d35}.badge.failed{background:#f9eeee;color:#9b4c4c}.right{display:flex;align-items:center;gap:15px}.empty{padding:38px 22px;color:#8a938c;text-align:center;font-size:14px}.link{font-size:12px;font-weight:700;color:#256242}.error{background:#fff3f1;color:#a0443c;border-radius:9px;padding:12px 14px;margin:15px 0}.hint{font-size:12px;color:#879087;margin-top:10px}
@media(max-width:720px){.grid{grid-template-columns:1fr}.shell{padding:0 18px 45px}.hero{padding:39px 0 30px}.card{padding:22px}.field{flex-direction:column}.top{height:70px}.row{padding:16px}.right{gap:9px}}
'''


def page(body, title='Playlist desk'):
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{CSS}</style><div class="shell"><header class="top"><a class="brand" href="/"><span class="mark">♪</span>Playlist desk</a><small>Personal library</small></header>{body}</div></html>'''.encode()


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
        if self.path != '/submit':
            self.send_error(404)
            return
        origin = self.headers.get('Origin')
        host = self.headers.get('Host', '')
        if origin and urllib.parse.urlparse(origin).netloc != host:
            self.send_error(403, 'Invalid origin')
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > 4096:
                raise ValueError('Input too large')
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
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
            runs = read_runs()
            runs.insert(0, {'id': ident, 'url': url, 'submitted': datetime.now(timezone.utc).isoformat(), 'status': 'submitted'})
            save_runs(runs[:100])
            self.send_response(303)
            self.send_header('Location', '/')
            self.end_headers()
        except urllib.error.HTTPError as error:
            message = 'n8n rejected the webhook credentials (403); check N8N_WEBHOOK_TOKEN' if error.code == 403 else f'n8n webhook returned HTTP {error.code}; check N8N_WEBHOOK_URL'
            self.send(page('<div class="error">' + html.escape(message) + '</div><p><a href="/">Return to dashboard</a></p>'), status=502)
        except (ValueError, urllib.error.URLError, TimeoutError) as error:
            self.send(page('<div class="error">' + html.escape(str(error)) + '</div><p><a href="/">Return to dashboard</a></p>'), status=400)

    def do_GET(self):
        if not self.auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ('/file', '/zip'):
            self.download(parsed)
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
        rows = []
        if folder:
            parent = str(Path(folder).parent)
            rows.append('<div class="row"><a href="/?folder=' + urllib.parse.quote('' if parent == '.' else parent) + '">← Back</a></div>')
        for item in items:
            rel = str(item.relative_to(ROOT))
            quoted = urllib.parse.quote(rel)
            if item.is_dir():
                rows.append(f'<div class="row"><div><strong>📁 {safe_name(item)}</strong><div class="muted">Folder</div></div><div class="right"><a class="link" href="/?folder={quoted}">Open</a><a class="link" href="/zip?path={quoted}">Download ZIP</a></div></div>')
            elif item.is_file():
                rows.append(f'<div class="row"><div><strong>{safe_name(item)}</strong><div class="muted">{size_label(item.stat().st_size)}</div></div><a class="link" href="/file?path={quoted}">Download</a></div>')
        runrows = []
        for run in read_runs()[:8]:
            status = html.escape(run['status'])
            runrows.append(f'<div class="row"><div><strong>Spotify playlist {html.escape(run["id"])}</strong><div class="muted">Submitted {html.escape(run["submitted"][:16].replace("T", " "))} UTC</div></div><span class="badge waiting">{status}</span></div>')
        transferrows = []
        slskd = os.environ.get('SLSKD_URL', '').rstrip('/')
        if slskd:
            try:
                transfers = request_json(slskd + '/api/v0/transfers/downloads', os.environ.get('SLSKD_API_KEY'))
                for user in transfers if isinstance(transfers, list) else []:
                    for directory_group in user.get('directories', []):
                        for file in directory_group.get('files', []):
                            name = str(file.get('filename', 'Unknown file')).replace('\\', '/')
                            state = str(file.get('state', 'Unknown'))
                            size = file.get('size') or 0
                            done = file.get('bytesTransferred') or 0
                            percent = f'{done / size:.0%}' if size else '—'
                            speed = file.get('averageSpeed') or 0
                            detail = f'{percent} · {size_label(done)} / {size_label(size)} · {size_label(speed)}/s'
                            if file.get('error'):
                                detail += ' · ' + str(file['error'])
                            transferrows.append(f'<div class="row"><div><strong>{html.escape(Path(name).name)}</strong><div class="muted">{html.escape(directory_group.get("directory", ""))} · {html.escape(detail)}</div></div><span class="badge">{html.escape(state)}</span></div>')
            except (urllib.error.URLError, ValueError, KeyError, TimeoutError):
                transferrows.append('<div class="empty">slskd is unavailable right now.</div>')
        body = '''<section class="hero"><div class="eyebrow">Your music, organized</div><h1>Playlist downloads.</h1><p>Send a playlist, follow transfers, and collect finished files.</p></section><div class="grid"><section class="card"><h2>New playlist</h2><div class="muted">Paste a Spotify playlist link to start processing.</div><form method="post" action="/submit" class="field"><input name="url" aria-label="Spotify playlist URL" placeholder="https://open.spotify.com/playlist/..." required><button type="submit">Start download</button></form><div class="hint">Spotify playlist URLs and URIs are supported.</div></section><section class="card"><h2>At a glance</h2><div class="muted">Your files stay on the Raspberry Pi until you download them here.</div><div style="margin-top:26px;font-size:37px;letter-spacing:-.05em;font-weight:750">''' + str(len(items)) + '''</div><div class="muted">items in this folder</div></section></div>'''
        body += '<section class="section"><div class="sectionhead"><h2>Recent playlists</h2></div><div class="list">' + (''.join(runrows) or '<div class="empty">No playlists submitted yet.</div>') + '</div></section>'
        body += '<section class="section"><div class="sectionhead"><h2>Transfers</h2></div><div class="list">' + (''.join(transferrows) or '<div class="empty">No active transfers.</div>') + '</div></section>'
        body += '<section class="section"><div class="sectionhead"><h2>Completed downloads</h2></div><div class="list">' + (''.join(rows) or '<div class="empty">No completed files here yet.</div>') + '</div></section>'
        body += '<script>setTimeout(()=>location.reload(),15000)</script>'
        self.send(page(body))


if __name__ == '__main__':
    assert playlist_id('spotify:playlist:37i9dQZF1DXcBWIGoYBM5M') == '37i9dQZF1DXcBWIGoYBM5M'
    assert playlist_id('https://evil.example/playlist/37i9dQZF1DXcBWIGoYBM5M') is None
    ROOT.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
