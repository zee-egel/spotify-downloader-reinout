"""Safe local smoke check; no calls to n8n or slskd."""
import base64
import io
import json
import os
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class MockWebhook(BaseHTTPRequestHandler):
    received = []
    status = 200

    def do_POST(self):
        if self.status == 200:
            self.received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        self.send_response(self.status)
        self.end_headers()


class MockSlskd(BaseHTTPRequestHandler):
    def do_GET(self):
        data = [{'username': 'fast-peer', 'directories': [{'directory': 'My Playlist', 'files': [{'batchId': 'new-batch', 'filename': r'C:\\music\\Track <one>.mp3', 'state': 'InProgress', 'size': 1000, 'bytesTransferred': 500, 'averageSpeed': 100}]}]}]
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


with tempfile.TemporaryDirectory() as temporary:
    os.environ.update(APP_PASSWORD='secret', N8N_WEBHOOK_TOKEN='callback-secret', DOWNLOADS_ROOT=temporary, STATE_FILE=temporary + '/state.json')
    import app
    app.ROOT = Path(temporary).resolve()
    app.STATE = Path(temporary + '/state.json')
    app.playlist_name = lambda ident: 'Test Playlist <demo>'
    (Path(temporary) / 'Playlist').mkdir()
    (Path(temporary) / 'Playlist' / 'song.mp3').write_bytes(b'music')
    (Path(temporary) / 'escape').symlink_to('/etc')
    webhook = ThreadingHTTPServer(('127.0.0.1', 0), MockWebhook)
    threading.Thread(target=webhook.serve_forever, daemon=True).start()
    os.environ['N8N_WEBHOOK_URL'] = f'http://127.0.0.1:{webhook.server_port}/webhook'
    slskd = ThreadingHTTPServer(('127.0.0.1', 0), MockSlskd)
    threading.Thread(target=slskd.serve_forever, daemon=True).start()
    os.environ['SLSKD_URL'] = f'http://127.0.0.1:{slskd.server_port}'
    server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    auth = 'Basic ' + base64.b64encode(b'admin:secret').decode()

    def fetch(path, data=None, authorized=True, origin=None):
        headers = {'Authorization': auth} if authorized else {}
        if origin:
            headers['Origin'] = origin
        return urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers))

    try:
        fetch('/', authorized=False)
        assert False
    except urllib.error.HTTPError as error:
        assert error.code == 401
    dashboard = fetch('/').read()
    assert b'Playlist downloads.' in dashboard
    assert b'href="/#files"' in dashboard
    assert b'new EventSource' in dashboard
    assert b'aria-valuenow="50"' in fetch('/transfers').read()
    assert b'&lt;one&gt;' in fetch('/transfers').read()
    with fetch('/events') as events:
        first = events.readline()
        assert first.startswith(b'data: ')
        assert json.loads(first[6:])['transfers'][0]['kind'] == 'downloading'
    assert fetch('/file?path=Playlist/song.mp3').read() == b'music'
    with zipfile.ZipFile(io.BytesIO(fetch('/zip?path=Playlist').read())) as archive:
        assert archive.read('song.mp3') == b'music'
    for path in ('/file?path=../etc/passwd', '/file?path=escape/passwd'):
        try:
            fetch(path)
            assert False
        except urllib.error.HTTPError as error:
            assert error.code == 400
    data = urllib.parse.urlencode({'url': 'spotify:playlist:37i9dQZF1DXcBWIGoYBM5M'}).encode()
    try:
        fetch('/submit', data, origin='https://evil.example')
        assert False
    except urllib.error.HTTPError as error:
        assert error.code == 403
    MockWebhook.status = 403
    try:
        fetch('/submit', data)
        assert False
    except urllib.error.HTTPError as error:
        assert error.code == 502
        assert b'webhook credentials' in error.read()
    MockWebhook.status = 200
    assert fetch('/submit', data).status == 200  # follows redirect
    assert MockWebhook.received[0]['playlistId'] == '37i9dQZF1DXcBWIGoYBM5M'
    assert len(MockWebhook.received[0]['submissionId']) == 32
    assert app.read_runs()[0]['status'] == 'submitted'
    assert app.read_runs()[0]['name'] == 'Test Playlist <demo>'
    assert b'Test Playlist &lt;demo&gt;' in fetch('/').read()
    assert b'1 item' in fetch('/').read()
    callback = {'playlistId': app.read_runs()[0]['id'], 'submissionId': app.read_runs()[0]['submissionId'], 'phase': 'progress',
                'state': {'totalSpotifyTracks': 1, 'downloads': [{'batchId': 'new-batch', 'artist': 'Artist', 'title': 'Track <one>', 'fallbackAttempted': True, 'fallbackSource': 'fast-peer', 'fallbackStatus': 'queued'}], 'problems': []}}
    request = urllib.request.Request(base + '/workflow-status', json.dumps(callback).encode(), headers={'X-Playlist-Token': 'callback-secret', 'Content-Type': 'application/json'})
    assert urllib.request.urlopen(request).status == 200
    view = app.run_view(app.read_runs()[0], app.transfer_snapshot()[0])
    assert view['counts']['downloading'] == 1 and view['counts']['fallback'] == 1
    assert view['tracks'][0]['source'] == 'fast-peer'
    complete = [dict(app.transfer_snapshot()[0][0], kind='completed'), {'batchId': 'old-batch', 'kind': 'failed'}]
    assert app.run_view(app.read_runs()[0], complete)['counts']['failed'] == 0
    callback['phase'] = 'queued'
    urllib.request.urlopen(urllib.request.Request(base + '/workflow-status', json.dumps(callback).encode(), headers={'X-Playlist-Token': 'callback-secret'}))
    assert app.read_runs()[0]['workflow']['phase'] == 'progress'
    assert app.transfer_kind('Queued, Remotely') == 'queued remotely'
    assert app.transfer_kind('Completed, Succeeded') == 'completed'
    assert app.file_sort_key(Path(temporary) / 'Playlist' / 'song.mp3', 'size') == 5
    (Path(temporary) / 'Playlist' / 'aaa.txt').write_bytes(b'x' * 10)
    sorted_page = fetch('/?folder=Playlist&sort=size&dir=desc').read()
    assert sorted_page.index(b'aaa.txt') < sorted_page.index(b'song.mp3')
    app.save_runs([{'id': '37i9dQZF1DXcBWIGoYBM5M', 'url': 'spotify:playlist:37i9dQZF1DXcBWIGoYBM5M', 'submitted': '2026-09-30T00:00:00', 'status': 'submitted'}])
    assert b'Test Playlist &lt;demo&gt;' in fetch('/').read()
    assert app.read_runs()[0]['name'] == 'Test Playlist <demo>'
    try:
        fetch('/delete', urllib.parse.urlencode({'path': 'Playlist/song.mp3'}).encode(), origin='https://evil.example')
        assert False
    except urllib.error.HTTPError as error:
        assert error.code == 403
    for path in ('../outside', 'escape/passwd', ''):
        try:
            fetch('/delete', urllib.parse.urlencode({'path': path}).encode(), origin=base)
            assert False
        except urllib.error.HTTPError as error:
            assert error.code == 400
    fetch('/delete', urllib.parse.urlencode({'path': 'Playlist/song.mp3'}).encode(), origin=base)
    assert not (Path(temporary) / 'Playlist' / 'song.mp3').exists()
    (Path(temporary) / 'Playlist' / 'nested.txt').write_text('fixture')
    fetch('/delete', urllib.parse.urlencode({'path': 'Playlist'}).encode(), origin=base)
    assert not (Path(temporary) / 'Playlist').exists()
    fetch('/clear-history', b'', origin=base)
    assert app.read_runs() == []
    server.shutdown()
    webhook.shutdown()
    slskd.shutdown()
print('Local smoke checks passed')
