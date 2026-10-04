"""Two Basic-auth accounts and their private integrations; SQLite is in the state volume."""
import base64
import hashlib
import html
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from contextlib import contextmanager

LOCK = threading.RLock()


@contextmanager
def database():
    path = Path(os.environ.get('STATE_FILE', './state.json')).with_name('profiles.sqlite3')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path)
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            yield db
    finally:
        db.close()


def load(user):
    with LOCK, database() as db:
        row = db.execute('SELECT data FROM profiles WHERE id=?', (user,)).fetchone()
        return json.loads(row[0]) if row else {}


def update(user, **values):
    with LOCK, database() as db:
        data = load(user)
        data.update(values)
        db.execute('INSERT OR REPLACE INTO profiles VALUES (?, ?)', (user, json.dumps(data)))
        return data


def accounts():
    owner = os.environ.get('APP_USER', 'admin')
    extra = os.environ.get('EXTRA_APP_USER', '')
    if extra and extra == owner:
        raise ValueError('The two account names must differ')
    result = {owner: ('owner', os.environ.get('APP_PASSWORD', ''))}
    if extra and os.environ.get('EXTRA_APP_PASSWORD'):
        result[extra] = ('extra', os.environ['EXTRA_APP_PASSWORD'])
    return result


def json_request(url, *, headers=None, data=None):
    req = urllib.request.Request(url, data, headers or {})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def redirect_uri():
    base = os.environ.get('APP_PUBLIC_URL', '').rstrip('/')
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('Set APP_PUBLIC_URL to the HTTPS address of this app')
    return base + '/spotify/callback'


def spotify_start(user):
    client = os.environ.get('SPOTIFY_CLIENT_ID', '')
    if not client:
        raise ValueError('Spotify linking has not been configured yet')
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(32)
    update(user, oauth={'state': state, 'verifier': verifier, 'expires': time.time() + 600})
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    return 'https://accounts.spotify.com/authorize?' + urllib.parse.urlencode({
        'client_id': client, 'response_type': 'code', 'redirect_uri': redirect_uri(),
        'scope': 'playlist-read-private playlist-read-collaborative', 'state': state,
        'code_challenge_method': 'S256', 'code_challenge': challenge, 'show_dialog': 'true'})


def spotify_tokens(values):
    values['client_id'] = os.environ.get('SPOTIFY_CLIENT_ID', '')
    return json_request('https://accounts.spotify.com/api/token',
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        data=urllib.parse.urlencode(values).encode())


def spotify_finish(user, query):
    import hmac
    with LOCK:
        pending = load(user).get('oauth') or {}
        state = query.get('state', [''])[0]
        if not state or not hmac.compare_digest(state, pending.get('state', '')) or pending.get('expires', 0) < time.time():
            raise ValueError('Spotify login expired or does not belong to this account. Try connecting again.')
        update(user, oauth=None)
    if query.get('error') or not query.get('code'):
        raise ValueError('Spotify connection was canceled')
    tokens = spotify_tokens({'grant_type': 'authorization_code', 'code': query['code'][0],
                             'redirect_uri': redirect_uri(), 'code_verifier': pending['verifier']})
    me = json_request('https://api.spotify.com/v1/me', headers={'Authorization': 'Bearer ' + tokens['access_token']})
    tokens.update(expires=time.time() + tokens['expires_in'], name=me.get('display_name') or me['id'])
    update(user, spotify=tokens)


def spotify_get(user, path):
    if not path.startswith('/v1/'):
        raise ValueError('Invalid Spotify path')
    with LOCK:
        tokens = load(user).get('spotify')
        if not tokens:
            raise ValueError('Connect your Spotify account in Profile before importing')
        if tokens.get('expires', 0) < time.time() + 60:
            fresh = spotify_tokens({'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token']})
            tokens = {**tokens, **fresh, 'expires': time.time() + fresh['expires_in']}
            update(user, spotify=tokens)
    return json_request('https://api.spotify.com' + path, headers={'Authorization': 'Bearer ' + tokens['access_token']})


def spotify_playlist(user, ident):
    playlist = spotify_get(user, '/v1/playlists/' + ident)
    items, offset = [], 0
    while True:
        page = spotify_get(user, f'/v1/playlists/{ident}/items?limit=50&offset={offset}')
        items.extend(page['items'])
        if not page.get('next'):
            break
        if not page['items']:
            raise ValueError('Spotify returned an incomplete playlist')
        offset += len(page['items'])
    return {'id': ident, 'name': playlist.get('name', 'Spotify playlist')}, {'items': items, 'next': None}


def telegram(user, method, payload=None):
    settings = load(user).get('telegram') or {}
    token = settings.get('token', '')
    if not re_token(token):
        raise ValueError('Add your Telegram bot in Profile first')
    return telegram_request(token, method, payload)


def re_token(token):
    import re
    return bool(re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token))


def telegram_request(token, method, payload=None):
    result = json_request('https://api.telegram.org/bot' + token + '/' + method,
        headers={'Content-Type': 'application/json'}, data=json.dumps(payload or {}).encode())
    if not result.get('ok'):
        raise ValueError('Telegram rejected the request')
    return result['result']


def save_telegram(user, token, chat):
    import re
    current = load(user).get('telegram') or {}
    token = token.strip() or current.get('token', '')
    if not re_token(token) or not re.fullmatch(r'-?[0-9]+', chat):
        raise ValueError('Enter a valid bot token and numeric Telegram chat ID')
    bot = telegram_request(token, 'getMe')
    destination = telegram_request(token, 'getChat', {'chat_id': chat})
    if str(destination['id']) != str(int(chat)):
        raise ValueError('Telegram chat could not be verified')
    update(user, telegram={'token': token, 'chat': chat, 'name': bot.get('username', 'Telegram bot')})


def settings_html(user, username):
    settings = load(user)
    spotify = settings.get('spotify') or {}
    bot = settings.get('telegram') or {}
    esc = html.escape
    return f'''<section><h1>Profile · {esc(username)}</h1>
<h2>Spotify</h2><p>{esc(spotify.get('name', 'Not connected'))}</p>
<form method="post" action="/spotify/connect"><button>Connect Spotify</button></form>
<form method="post" action="/spotify/disconnect"><button class="danger">Disconnect Spotify</button></form>
<h2>Telegram</h2><p>{esc(bot.get('name', 'No bot connected'))}</p>
<p>Create a bot with @BotFather. Start your bot in Telegram before connecting your chat.</p>
<form method="post" action="/profile/telegram">
<p><label>Bot token <input type="password" name="token" autocomplete="new-password" placeholder="{'Leave blank to keep your token' if bot else 'BotFather token'}"></label></p>
<p><label>Chat ID <input name="chat" inputmode="numeric" value="{esc(bot.get('chat', ''), quote=True)}" required></label></p>
<button>Save bot</button></form>
<form method="post" action="/profile/telegram-test"><button>Send test message</button></form>
<form method="post" action="/profile/telegram-disconnect"><button class="danger">Disconnect bot</button></form>
</section>'''
