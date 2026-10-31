"""Basic-auth accounts and their private integrations; SQLite is in the state volume."""
import base64
import hashlib
import html
from views import render_html
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
    result = {owner: ('owner', os.environ.get('APP_PASSWORD', ''))}
    for suffix, profile in (('', 'extra'), ('_2', 'extra2')):
        username = os.environ.get('EXTRA_APP_USER' + suffix, '')
        password = os.environ.get('EXTRA_APP_PASSWORD' + suffix, '')
        if username and username in result:
            raise ValueError('Accountnamen moeten verschillen')
        if username and password:
            result[username] = (profile, password)
    return result


def json_request(url, *, headers=None, data=None):
    req = urllib.request.Request(url, data, headers or {})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def redirect_uri():
    base = os.environ.get('APP_PUBLIC_URL', '').rstrip('/')
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('Stel APP_PUBLIC_URL in op het HTTPS-adres van deze app')
    return base + '/spotify/callback'


def spotify_start(user):
    client = os.environ.get('SPOTIFY_CLIENT_ID', '')
    if not client:
        raise ValueError('De Spotify-koppeling is nog niet ingesteld')
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
            raise ValueError('De Spotify-aanmelding is verlopen of hoort niet bij dit account. Probeer opnieuw te koppelen.')
        update(user, oauth=None)
    if query.get('error') or not query.get('code'):
        raise ValueError('De Spotify-koppeling is geannuleerd')
    tokens = spotify_tokens({'grant_type': 'authorization_code', 'code': query['code'][0],
                             'redirect_uri': redirect_uri(), 'code_verifier': pending['verifier']})
    me = json_request('https://api.spotify.com/v1/me', headers={'Authorization': 'Bearer ' + tokens['access_token']})
    tokens.update(expires=time.time() + tokens['expires_in'], name=me.get('display_name') or me['id'])
    update(user, spotify=tokens)


def spotify_get(user, path):
    if not path.startswith('/v1/'):
        raise ValueError('Ongeldig Spotify-pad')
    with LOCK:
        tokens = load(user).get('spotify')
        if not tokens:
            raise ValueError('Koppel je Spotify-account bij Profiel voordat je importeert')
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
            raise ValueError('Spotify heeft een onvolledige playlist teruggegeven')
        offset += len(page['items'])
    return {'id': ident, 'name': playlist.get('name', 'Spotify-playlist')}, {'items': items, 'next': None}


def spotify_selection(user, kind, ident):
    if kind == 'playlist':
        return spotify_playlist(user, ident)
    if kind == 'track':
        track = spotify_get(user, '/v1/tracks/' + ident)
        name = track.get('name') or 'Spotify-track'
        return {'id': ident, 'name': name}, {'items': [{'item': track}], 'next': None}
    album = spotify_get(user, '/v1/albums/' + ident)
    album_info = {key: album.get(key) for key in ('name', 'artists', 'release_date', 'images')}
    items = []
    page = album.get('tracks') or {}
    while True:
        items.extend({'item': {**track, 'album': album_info}} for track in page.get('items', []))
        if not page.get('next'):
            break
        page = spotify_get(user, f"/v1/albums/{ident}/tracks?limit=50&offset={len(items)}")
    return {'id': ident, 'name': album.get('name', 'Spotify-album')}, {'items': items, 'next': None}


def telegram(user, method, payload=None):
    settings = load(user).get('telegram') or {}
    token = settings.get('token', '')
    if not re_token(token):
        raise ValueError('Voeg eerst je Telegram-bot toe bij Profiel')
    return telegram_request(token, method, payload)


def re_token(token):
    import re
    return bool(re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token))


def telegram_request(token, method, payload=None):
    result = json_request('https://api.telegram.org/bot' + token + '/' + method,
        headers={'Content-Type': 'application/json'}, data=json.dumps(payload or {}).encode())
    if not result.get('ok'):
        raise ValueError('Telegram heeft het verzoek geweigerd')
    return result['result']


def save_telegram(user, token, chat):
    import re
    current = load(user).get('telegram') or {}
    token = token.strip() or current.get('token', '')
    if not re_token(token) or not re.fullmatch(r'-?[0-9]+', chat):
        raise ValueError('Vul een geldig bottoken en een numeriek Telegram-chat-ID in')
    bot = telegram_request(token, 'getMe')
    destination = telegram_request(token, 'getChat', {'chat_id': chat})
    if str(destination['id']) != str(int(chat)):
        raise ValueError('De Telegram-chat kon niet worden geverifieerd')
    update(user, telegram={'token': token, 'chat': chat, 'name': bot.get('username', 'Telegram-bot')})


def settings_html(user, username):
    settings = load(user)
    spotify = settings.get('spotify') or {}
    bot = settings.get('telegram') or {}
    esc = html.escape
    profile_script = render_html('script', source='/profile.js')
    return render_html(
        'profile',
        avatar_initial=esc(username[:1].upper()),
        username=esc(username),
        spotify_kind='success' if spotify else '',
        spotify_status='Verbonden' if spotify else 'Niet verbonden',
        spotify_name=esc(spotify.get('name', '')),
        spotify_button='Spotify opnieuw koppelen' if spotify else 'Spotify koppelen',
        spotify_disconnect=render_html('spotify-disconnect') if spotify else '',
        telegram_kind='success' if bot else '',
        telegram_status='Verbonden' if bot else 'Niet verbonden',
        telegram_name=esc(bot.get('name', '')),
        token_placeholder='Laat leeg om je token te behouden…' if bot else 'Plak je BotFather-token…',
        token_hint='Je opgeslagen token blijft privé. Laat dit veld leeg om het te behouden.' if bot else 'Je token wordt privé opgeslagen en niet weergegeven.',
        chat_id=esc(bot.get('chat', ''), quote=True),
        telegram_actions=render_html('telegram-actions') if bot else '',
        profile_script=profile_script,
    )
