"""Check the callback graph without n8n credentials: python3 test_workflow.py."""
import json
import subprocess
from workflow_live_updates import patch

names = ['Normalize Tracks', 'Loop Over Items', 'Auto Download', 'Queue Status', 'Search Error', 'Search Timeout', 'Report Queue Status', 'Summary']
original = {'name': 'Test', 'settings': {}, 'nodes': [{'name': name, 'parameters': {}} for name in names],
            'connections': {name: {'main': [[{'node': 'Loop Over Items', 'type': 'main', 'index': 0}]]} for name in names}}
original['connections']['Loop Over Items']['main'].append([{'node': 'Search slskd request', 'type': 'main', 'index': 0}])
original['connections']['Auto Download']['main'].append([{'node': 'Loop Over Items', 'type': 'main', 'index': 0}])
original['nodes'][-1]['parameters']['jsCode'] = '({ batchId: item.batchId, artist: item.artist }); ({ artist: item.artist, title: item.title });'
before = json.dumps(original)
updated = patch(original)
assert json.dumps(original) == before
nodes = {node['name']: node for node in updated['nodes']}
assert len(nodes) == len(names) + 9
assert nodes['Report Playlist Tracks']['executeOnce']
for source in ('Queue Status', 'Search Error', 'Search Timeout'):
    assert updated['connections'][source]['main'][0][0]['node'] == 'Track Result'
assert updated['connections']['Auto Download']['main'][0][0]['node'] == 'Report Selected Track'
assert updated['connections']['Auto Download']['main'][1][0]['node'] == 'Track Result'
assert updated['connections']['Restore Track Result']['main'][0][0]['node'] == 'Loop Over Items'
assert 'spotifyId: item.spotifyId' in nodes['Summary']['parameters']['jsCode']
# Execute the actual callback expressions, not copies of their payloads.
subprocess.run(['node', '-e', '''
const assert = require('node:assert/strict');
const nodes = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const track = {spotifyId:'id', title:'Song', artist:'Artist', username:'peer', filename:'music/song.mp3'};
const $json = track;
const $ = name => ({first:()=>({json:name==='Playlist desk webhook'?{body:{submissionId:'run'}}:name==='Get Playlist'?{name:'Playlist'}:{playlistId:'playlist'}}), all:()=>[{json:track}], itemMatching:()=>({json:track})});
function payload(name) { return eval('(' + nodes[name].parameters.jsonBody.slice(3,-2) + ')'); }
const metadata = payload('Report Playlist Tracks');
assert.equal(metadata.state.totalSpotifyTracks,1);
assert.equal(metadata.state.tracks[0].status,'pending');
assert.equal(payload('Report Searching Track').state.track.status,'searching');
assert.equal(payload('Report Selected Track').state.track.filename,track.filename);
assert.equal(payload('Report Selected Track').state.track.status,'matching');
assert.equal(payload('Report Track Result').state.track,$json);
'''], input=json.dumps(nodes), text=True, check=True)
print('Workflow callback checks passed')
