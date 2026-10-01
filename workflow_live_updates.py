"""Add early UI callbacks to an exported playlist workflow; never contains API keys.

Usage: python3 workflow_live_updates.py before.json after.json
The output is a PUT /api/v1/workflows/{id} body. Applying it does not start a run.
"""
import copy
import json
import sys
from pathlib import Path


def patch(workflow):
    workflow = copy.deepcopy(workflow)
    nodes = {node['name']: node for node in workflow['nodes']}
    connections = workflow['connections']

    def edge(name):
        return {'node': name, 'type': 'main', 'index': 0}

    def add(name, kind, parameters, position, **options):
        if name in nodes:
            raise ValueError('Workflow already contains ' + name)
        node = {'id': 'playlist-live-' + name.lower().replace(' ', '-'), 'name': name,
                'type': 'n8n-nodes-base.' + kind, 'typeVersion': 2 if kind == 'code' else 4.5,
                'parameters': parameters, 'position': position, **options}
        workflow['nodes'].append(node)
        nodes[name] = node

    def report(name, state, position, **options):
        params = copy.deepcopy(nodes['Report Queue Status']['parameters'])
        params['jsonBody'] = "={{ { playlistId: $('Code in JavaScript1').first().json.playlistId, submissionId: $('Playlist desk webhook').first().json.body.submissionId, phase: 'queued', state: " + state + " } }}"
        params['options'] = {**params.get('options', {}), 'timeout': 5000}
        add(name, 'httpRequest', params, position, onError='continueRegularOutput', **options)

    def insert(source, outlet, reporter, restore, state, code, position, **options):
        targets = connections[source]['main'][outlet]
        report(reporter, state, position, **options)
        add(restore, 'code', {'jsCode': code}, [position[0] + 160, position[1]])
        connections[source]['main'][outlet] = [edge(reporter)]
        connections[reporter] = {'main': [[edge(restore)]]}
        connections[restore] = {'main': [targets]}

    insert('Normalize Tracks', 0, 'Report Playlist Tracks', 'Restore Playlist Tracks',
           "{ playlistName: $('Get Playlist').first().json.name, resolving: true, totalSpotifyTracks: $('Normalize Tracks').all().length, tracks: $('Normalize Tracks').all().map(item => ({ spotifyId: item.json.spotifyId, title: item.json.title, artist: item.json.artist, status: 'pending' })) }",
           "return $('Normalize Tracks').all();", [1104, 1600], executeOnce=True)
    insert('Loop Over Items', 1, 'Report Searching Track', 'Restore Searching Track',
           "{ resolving: true, track: { ...$json, status: 'searching' } }",
           "return [{ json: $('Loop Over Items').itemMatching(0).json, pairedItem: { item: 0 } }];", [1328, 1160])
    insert('Auto Download', 0, 'Report Selected Track', 'Restore Selected Track',
           "{ resolving: true, track: { ...$json, status: 'matching' } }",
           "return [{ json: $('Code in JavaScript').itemMatching(0).json, pairedItem: { item: 0 } }];", [3344, 440])

    # All result paths report before the next search, while preserving the loop's item linking.
    add('Track Result', 'code', {'jsCode': "return $input.all();"}, [3900, 1280])
    report('Report Track Result', "{ resolving: true, track: $json }", [4060, 1280])
    add('Restore Track Result', 'code', {'jsCode': "return [{ json: $('Track Result').itemMatching(0).json, pairedItem: { item: 0 } }];"}, [4220, 1280])
    for source, outlet in (('Queue Status', 0), ('Search Error', 0), ('Search Timeout', 0), ('Auto Download', 1)):
        assert connections[source]['main'][outlet] == [edge('Loop Over Items')], source
        connections[source]['main'][outlet] = [edge('Track Result')]
    connections['Track Result'] = {'main': [[edge('Report Track Result')]]}
    connections['Report Track Result'] = {'main': [[edge('Restore Track Result')]]}
    connections['Restore Track Result'] = {'main': [[edge('Loop Over Items')]]}

    summary = nodes['Summary']['parameters']['jsCode']
    assert 'batchId: item.batchId, artist:' in summary
    summary = summary.replace('batchId: item.batchId, artist:', 'spotifyId: item.spotifyId, batchId: item.batchId, artist:')
    summary = summary.replace('({ artist: item.artist, title:', '({ spotifyId: item.spotifyId, artist: item.artist, title:')
    nodes['Summary']['parameters']['jsCode'] = summary
    return {key: workflow[key] for key in ('name', 'nodes', 'connections', 'settings')}


if __name__ == '__main__':
    result = patch(json.loads(Path(sys.argv[1]).read_text()))
    Path(sys.argv[2]).write_text(json.dumps(result, indent=2))
