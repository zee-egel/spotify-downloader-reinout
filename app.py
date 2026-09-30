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
                    'folder': str(group.get('directory') or ''), 'username': str(user.get('username') or ''),
                    'state': state, 'kind': transfer_kind(raw_state, error), 'error': error,
                    'size': size, 'done': done, 'speed': speed, 'percent': max(0, min(100, percent)),
                    'date': str(file.get('requestedAt') or file.get('queuedAt') or file.get('startedAt') or ''), 'order': len(rows)})
    return rows, None


def run_view(run, transfers):
    state = run.get('workflow') or {}
    by_batch = {}
    for transfer in transfers:
        by_batch.setdefault(transfer['batchId'], []).append(transfer)
    tracks = []
    for download in state.get('downloads') or []:
        files = by_batch.get(str(download.get('batchId') or ''), [])
        kinds = [file['kind'] for file in files]
        status = next((kind for kind in ('completed', 'downloading', 'queued remotely', 'queued locally', 'failed') if kind in kinds), 'unknown')
        if download.get('fallbackStatus') == 'queued' and status not in ('completed', 'failed'):
            status = 'trying another source' if status == 'unknown' else status
        tracks.append({'batchId': str(download.get('batchId') or ''), 'artist': download.get('artist') or '', 'title': download.get('title') or '',
            'status': status, 'fallback': bool(download.get('fallbackAttempted')),
            'source': download.get('fallbackSource') if download.get('fallbackAttempted') else next((f['username'] for f in files), ''),
            'percent': max((file['percent'] for file in files), default=0)})
    for problem in state.get('problems') or []:
        status = problem.get('status') or 'unknown'
        if status in ('error', 'search_timeout'):
            status = 'failed'
        tracks.append({'batchId': '', 'artist': problem.get('artist') or '', 'title': problem.get('title') or '',
            'status': status, 'fallback': False, 'source': '', 'percent': 0})
    counts = {kind: sum(track['status'] == kind for track in tracks) for kind in ('completed', 'downloading', 'queued locally', 'queued remotely', 'failed', 'unknown')}
    counts['fallback'] = sum(track['fallback'] for track in tracks)
    counts['no match'] = state.get('noMatch') or 0
    counts['needs review'] = state.get('reviewRequired') or 0
    return {'id': run.get('id'), 'submissionId': run.get('submissionId'), 'name': run.get('name'), 'submitted': run.get('submitted'),
        'phase': state.get('phase') or 'submitted', 'total': state.get('totalSpotifyTracks'),
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


CSS = """
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#202722;background:#f6f7f4;font-synthesis:none}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0}button,input{font:inherit}a{color:inherit;text-decoration:none}a:hover{text-decoration:none;color:#1d6444}
.shell{max-width:1120px;margin:auto;padding:0 28px 72px}.top{height:76px;display:flex;align-items:center;justify-content:space-between;gap:24px;border-bottom:1px solid #e1e6df}.brand{display:flex;gap:11px;align-items:center;font-weight:750;letter-spacing:-.04em;font-size:19px;white-space:nowrap}.mark{height:34px;width:34px;border-radius:10px;background:#1d5139;color:white;display:grid;place-items:center;font-size:18px}.nav{display:flex;align-items:center;gap:5px}.nav a{padding:9px 13px;color:#657069;font-size:13px;font-weight:650;border-radius:8px}.nav a:hover,.nav a:focus-visible{background:#e9eee8;color:#1d5139;outline:none}
.hero{padding:49px 0 32px}.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.15em;color:#62806d;font-weight:800}.hero h1{font-size:clamp(34px,5vw,50px);letter-spacing:-.055em;line-height:1.1;margin:10px 0}.hero p{color:#68736b;margin:0;font-size:15px}.grid{display:grid;grid-template-columns:1.55fr .75fr;gap:18px}.card,.list{background:white;border:1px solid #e0e6df;border-radius:15px;box-shadow:0 2px 12px #172f2106}.card{padding:26px}.card h2{font-size:17px;letter-spacing:-.025em;margin:0 0 8px}.muted{color:#758077;font-size:13px;line-height:1.5}.field{display:flex;gap:9px;margin-top:22px}input{width:100%;min-width:0;border:1px solid #d8ded6;border-radius:9px;padding:12px 14px;outline:none;background:#fcfdfa;color:#202722}input:focus{border-color:#28734e;box-shadow:0 0 0 3px #28734e20}button{border:0;background:#205b40;color:white;border-radius:9px;padding:12px 17px;font-weight:650;white-space:nowrap;cursor:pointer}button:hover{background:#184b34}.hint{font-size:12px;color:#89948b;margin-top:10px}.stat{font-size:38px;letter-spacing:-.05em;font-weight:750;line-height:1;margin:26px 0 7px}.section{margin-top:38px;scroll-margin-top:20px}.sectionhead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.sectionhead h2{font-size:21px;letter-spacing:-.035em;margin:0}.sectionhead .muted{margin:3px 0 0}.list{overflow:hidden}.row{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:17px 21px;border-top:1px solid #edf0ec}.row:first-child{border-top:0}.row>div:first-child{min-width:0}.row strong{display:block;font-size:14px;font-weight:650;overflow-wrap:anywhere}.row .muted{margin-top:5px}.right{display:flex;align-items:center;gap:16px;flex-shrink:0}.badge{border-radius:100px;padding:6px 10px;font-size:11px;font-weight:750;background:#eaf3ed;color:#276444;white-space:nowrap}.badge.waiting{background:#f5f0e5;color:#876b35}.badge.failed{background:#faecea;color:#9c4b43}.empty{padding:38px 22px;color:#87928a;text-align:center;font-size:14px}.link{font-size:12px;font-weight:700;color:#256242;white-space:nowrap}.error{background:#fff0ed;color:#a0443c;border-radius:9px;padding:12px 14px;margin:15px 0}.crumbs{display:flex;align-items:center;gap:7px;flex-wrap:wrap;color:#77837a;font-size:13px}.crumbs a{color:#286344;font-weight:650}.transfer{display:block}.transfer-top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}.transfer .muted{margin-top:5px}.progress{height:7px;background:#e9eee9;border-radius:99px;overflow:hidden;margin-top:15px}.progress span{display:block;height:100%;background:#2e7953;border-radius:99px}.transfer-bottom{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:8px;color:#78837a;font-size:12px}.refresh{font-size:12px;color:#89948b}.file-icon{display:inline-grid;place-items:center;width:29px;height:29px;margin-right:8px;border-radius:8px;background:#edf3ee;color:#3b7051;font-size:14px}.filename{display:flex;align-items:center}.filename strong{display:inline}
.danger{background:transparent;color:#a54b45;padding:6px 2px;font-size:12px;font-weight:700}.danger:hover{background:transparent;color:#7e342f;text-decoration:underline}.count{color:#8a958d;font-weight:500;font-size:12px}.inline-form{display:inline;margin:0}
.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 12px}.controls input{max-width:240px}.controls select{font:inherit;color:#26332a;background:#fff;border:1px solid #d8ded6;border-radius:9px;padding:10px;min-height:43px}.controls button{padding:10px 12px}.track-list{border-top:1px solid #edf0ec;padding:8px 21px 16px}.track-row{display:flex;justify-content:space-between;gap:14px;padding:8px 0;font-size:13px}.track-row span:last-child{color:#657069;white-space:nowrap}.run-details{width:100%;margin-top:10px}.run-details summary{cursor:pointer;color:#256242;font-size:12px;font-weight:700}.run-counts{font-size:12px;color:#657069;margin-top:5px}.live-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#2e7953;margin-right:6px}.live-dot.offline{background:#a54b45}
#transfer-list{max-height:min(65vh,720px);overflow-y:auto;overscroll-behavior:contain}.transfer-group{border-top:1px solid #edf0ec}.transfer-group:first-child{border-top:0}.transfer-group>summary{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:15px 20px;cursor:pointer;font-size:14px;font-weight:700;list-style:none;background:#fff}.transfer-group>summary::-webkit-details-marker{display:none}.transfer-group>summary:before{content:'›';font-size:20px;line-height:1;color:#67766b;transition:transform .15s}.transfer-group[open]>summary:before{transform:rotate(90deg)}.transfer-group>summary span:first-of-type{flex:1;overflow-wrap:anywhere}.transfer-group>summary .count{white-space:nowrap}.transfer-group>summary:focus-visible{outline:2px solid #28734e;outline-offset:-3px}.transfer-group .row{border-top:1px solid #edf0ec}
@media(max-width:720px){.shell{padding:0 17px 42px}.top{height:auto;min-height:72px;align-items:flex-start;flex-wrap:wrap;padding:16px 0}.nav{width:100%;overflow:auto}.nav a{padding:8px 10px}.hero{padding:35px 0 27px}.grid{grid-template-columns:1fr}.card{padding:22px}.field{flex-direction:column}.row{padding:15px;align-items:flex-start}.right{gap:10px;flex-wrap:wrap;justify-content:flex-end}.transfer-top{align-items:flex-start}.section{margin-top:31px}}
"""

LIVE_JS = r"""
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const bytes=n=>{let u=['B','KB','MB','GB','TB'],i=0;n=Number(n)||0;while(n>=1024&&i<4){n/=1024;i++}return (i?n.toFixed(1):Math.round(n))+' '+u[i]};
let snapshot={transfers:[],runs:[],error:null};
const controls={search:document.getElementById('transfer-search'),status:document.getElementById('transfer-status'),sort:document.getElementById('transfer-sort'),reverse:document.getElementById('transfer-reverse')};
const transferList=document.getElementById('transfer-list'),groupOpen=new Map();
transferList.addEventListener('click',e=>{const summary=e.target.closest('summary');if(summary&&transferList.contains(summary))groupOpen.set(summary.parentElement.dataset.key,!summary.parentElement.open)});
function renderTransfers(){
  const query=controls.search.value.trim().toLowerCase(),status=controls.status.value,sort=controls.sort.value,reverse=controls.reverse.dataset.reverse==='true';
  let rows=snapshot.transfers.filter(t=>(!status||t.kind===status)&&(!query||(t.name+' '+t.username+' '+t.folder).toLowerCase().includes(query)));
  const order={'completed':0,'downloading':1,'queued locally':2,'queued remotely':3,'failed':4,'unknown':5};
  rows.sort((a,b)=>{let x=sort==='status'?(order[a.kind]??9)-(order[b.kind]??9):sort==='progress'?a.percent-b.percent:sort==='speed'?a.speed-b.speed:sort==='newest'?(a.date&&b.date?String(a.date).localeCompare(String(b.date))||b.order-a.order:b.order-a.order):a.name.localeCompare(b.name);return (reverse?-1:1)*(x||a.name.localeCompare(b.name))});
  const runByBatch=new Map();
  for(const run of snapshot.runs||[])for(const track of run.tracks)if(track.batchId)runByBatch.set(track.batchId,run);
  const groups=new Map();
  for(const t of rows){const run=runByBatch.get(t.batchId),key=run?(run.submissionId||run.submitted||run.id):'other';if(!groups.has(key))groups.set(key,{name:run?.name||'Other transfers',rows:[]});groups.get(key).rows.push(t)}
  const focused=document.activeElement?.closest('#transfer-list summary')?.parentElement?.dataset.key,scroll=transferList.scrollTop;
  transferList.innerHTML=snapshot.error?'<div class="empty">'+esc(snapshot.error)+'</div>':groups.size?[...groups].map(([key,group])=>'<details class="transfer-group" data-key="'+esc(key)+'" '+(groupOpen.get(key)===false?'':'open')+'><summary><span>'+esc(group.name)+'</span><span class="count">'+group.rows.length+' transfer'+(group.rows.length===1?'':'s')+'</span></summary>'+group.rows.map(t=>{
    const active=(snapshot.runs||[]).some(r=>r.tracks.some(track=>track.fallback&&track.source===t.username&&track.title&&t.name.toLowerCase().includes(track.title.toLowerCase())));
    const label=active&&t.kind!=='completed'?'Trying another source · '+t.state:t.state;
    return '<div class="row transfer"><div class="transfer-top"><div><strong>'+esc(t.name)+'</strong><div class="muted">'+esc(t.username)+' · '+esc(t.folder)+'</div></div><span class="badge '+(t.kind==='failed'?'failed':t.kind.includes('queued')?'waiting':'')+'">'+esc(label)+'</span></div><div class="progress" role="progressbar" aria-label="'+esc(t.name)+'" aria-valuenow="'+Math.round(t.percent)+'" aria-valuemin="0" aria-valuemax="100"><span style="width:'+t.percent+'%"></span></div><div class="transfer-bottom"><span>'+bytes(t.done)+' of '+bytes(t.size)+(t.speed?' · '+bytes(t.speed)+'/s':'')+(t.error?' · '+esc(t.error):'')+'</span><strong>'+Math.round(t.percent)+'%</strong></div></div>'
  }).join('')+'</details>').join(''):'<div class="empty">No matching transfers.</div>';
  transferList.scrollTop=scroll;
  if(focused)[...transferList.querySelectorAll('details')].find(group=>group.dataset.key===focused)?.querySelector('summary')?.focus({preventScroll:true});
}
function renderRuns(){
  const list=document.getElementById('run-list'),open=new Set([...list.querySelectorAll('details[open]')].map(x=>x.dataset.id));
  list.innerHTML=snapshot.runs.length?snapshot.runs.map((r,i)=>{
    const c=r.counts,total=r.total==null?'Unknown total':r.total+' tracks';
    const counts=['completed','downloading','queued locally','queued remotely','failed','unknown','no match','needs review'].map(k=>c[k]+' '+k).join(' · ');
    const tracks=r.tracks.map(t=>'<div class="track-row"><span>'+esc((t.artist?t.artist+' — ':'')+t.title)+'</span><span>'+esc(t.fallback&&t.status!=='completed'?'Trying another source · '+t.status:t.status)+'</span></div>').join('');
    const key=esc(r.submissionId||r.submitted||i);
    return '<div class="row"><div style="width:100%"><strong>'+esc(r.name||'Spotify playlist '+r.id)+'</strong><div class="muted">'+esc((r.submitted||'').slice(0,16).replace('T',' '))+' UTC · '+esc(r.phase)+'</div><div class="run-counts">'+esc(total)+' · '+esc(counts)+' · '+c.fallback+' alternate attempted</div><details class="run-details" data-id="'+key+'" '+(open.has(key)?'open':'')+'><summary>View tracks</summary><div class="track-list">'+(tracks||'<div class="muted">Track details are not available yet.</div>')+'</div></details></div></div>'
  }).join(''):'<div class="empty">No playlists submitted yet.</div>';
}
for(const control of [controls.search,controls.status,controls.sort])control.addEventListener(control===controls.search?'input':'change',renderTransfers);
controls.reverse.addEventListener('click',()=>{let reverse=controls.reverse.dataset.reverse!=='true';controls.reverse.dataset.reverse=String(reverse);controls.reverse.textContent=reverse?'Descending':'Ascending';renderTransfers()});
const stream=new EventSource('/events');
stream.onmessage=e=>{snapshot=JSON.parse(e.data);document.getElementById('live-status').innerHTML='<span class="live-dot"></span>Live';renderTransfers();renderRuns()};
stream.onerror=()=>{document.getElementById('live-status').innerHTML='<span class="live-dot offline"></span>Reconnecting…'};
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
                raise ValueError('N8N_WEBHOOK_URL is not configured')
            headers = {'Content-Type': 'application/json'}
            if os.environ.get('N8N_WEBHOOK_TOKEN'):
                headers['Authorization'] = 'Bearer ' + os.environ['N8N_WEBHOOK_TOKEN']
            submission_id = uuid.uuid4().hex
            req = urllib.request.Request(webhook, json.dumps({'url': url, 'playlistId': ident, 'submissionId': submission_id}).encode(), headers)
            with urllib.request.urlopen(req, timeout=15) as response:
                if response.status >= 300:
                    raise ValueError('Workflow rejected submission')
            name = playlist_name(ident) or 'Spotify playlist ' + ident
            with STATE_LOCK:
                runs = read_runs()
                runs.insert(0, {'id': ident, 'submissionId': submission_id, 'name': name, 'url': url, 'submitted': datetime.now(timezone.utc).isoformat(), 'status': 'submitted'})
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
            if not 0 < length <= 262144:
                raise ValueError('Invalid status size')
            payload = json.loads(self.rfile.read(length))
            ident = payload.get('playlistId') if isinstance(payload, dict) else None
            ident = ident if isinstance(ident, str) and re.fullmatch(r'[A-Za-z0-9]{22}', ident) else None
            if not ident or not isinstance(payload.get('state'), dict):
                raise ValueError('Invalid workflow status')
            state = payload['state']
            clean = {key: state.get(key) for key in ('playlistName', 'totalSpotifyTracks', 'queuedDownloads', 'downloads', 'problems', 'completedFiles', 'failedFiles', 'pendingFiles', 'missingBatches', 'fallbackAttempts', 'timedOut', 'noMatch', 'reviewRequired', 'errors')}
            clean['phase'] = payload.get('phase') if payload.get('phase') in ('queued', 'progress', 'final') else 'progress'
            with STATE_LOCK:
                runs = read_runs()
                run = next((item for item in runs if item.get('id') == ident and item.get('submissionId') == payload.get('submissionId') and item.get('submissionId')), None)
                if run:
                    phases = {'queued': 0, 'progress': 1, 'final': 2}
                    previous = run.get('workflow') or {}
                    if phases[clean['phase']] >= phases.get(previous.get('phase'), -1) and (clean.get('fallbackAttempts') or 0) >= (previous.get('fallbackAttempts') or 0):
                        run['workflow'] = clean
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
        previous = None
        try:
            while True:
                transfers, error = transfer_snapshot()
                data = json.dumps({'transfers': transfers, 'runs': [run_view(run, transfers) for run in read_runs()[:8]], 'error': error}, ensure_ascii=False)
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
        try:
            directory = inside(folder)
            if not directory.is_dir():
                raise ValueError('Folder not found')
            items = sorted((p for p in directory.iterdir() if p.resolve().is_relative_to(ROOT) and not p.is_symlink()), key=lambda p: file_sort_key(p, sort), reverse=direction == 'desc')
        except (ValueError, OSError):
            self.send(page('<div class="error">Folder not found</div>'), status=404)
            return
        crumbs = ['<a href="/?' + sort_query.lstrip('&') + '#files">Downloads</a>']
        current = Path()
        for part in Path(folder).parts if folder else []:
            current /= part
            crumbs.append('<span>›</span><a href="/?folder=' + urllib.parse.quote(str(current)) + sort_query + '#files">' + html.escape(part) + '</a>')
        rows = []
        for item in items:
            rel = str(item.relative_to(ROOT))
            quoted = urllib.parse.quote(rel)
            delete = '<form class="inline-form" method="post" action="/delete" onsubmit="return confirm(\'Delete this item permanently?\')"><input type="hidden" name="path" value="' + html.escape(rel, quote=True) + '"><button class="danger" type="submit">Delete</button></form>'
            if item.is_dir():
                count = sum(1 for child in item.iterdir() if not child.is_symlink() and child.resolve().is_relative_to(ROOT))
                rows.append(f'<div class="row"><div><a class="filename" href="/?folder={quoted}{sort_query}#files"><span class="file-icon">▣</span><strong>{safe_name(item)}</strong></a><div class="muted">{count} item{"s" if count != 1 else ""} · Modified {datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d")}</div></div><div class="right"><a class="link" href="/?folder={quoted}{sort_query}#files">Open</a><a class="link" href="/zip?path={quoted}">Download ZIP</a>{delete}</div></div>')
            elif item.is_file():
                rows.append(f'<div class="row"><div><div class="filename"><span class="file-icon">♪</span><strong>{safe_name(item)}</strong></div><div class="muted">{size_label(item.stat().st_size)} · Modified {datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d")}</div></div><div class="right"><a class="link" href="/file?path={quoted}">Download</a>{delete}</div></div>')
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
        body += '<section class="section" id="activity"><div class="sectionhead"><div><h2>Recent submissions</h2><p class="muted">Workflow and download status.</p></div><form class="inline-form" method="post" action="/clear-history" onsubmit="return confirm(\'Clear recent submissions? This will not delete downloaded files.\')"><button class="danger" type="submit">Clear history</button></form></div><div class="list" id="run-list">' + (''.join(runrows) or '<div class="empty">No playlists submitted yet.</div>') + '</div></section>'
        body += '<section class="section" id="transfers"><div class="sectionhead"><div><h2>Transfers</h2><p class="muted">Live download progress from slskd.</p></div><span class="refresh" id="live-status" aria-live="polite">Connecting…</span></div><div class="controls"><input id="transfer-search" type="search" placeholder="Search transfers" aria-label="Search transfers"><select id="transfer-status" aria-label="Filter transfer status"><option value="">All statuses</option><option>completed</option><option>downloading</option><option>queued locally</option><option>queued remotely</option><option>failed</option><option>unknown</option></select><select id="transfer-sort" aria-label="Sort transfers"><option value="newest">Newest</option><option value="status">Status</option><option value="progress">Progress</option><option value="name">Name</option><option value="speed">Speed</option></select><button id="transfer-reverse" type="button" data-reverse="true">Descending</button></div><div class="list" id="transfer-list"><div class="empty">Connecting to slskd…</div></div></section>'
        body += '<section class="section" id="files"><div class="sectionhead"><div><h2>Completed downloads</h2><div class="crumbs">' + ''.join(crumbs) + '</div></div><a class="link" href="' + html.escape(self.path, quote=True) + '#files">Refresh files</a></div><form class="controls" method="get" action="/"><input type="hidden" name="folder" value="' + html.escape(folder, quote=True) + '"><label for="file-sort">Sort files</label><select id="file-sort" name="sort"><option value="name"' + (' selected' if sort == 'name' else '') + '>Name</option><option value="type"' + (' selected' if sort == 'type' else '') + '>Type</option><option value="size"' + (' selected' if sort == 'size' else '') + '>Size</option><option value="modified"' + (' selected' if sort == 'modified' else '') + '>Modified</option></select><select name="dir" aria-label="File sort direction"><option value="asc"' + (' selected' if direction == 'asc' else '') + '>Ascending</option><option value="desc"' + (' selected' if direction == 'desc' else '') + '>Descending</option></select><button type="submit">Apply</button></form><div class="list">' + (''.join(rows) or '<div class="empty">No completed files here yet.</div>') + '</div></section>'
        body += '<script>' + LIVE_JS + '</script>'
        self.send(page(body))

if __name__ == '__main__':
    assert playlist_id('spotify:playlist:37i9dQZF1DXcBWIGoYBM5M') == '37i9dQZF1DXcBWIGoYBM5M'
    assert playlist_id('https://evil.example/playlist/37i9dQZF1DXcBWIGoYBM5M') is None
    ROOT.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
