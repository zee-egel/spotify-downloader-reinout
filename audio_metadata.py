"""Spotify metadata and lossless, atomic audio tagging using FFmpeg."""
import base64
import logging
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import urllib.parse
import urllib.request


def spotify_metadata(items):
    result = {}
    for entry in (items or {}).get('items', []):
        track = entry.get('item') or entry.get('track') or {}
        if not track.get('id') or not track.get('name'):
            continue
        album = track.get('album') or {}
        images = album.get('images') or []
        result[track['id']] = {
            'title': track['name'], 'artist': ', '.join(a['name'] for a in track.get('artists', [])),
            'album': album.get('name'),
            'albumArtist': ', '.join(a['name'] for a in album.get('artists', [])),
            'releaseDate': album.get('release_date'), 'trackNumber': track.get('track_number'),
            'discNumber': track.get('disc_number'),
            'coverUrl': images[0]['url'] if images else None,
            'spotifyUrl': (track.get('external_urls') or {}).get('spotify'),
        }
    return result


def cover_bytes(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or parsed.netloc != 'i.scdn.co':
        raise ValueError('Invalid Spotify artwork URL')
    # Do not follow redirects from artwork URLs to arbitrary hosts.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    with urllib.request.build_opener(NoRedirect).open(url, timeout=10) as response:
        data = response.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise ValueError('Artwork is too large')
    if data.startswith(b'\xff\xd8\xff'):
        return data, 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return data, 'image/png'
    raise ValueError('Unsupported artwork image')


def tag_audio(path, track, codec=None):
    """Return (published path, tagging result). Failure leaves the original intact."""
    path = Path(path)
    if not track.get('title') or not (track.get('artist') or track.get('primaryArtist')):
        return path, {'status': 'skipped', 'reason': 'Spotify metadata is unavailable'}
    suffix = path.suffix.lower()
    # WebM has no portable embedded cover support. Remux Opus to Ogg without encoding.
    output_suffix = '.opus' if suffix == '.webm' and codec == 'opus' else suffix
    if output_suffix not in ('.mp3', '.flac', '.m4a', '.mp4', '.ogg', '.opus', '.wav', '.aiff', '.aif'):
        return path, {'status': 'skipped', 'reason': 'This audio container does not support tagging here'}
    try:
        with tempfile.TemporaryDirectory(dir=path.parent, prefix='.metadata-') as temporary:
            output = Path(temporary) / ('tagged' + output_suffix)
            args = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(path)]
            artwork, mime, warning = None, None, None
            if track.get('coverUrl'):
                try:
                    artwork, mime = cover_bytes(track['coverUrl'])
                except (OSError, ValueError) as error:
                    logging.warning('Spotify cover download failed: %s', type(error).__name__)
                    warning = 'Cover download failed; text tags were saved'
            if artwork and output_suffix == '.wav':
                artwork = None
                warning = 'Text tags saved; this container cannot embed cover art here'
            if artwork and output_suffix in ('.ogg', '.opus'):
                mime_bytes = mime.encode()
                picture = (struct.pack('>II', 3, len(mime_bytes)) + mime_bytes +
                           struct.pack('>IIIIII', 0, 0, 0, 0, 0, len(artwork)) + artwork)
                # Artwork can exceed the OS argument-size limit; pass it as metadata input.
                cover = Path(temporary) / 'cover.ffmetadata'
                cover.write_text(';FFMETADATA1\nmetadata_block_picture=' + base64.b64encode(picture).decode() + '\n')
                args += ['-f', 'ffmetadata', '-i', str(cover)]
            if artwork and output_suffix not in ('.ogg', '.opus'):
                cover = Path(temporary) / ('cover.png' if mime == 'image/png' else 'cover.jpg')
                cover.write_bytes(artwork)
                args += ['-i', str(cover), '-map', '0:a:0', '-map', '1:v:0', '-disposition:v:0', 'attached_pic']
                args += ['-metadata:s:v:0', 'title=Album cover', '-metadata:s:v:0', 'comment=Cover (front)']
            else:
                args += ['-map', '0:a:0']
                if not artwork and output_suffix in ('.mp3', '.flac', '.m4a', '.mp4', '.aiff', '.aif'):
                    args += ['-map', '0:v?', '-disposition:v:0', 'attached_pic']
            if artwork and output_suffix in ('.ogg', '.opus'):
                args += ['-map_metadata', '1']
            args += ['-map_metadata', '0', '-c', 'copy']
            tags = {'title': track.get('title'), 'artist': track.get('artist') or track.get('primaryArtist'),
                    'album': track.get('album'), 'album_artist': track.get('albumArtist'),
                    'date': track.get('releaseDate'), 'track': track.get('trackNumber'),
                    'disc': track.get('discNumber'), 'comment': track.get('spotifyUrl')}
            for key, value in tags.items():
                if value is not None and value != '':
                    args += ['-metadata', f'{key}={value}']
            if output_suffix == '.mp3':
                args += ['-id3v2_version', '3']
            if output_suffix in ('.aiff', '.aif'):
                args += ['-write_id3v2', '1', '-id3v2_version', '3']
            subprocess.run(args + [str(output)], check=True, capture_output=True, timeout=45)
            if not output.stat().st_size:
                raise ValueError('Empty tagged audio')
            destination = path.with_suffix(output_suffix)
            if destination != path and destination.exists():
                raise ValueError('Tagged destination already exists')
            shutil.copystat(path, output)
            with output.open('rb') as saved:
                os.fsync(saved.fileno())
            output.replace(destination)
            if destination != path:
                path.unlink()
            return destination, {'status': 'partial' if warning else 'tagged', 'reason': warning or ''}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        logging.warning('Audio tagging failed: %s', type(error).__name__)
        return path, {'status': 'failed', 'reason': 'Could not embed Spotify metadata; original audio retained'}
