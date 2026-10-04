"""Isolated profile/authentication checks, without real Spotify, Telegram, or slskd: python3 test_profiles.py."""
import base64
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import app
import profiles
from workflow_profiles import patch


class QuietHandler(app.Handler):
    def log_message(self, *args):
        pass


class Webhook(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        assert self.headers['X-Playlist-Token'] == 'workflow-secret'
        self.received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        self.send_response(200)
        self.end_headers()


with tempfile.TemporaryDirectory() as temporary:
    os.environ.update(APP_USER='admin', APP_PASSWORD='owner-password', EXTRA_APP_USER='friend',
        EXTRA_APP_PASSWORD='friend-password', STATE_FILE=temporary + '/runs.json',
        SPOTIFY_CLIENT_ID='spotify-client', APP_PUBLIC_URL='https://dj.reinout.dance', N8N_WEBHOOK_TOKEN='workflow-secret')
    app.ROOT = (Path(temporary) / 'downloads').resolve()
    app.STATE = Path(os.environ['STATE_FILE'])
    private = app.ROOT / 'profiles' / 'extra'
    private.mkdir(parents=True)
    (app.ROOT / 'owner.mp3').write_bytes(b'owner file')
    (private / 'friend.mp3').write_bytes(b'friend file')
    (private / 'escape').symlink_to(app.ROOT / 'owner.mp3')
    ident = '6aCO6mHPH4X5J7JW6lJKcl'
    app.save_runs([{'id': ident, 'submissionId': 'owner-run', 'name': 'Owner playlist',
        'submitted': '2026-10-01T00:00:00+00:00', 'workflow': {'phase': 'final', 'downloads': [{'batchId': 'owner-batch'}]}}])
    rows = [{'batchId': batch, 'username': 'peer', 'filename': title, 'name': title,
             'folder': '', 'kind': 'completed', 'state': 'Completed', 'size': 100, 'done': 100,
             'percent': 100, 'speed': 0, 'date': '2026-10-04T00:00:00+00:00'}
            for batch, title in [('owner-batch', 'owner.mp3'), ('friend-batch', 'friend.mp3'), ('unknown', 'hidden.mp3')]]
    app.transfer_snapshot = lambda: (rows, None)
    calls = []

    def service(url, *, headers=None, data=None):
        calls.append((url, headers, data))
        if url.endswith('/getMe'):
            return {'ok': True, 'result': {'username': 'friend_bot'}}
        if url.endswith('/getChat'):
            return {'ok': True, 'result': {'id': 123}}
        if url.endswith('/sendMessage'):
            return {'ok': True, 'result': {'message_id': 1}}
        if url.endswith('/api/token'):
            return {'access_token': 'fresh-access', 'refresh_token': 'refresh-secret', 'expires_in': 3600}
        if url.endswith('/v1/me'):
            return {'id': 'friend-spotify', 'display_name': 'Friend Spotify'}
        assert headers['Authorization'] == 'Bearer fresh-access'
        if '/items?' in url:
            return {'items': [{'item': {'id': 'song', 'name': 'Song', 'artists': [{'name': 'Artist'}], 'duration_ms': 1000}}], 'next': None}
        return {'name': 'Friend playlist', 'id': ident}

    profiles.json_request = service
    webhook = ThreadingHTTPServer(('127.0.0.1', 0), Webhook)
    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    for instance in (webhook, server):
        threading.Thread(target=instance.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    os.environ['N8N_WEBHOOK_URL'] = f'http://127.0.0.1:{webhook.server_port}/webhook'

    def fetch(path, user='friend', form=None, callback=None, origin=True, password=None):
        password = password or ('friend-password' if user == 'friend' else 'owner-password')
        headers = {'Authorization': 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            if origin:
                headers['Origin'] = base
        if callback is not None:
            data = json.dumps(callback).encode()
            headers = {'X-Playlist-Token': 'workflow-secret', 'Content-Type': 'application/json'}
        return urllib.request.urlopen(urllib.request.Request(base + path, data, headers), timeout=5)

    def rejected(path, status, **kwargs):
        try:
            fetch(path, **kwargs)
            assert False, path
        except urllib.error.HTTPError as error:
            assert error.code == status, (path, error.code)

    rejected('/', 401, password='bad-password')
    friend_page = fetch('/').read()
    assert b'owner.mp3' not in friend_page and b'Owner playlist' not in friend_page
    assert b'friend.mp3' in friend_page
    owner_page = fetch('/', user='admin').read()
    assert b'owner.mp3' in owner_page and b'profiles' in owner_page
    rejected('/file?path=../../owner.mp3', 400)
    rejected('/file?path=escape', 400)
    assert fetch('/file?path=profiles/extra/friend.mp3', user='admin').read() == b'friend file'
    rejected('/delete', 400, form={'path': '../../owner.mp3'})
    rejected('/delete', 400, user='admin', form={'path': 'profiles'})
    assert fetch('/file?path=friend.mp3').read() == b'friend file'
    (private / 'Playlist').mkdir()
    (private / 'Playlist' / 'song.mp3').write_bytes(b'music')
    archive = zipfile.ZipFile(io.BytesIO(fetch('/zip?path=Playlist').read()))
    assert archive.namelist() == ['song.mp3']
    assert b'owner.mp3' in fetch('/transfers', user='admin').read()
    assert b'friend.mp3' in fetch('/transfers', user='admin').read()
    assert b'hidden.mp3' in fetch('/transfers', user='admin').read()
    assert b'No transfers' in fetch('/transfers').read()
    rejected('/profile/telegram', 403, form={'token': '123:bot-secret', 'chat': '123'}, origin=False)
    rejected('/profile/telegram', 400, form={'token': 'https://evil.example', 'chat': '123'})
    assert fetch('/profile/telegram', form={'token': '123:bot-secret', 'chat': '123'}).status == 200
    assert profiles.load('owner') == {}
    settings_page = fetch('/profile').read()
    assert b'bot-secret' not in settings_page and b'friend_bot' in settings_page
    # OAuth state belongs to the authenticated account, expires, and can be used only once.
    authorize = profiles.spotify_start('extra')
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(authorize).query)
    assert query['code_challenge_method'] == ['S256']
    assert query['redirect_uri'] == ['https://dj.reinout.dance/spotify/callback']
    state = query['state'][0]
    rejected('/spotify/callback?code=one-time&state=' + state, 400, user='admin')
    assert fetch('/spotify/callback?code=one-time&state=' + state).status == 200
    rejected('/spotify/callback?code=one-time&state=' + state, 400)
    assert profiles.load('extra')['spotify']['name'] == 'Friend Spotify'
    assert not profiles.load('owner').get('spotify')
    profiles.update('extra', spotify={**profiles.load('extra')['spotify'], 'expires': 0})
    assert fetch('/submit', form={'url': 'spotify:playlist:' + ident}).status == 200
    submission = Webhook.received[-1]
    assert submission['profileId'] == 'extra' and submission['downloadDirectory'] == 'profiles/extra'
    assert submission['playlist']['name'] == 'Friend playlist'
    assert 'access_token' not in json.dumps(submission) and 'refresh-secret' not in json.dumps(submission)
    rejected('/submit', 400, user='admin', form={'url': 'spotify:playlist:' + ident})  # no owner's Spotify linked
    callback = {'playlistId': ident, 'submissionId': submission['submissionId'], 'owner': 'owner', 'phase': 'final',
                'state': {'downloads': [{'batchId': 'friend-batch', 'artist': 'Artist', 'title': 'Friend Song'}]}}
    assert fetch('/workflow-status', callback=callback).status == 200
    assert b'Friend Song' in fetch('/').read() and b'Friend Song' not in fetch('/', user='admin').read()
    assert b'friend.mp3' in fetch('/transfers').read() and b'friend.mp3' in fetch('/transfers', user='admin').read()
    # Clearing a profile's visible history keeps transfer ownership and the other user's history.
    assert fetch('/clear-history', form={}).status == 200
    assert b'Friend playlist' not in fetch('/').read() and b'Owner playlist' in fetch('/', user='admin').read()
    assert b'friend.mp3' in fetch('/transfers').read()
    with fetch('/events') as response:
        event = json.loads(response.readline().decode().removeprefix('data: '))
        assert event['runs'] == [] and [row['batchId'] for row in event['transfers']] == ['friend-batch']
        assert 'owner.mp3' not in event['library']['html']
    notification = {'submissionId': submission['submissionId'], 'owner': 'owner', 'message': 'Finished'}
    assert fetch('/workflow-notify', callback=notification).status == 200
    assert fetch('/workflow-notify', callback=notification).status == 200
    sent = [call for call in calls if call[0].endswith('/sendMessage')]
    assert len(sent) == 1 and json.loads(sent[0][2])['chat_id'] == '123'
    assert 'bot123:bot-secret/' in sent[0][0]
    assert profiles.load('owner') == {}
    assert (Path(temporary) / 'profiles.sqlite3').stat().st_mode & 0o777 == 0o600
    for instance in (server, webhook):
        instance.shutdown()

# Execute the workflow's generated expressions to check profile destinations and payloads.
names = ['Get Playlist', 'HTTP Request', 'Queue Download', 'Queue Fallback', 'Send a text message', 'Send Queue Summary', 'Send Remote Pending']
workflow = {'name': 'Test', 'settings': {}, 'connections': {}, 'nodes': [
    {'name': name, 'type': 'test', 'parameters': {'jsonBody': '={{ { options: { destination: $json.playlistName } } }}', 'text': '={{ $json.message }}'}} for name in names]}
workflow['nodes'] += [{'name': 'Playlist desk webhook', 'parameters': {}, 'credentials': {'httpHeaderAuth': {'id': 'five', 'name': 'Header Auth account 5'}}},
                      {'name': 'Report Queue Status', 'parameters': {'url': 'https://dj.reinout.dance/workflow-status'}}]
patched = patch(workflow)
assert workflow['nodes'][0]['type'] == 'test'
nodes = {n['name']: n for n in patched['nodes']}
subprocess.run(['node', '-e', '''
const assert = require('node:assert/strict');
const nodes = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const body = {profileId:'extra',downloadDirectory:'profiles/extra',playlist:{name:'Private'},playlistItems:{items:[]},submissionId:'run'};
const $ = () => ({first:()=>({json:{body}})});
const $json = {playlistName:'Private',message:'Finished'};
assert.equal(new Function('$',nodes['Get Playlist'].parameters.jsCode)($)[0].json.name,'Private');
for (const name of ['Queue Download','Queue Fallback']) {
  const expression = nodes[name].parameters.jsonBody.slice(3,-2);
  assert.equal(eval('('+expression+')').options.destination,'profiles/extra/Private');
}
const notification = eval('('+nodes['Send a text message'].parameters.jsonBody.slice(3,-2)+')');
assert.deepEqual(notification,{submissionId:'run',message:'Finished'});
body.downloadDirectory='../owner';
assert.throws(()=>new Function('$',nodes['Get Playlist'].parameters.jsCode)($));
'''], input=json.dumps(nodes), text=True, check=True)
print('Profile isolation, OAuth, Telegram, and workflow routing checks passed')
