"""One-shot YouTube downloader invoked by n8n; stdout is one JSON result."""
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unicodedata


def normalized(value):
    value = ''.join(c for c in unicodedata.normalize('NFKD', value) if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[^\W_]+', value.casefold()))


def tokens(value):
    return set(normalized(value).split())


def duration_matches(expected, actual):
    return isinstance(actual, (int, float)) and math.isfinite(actual) and abs(expected - actual) <= max(2, min(5, expected * .01))


def matches(track, video):
    title = tokens(str(video.get('title') or ''))
    artist = tokens(track['primaryArtist'])
    wanted = tokens(track['title'])
    sources = [normalized(str(video.get(key) or '')) for key in ('title', 'channel', 'uploader')]
    versions = {'live', 'remix', 'cover', 'karaoke', 'instrumental', 'sped', 'slowed', 'extended', 'edit', 'remaster', 'remastered'}
    artist_match = any(' ' + normalized(track['primaryArtist']) + ' ' in ' ' + s + ' ' for s in sources)
    title_match = ' ' + normalized(track['title']) + ' ' in ' ' + sources[0] + ' '
    return bool(wanted and artist and title_match and artist_match
                and not ((title - wanted) & versions)
                and video.get('live_status') not in ('is_live', 'is_upcoming', 'post_live')
                and not video.get('is_live')
                and duration_matches(track['durationSeconds'], video.get('duration')))


def validate(track):
    if not isinstance(track, dict):
        raise ValueError('Invalid track')
    for key in ('spotifyId', 'title', 'primaryArtist', 'playlistName'):
        if not isinstance(track.get(key), str) or not 0 < len(track[key].strip()) <= 300:
            raise ValueError('Missing track metadata: ' + key)
    if not re.fullmatch(r'[A-Za-z0-9]{22}', track['spotifyId']):
        raise ValueError('Invalid Spotify ID')
    duration = track.get('durationSeconds')
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= 14400:
        raise ValueError('Invalid duration')
    if not tokens(track['title']) or not tokens(track['primaryArtist']):
        raise ValueError('Track needs manual matching')


def download(job, request):
    track = request['track']
    validate(track)
    deadline = time.monotonic() + 240

    def run(args):
        return subprocess.run(args, capture_output=True, text=True, check=True,
                              timeout=max(.1, min(90, deadline - time.monotonic())))

    base = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
            '--js-runtimes', 'node', '--socket-timeout', '15', '--retries', '1', '--fragment-retries', '1']
    query = track['primaryArtist'] + ' ' + track['title'] + ' audio'
    search = json.loads(run(base + ['--flat-playlist', '-J', 'ytsearch10:' + query]).stdout)
    candidates = [v for v in search.get('entries', []) if v and matches(track, v)]
    candidates.sort(key=lambda v: (not str(v.get('channel') or '').endswith(' - Topic'),
                                    abs(v['duration'] - track['durationSeconds'])))
    for candidate in candidates[:3]:
        ident = candidate.get('id', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', ident):
            continue
        url = 'https://www.youtube.com/watch?v=' + ident
        info = json.loads(run(base + ['--skip-download', '-J', url]).stdout)
        if not matches(track, info):
            continue
        with tempfile.TemporaryDirectory(dir=job) as temporary:
            output = str(Path(temporary) / 'audio.%(ext)s')
            downloaded = json.loads(run(base + ['-f', 'bestaudio', '--no-simulate', '-J', '-o', output, url]).stdout)
            files = [p for p in Path(temporary).iterdir() if p.suffix in ('.webm', '.m4a', '.opus', '.ogg')]
            if len(files) != 1:
                raise ValueError('No complete audio file')
            audio = files[0]
            measured = json.loads(run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(audio)]).stdout)
            duration = float(measured['format']['duration'])
            if not duration_matches(track['durationSeconds'], duration):
                continue
            root = Path(request['root']).resolve()
            folder_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', track['playlistName']).replace('..', '_').strip('. ') or 'Spotify Playlist'
            folder_name = folder_name.encode()[:200].decode('utf-8', 'ignore')
            folder = root / folder_name
            if folder_name == 'profiles' or not folder.resolve().is_relative_to(root):
                raise ValueError('Invalid destination')
            folder.mkdir(parents=True, exist_ok=True)
            name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', track['primaryArtist'] + ' - ' + track['title']).encode()[:140].decode('utf-8', 'ignore')
            destination = folder / (name + ' [' + track['spotifyId'] + ']' + audio.suffix)
            # Copy then rename on the downloads filesystem: partial audio never appears in Library.
            import shutil
            with tempfile.NamedTemporaryFile(dir=folder, prefix='.youtube-', delete=False) as staging:
                staging_path = Path(staging.name)
                try:
                    with audio.open('rb') as source:
                        shutil.copyfileobj(source, staging)
                    staging.flush()
                    os.fsync(staging.fileno())
                    staging_path.replace(destination)
                finally:
                    staging_path.unlink(missing_ok=True)
            return {'status': 'completed', 'source': 'YouTube', 'url': url,
                    'filename': str(destination.relative_to(root)), 'durationSeconds': duration,
                    'codec': downloaded.get('acodec'), 'bitrate': downloaded.get('abr')}
    return {'status': 'no_match', 'reason': 'No YouTube result matched artist, title, version and duration'}


def main(encoded):
    from urllib.parse import unquote
    try:
        if len(encoded) > 65536:
            raise ValueError('Request too large')
        payload = json.loads(unquote(encoded))
        profile = payload.get('profileId', 'owner')
        if profile not in ('owner', 'extra'):
            raise ValueError('Invalid profile')
        root = Path(os.environ.get('DOWNLOADS_ROOT', '/downloads')).resolve()
        if profile == 'extra':
            target = root / 'profiles' / 'extra'
            if (root / 'profiles').is_symlink() or target.is_symlink():
                raise ValueError('Invalid profile destination')
            root = target
        with tempfile.TemporaryDirectory() as temporary:
            return download(Path(temporary), {'root': str(root), 'track': payload['track']})
    except subprocess.TimeoutExpired:
        return {'status': 'failed', 'reason': 'YouTube search/download timed out'}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.CalledProcessError):
        return {'status': 'failed', 'reason': 'YouTube extraction or audio validation failed'}


if __name__ == '__main__':
    print(json.dumps(main(sys.argv[1] if len(sys.argv) == 2 else '')))
