"""One-shot YouTube downloader invoked by n8n; stdout is one JSON result."""
from collections import Counter, deque
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import tempfile
import time
import unicodedata


def normalized(value):
    value = ''.join(c for c in unicodedata.normalize('NFKD', value) if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[^\W_]+', value.casefold()))


def tokens(value):
    return set(normalized(value).split())


def duration_matches(expected, actual, trusted=False):
    tolerance = max(2, min(8 if trusted else 5, expected * (.02 if trusted else .01)))
    return not isinstance(actual, bool) and isinstance(actual, (int, float)) and math.isfinite(actual) and abs(expected - actual) <= tolerance


def is_topic(track, video):
    return normalized(str(video.get('channel') or '')) == normalized(track['primaryArtist']) + ' topic'


def recording_title(value):
    # Ignore metadata adornments, never mix names or performance/version labels.
    value = re.sub(r"\s*[([](?:feat\.?|ft\.?|featuring)\s+[^)\]]+[)\]]", "", value, flags=re.I)
    value = re.sub(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", "", value, flags=re.I)
    value = re.sub(r"\s*(?:[-–—]\s*|[([])(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?[)\]]?\s*$", "", value, flags=re.I)
    return normalized(value)


def mismatch(track, video, check_duration=True):
    title = recording_title(str(video.get('title') or ''))
    wanted = recording_title(track['title'])
    artist = normalized(track['primaryArtist'])
    sources = [normalized(str(video.get(key) or '')) for key in ('title', 'channel', 'uploader')]
    if not any(' ' + artist + ' ' in ' ' + source + ' ' for source in sources):
        return 'artist_mismatch'
    if not wanted or ' ' + wanted + ' ' not in ' ' + title + ' ':
        return 'title_mismatch'
    versions = {'live', 'remix', 'cover', 'karaoke', 'instrumental', 'sped', 'slowed',
                'extended', 'edit', 'acoustic', 'demo', 'nightcore', 'mashup'}
    # Artist names such as Live must not be mistaken for a performance label.
    version_title = (' ' + title + ' ').replace(' ' + artist + ' ', ' ')
    if (set(version_title.split()) - set(wanted.split())) & versions:
        return 'version_mismatch'
    if video.get('is_live') or video.get('live_status') in ('is_live', 'is_upcoming', 'post_live'):
        return 'live_stream'
    if check_duration and not duration_matches(track['durationSeconds'], video.get('duration'), trusted=is_topic(track, video)):
        return 'duration_mismatch'
    return None


def matches(track, video):
    return mismatch(track, video) is None


def failure_code(error):
    if isinstance(error, subprocess.TimeoutExpired):
        return 'timeout'
    if isinstance(error, FileNotFoundError):
        return 'missing_runtime'
    message = str(getattr(error, 'stderr', '') or '').casefold()
    for needles, code in ((('sign in', 'not a bot', 'cookies'), 'youtube_challenge'),
                          (('429', 'too many requests'), 'rate_limited'),
                          (('403', 'forbidden'), 'access_denied'),
                          (('no module named', 'javascript runtime', 'ejs'), 'missing_runtime'),
                          (('private video', 'not available', 'removed'), 'video_unavailable')):
        if any(word in message for word in needles):
            return code
    return 'extraction_failed'


FAILURE_REASONS = {
    'timeout': 'YouTube recovery reached its time limit; try again later.',
    'youtube_challenge': 'YouTube requires a sign-in or bot check on the download host.',
    'rate_limited': 'YouTube rate-limited the download host; try again later.',
    'access_denied': 'YouTube denied audio access; check the host’s yt-dlp version and JavaScript runtime.',
    'missing_runtime': 'The download host needs yt-dlp, yt-dlp-ejs, Node and ffprobe.',
    'video_unavailable': 'Matching YouTube videos are unavailable on the download host.',
    'extraction_failed': 'Matching YouTube candidates could not be extracted.',
    'audio_validation_failed': 'Downloaded candidates did not contain complete, readable audio.',
}


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


def download(job, request, progress=None):
    track = request['track']
    validate(track)
    deadline = time.monotonic() + 240

    diagnostics = []
    failures = []

    def remaining(limit=90):
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise subprocess.TimeoutExpired('YouTube recovery', 240)
        return min(limit, seconds)

    def run(args, limit=90):
        return subprocess.run(args, capture_output=True, text=True, check=True, timeout=remaining(limit))

    def note(stage, code, ident=None):
        if len(diagnostics) < 60:
            diagnostics.append({'stage': stage, 'code': code, **({'videoId': ident} if ident else {})})

    base = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
            '--js-runtimes', 'node', '--socket-timeout', '15', '--retries', '1', '--fragment-retries', '1']
    if proxy := os.environ.get('YOUTUBE_PROXY'):
        base += ['--proxy', proxy]
    queries = list(dict.fromkeys([
        track['primaryArtist'] + ' ' + track['title'] + ' audio',
        track['primaryArtist'] + ' ' + recording_title(track['title']) + ' audio',
        track['primaryArtist'] + ' ' + recording_title(track['title']) + ' topic',
    ]))
    seen = set()
    attempts = 0
    for query in queries:
        if progress:
            progress(status='youtube searching', percent=None)
        try:
            search = json.loads(run(base + ['--flat-playlist', '-J', 'ytsearch15:' + query], 30).stdout)
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            code = failure_code(error)
            failures.append(code)
            note('search', code)
            continue
        candidates = []
        for video in search.get('entries', []) or []:
            if not isinstance(video, dict):
                continue
            ident = video.get('id')
            if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', ident) or ident in seen:
                continue
            seen.add(ident)
            # Flat search can omit duration. Require full metadata before downloading.
            reason = mismatch(track, video, check_duration=video.get('duration') is not None)
            if reason:
                note('search', reason, ident)
            else:
                candidates.append(video)
        candidates.sort(key=lambda v: (not str(v.get('channel') or '').endswith(' - Topic'),
                                       abs((v.get('duration') or track['durationSeconds']) - track['durationSeconds'])))
        for candidate in candidates:
            if attempts >= 6 or time.monotonic() >= deadline:
                break
            attempts += 1
            ident = candidate['id']
            url = 'https://www.youtube.com/watch?v=' + ident
            stage = 'metadata'
            try:
                if progress:
                    progress(status='youtube checking', percent=None)
                info = json.loads(run(base + ['--skip-download', '-J', url], 30).stdout)
                reason = mismatch(track, info)
                if reason:
                    note(stage, reason, ident)
                    continue
                with tempfile.TemporaryDirectory(dir=job) as temporary:
                    output = str(Path(temporary) / 'audio.%(ext)s')
                    args = base + ['-f', 'bestaudio', '--no-simulate', '-J', '-o', output, url]
                    stage = 'download'
                    if progress:
                        progress(status='youtube downloading', percent=None)
                        args += ['--progress', '--newline', '--progress-delta', '1',
                                 '--progress-template', 'download:YT_PROGRESS:%(progress._percent_str)s']
                        with tempfile.TemporaryFile(mode='w+') as captured:
                            errors = deque(maxlen=10)
                            process = subprocess.Popen(args, stdout=captured, stderr=subprocess.PIPE, text=True, start_new_session=True)
                            def updates():
                                for line in process.stderr:
                                    match = re.search(r'YT_PROGRESS:\s*([\d.]+)%', line)
                                    if match:
                                        progress(status='youtube downloading', percent=min(100, float(match[1])))
                                    else:
                                        errors.append(line[:2000])
                            reader = threading.Thread(target=updates, daemon=True)
                            reader.start()
                            try:
                                process.wait(timeout=remaining())
                            finally:
                                if process.poll() is None:
                                    os.killpg(process.pid, signal.SIGKILL)
                                process.wait()
                                reader.join()
                                process.stderr.close()
                            if process.returncode:
                                raise subprocess.CalledProcessError(process.returncode, args, stderr=''.join(errors))
                            captured.seek(0)
                            downloaded = json.load(captured)
                        progress(status='youtube validating', percent=None)
                    else:
                        downloaded = json.loads(run(args).stdout)
                    stage = 'validation'
                    files = [p for p in Path(temporary).iterdir() if p.suffix in ('.webm', '.m4a', '.opus', '.ogg', '.mp3', '.aac', '.flac', '.wav')]
                    if len(files) != 1 or not files[0].stat().st_size:
                        raise ValueError('No complete audio file')
                    audio = files[0]
                    measured = json.loads(run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(audio)], 15).stdout)
                    duration = float(measured['format']['duration'])
                    if not duration_matches(track['durationSeconds'], duration, trusted=is_topic(track, info)):
                        note(stage, 'duration_mismatch', ident)
                        continue
                    # Publish only after validation, while the candidate directory still exists.
                    stage = 'save'
                    result = save_audio(request, track, audio, downloaded, url, duration)
                    result['diagnostics'] = diagnostics
                    return result
            except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                if stage == 'save':
                    note(stage, 'storage_failed', ident)
                    return {'status': 'failed', 'code': 'storage_failed', 'reason': 'Validated audio could not be saved; check download storage and permissions.', 'diagnostics': diagnostics}
                code = failure_code(error)
                if stage == 'validation' and code == 'extraction_failed':
                    code = 'audio_validation_failed'
                failures.append(code)
                note(stage, code, ident)
                continue
        if attempts >= 6 or time.monotonic() >= deadline:
            break
    if time.monotonic() >= deadline:
        failures.append('timeout')
    if failures:
        code = Counter(failures).most_common(1)[0][0]
        return {'status': 'failed', 'reason': FAILURE_REASONS[code], 'code': code, 'diagnostics': diagnostics}
    return {'status': 'no_match', 'reason': 'No YouTube result matched artist, recording version and duration.',
            'code': 'no_matching_recording', 'diagnostics': diagnostics}


def save_audio(request, track, audio, downloaded, url, duration):
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


def main(encoded, progress=None):
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
            return download(Path(temporary), {'root': str(root), 'track': payload['track']}, progress)
    except subprocess.TimeoutExpired:
        return {'status': 'failed', 'reason': FAILURE_REASONS['timeout'], 'code': 'timeout'}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.CalledProcessError) as error:
        code = failure_code(error)
        return {'status': 'failed', 'reason': FAILURE_REASONS[code], 'code': code}


if __name__ == '__main__':
    print(json.dumps(main(sys.argv[1] if len(sys.argv) == 2 else '')))
