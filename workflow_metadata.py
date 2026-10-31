"""Retain Spotify tags in an existing n8n export; writes a local API update body only.

Usage: python3 workflow_metadata.py before.json after.json
"""
import copy
import json
from pathlib import Path
import sys


def patch(workflow):
    workflow = copy.deepcopy(workflow)
    nodes = {node['name']: node for node in workflow['nodes']}
    code = nodes['Normalize Tracks']['parameters']['jsCode']
    marker = "album: track.album?.name ?? null,"
    if 'coverUrl:' not in code:
        if marker not in code:
            raise ValueError('Unrecognized Normalize Tracks code')
        code = code.replace(marker, marker + "\n        albumArtist: (track.album?.artists ?? []).map(a => a.name).filter(Boolean).join(', '),\n        releaseDate: track.album?.release_date ?? null,\n        trackNumber: track.track_number ?? null,\n        discNumber: track.disc_number ?? null,\n        coverUrl: track.album?.images?.[0]?.url ?? null,")
    nodes['Normalize Tracks']['parameters']['jsCode'] = code
    if 'Report Playlist Tracks' not in nodes:
        raise ValueError('Install workflow_live_updates.py first so the app receives track metadata')
    params = nodes['Report Playlist Tracks']['parameters']
    marker = 'spotifyId: item.json.spotifyId, title: item.json.title, artist: item.json.artist,'
    if marker in params['jsonBody']:
        params['jsonBody'] = params['jsonBody'].replace(marker, '...item.json,')
    elif '...item.json' not in params['jsonBody']:
        raise ValueError('Unrecognized playlist metadata callback')
    return {key: workflow[key] for key in ('name', 'nodes', 'connections', 'settings')}


if __name__ == '__main__':
    Path(sys.argv[2]).write_text(json.dumps(patch(json.loads(Path(sys.argv[1]).read_text())), indent=2))
