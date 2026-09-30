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


with tempfile.TemporaryDirectory() as temporary:
    os.environ.update(APP_PASSWORD='secret', DOWNLOADS_ROOT=temporary, STATE_FILE=temporary + '/state.json')
    import app
    app.ROOT = Path(temporary).resolve()
    app.STATE = Path(temporary + '/state.json')
    (Path(temporary) / 'Playlist').mkdir()
    (Path(temporary) / 'Playlist' / 'song.mp3').write_bytes(b'music')
    (Path(temporary) / 'escape').symlink_to('/etc')
    webhook = ThreadingHTTPServer(('127.0.0.1', 0), MockWebhook)
    threading.Thread(target=webhook.serve_forever, daemon=True).start()
    os.environ['N8N_WEBHOOK_URL'] = f'http://127.0.0.1:{webhook.server_port}/webhook'
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
    assert b'Playlist downloads.' in fetch('/').read()
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
    assert MockWebhook.received == [{'url': 'spotify:playlist:37i9dQZF1DXcBWIGoYBM5M', 'playlistId': '37i9dQZF1DXcBWIGoYBM5M'}]
    assert app.read_runs()[0]['status'] == 'submitted'
    server.shutdown()
    webhook.shutdown()
print('Local smoke checks passed')
