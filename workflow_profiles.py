"""Route a live workflow through app profiles. Usage: python3 workflow_profiles.py before.json after.json."""
import copy
import json
import sys
from pathlib import Path


def patch(workflow):
    workflow = copy.deepcopy(workflow)
    nodes = {node['name']: node for node in workflow['nodes']}
    credential = nodes['Playlist desk webhook']['credentials']['httpHeaderAuth']
    callback = nodes['Report Queue Status']['parameters']['url']
    assert callback.endswith('/workflow-status')
    base = callback.removesuffix('/workflow-status')
    for name, value in (('Get Playlist', 'playlist'), ('HTTP Request', 'playlistItems')):
        node = nodes[name]
        node.update(type='n8n-nodes-base.code', typeVersion=2)
        node.pop('credentials', None)
        node['parameters'] = {'jsCode': "const body = $('Playlist desk webhook').first().json.body;\n"
            "if (!['owner', 'extra'].includes(body.profileId)) throw new Error('Unknown profile');\n"
            "const expected = body.profileId === 'extra' ? 'profiles/extra' : '';\n"
            "if (body.downloadDirectory !== expected) throw new Error('Invalid profile destination');\n"
            f"if (!body.{value}) throw new Error('Connect Spotify in Playlist desk before importing');\n"
            f"return [{{ json: body.{value} }}];"}
    for name in ('Queue Download', 'Queue Fallback'):
        params = nodes[name]['parameters']
        previous = 'destination: $json.playlistName'
        if previous not in params['jsonBody']:
            raise ValueError('Unrecognized destination or already patched: ' + name)
        params['jsonBody'] = params['jsonBody'].replace(previous,
            "destination: [$('Playlist desk webhook').first().json.body.downloadDirectory, $json.playlistName].filter(Boolean).join('/')")
    for name in ('Send a text message', 'Send Queue Summary', 'Send Remote Pending'):
        node = nodes[name]
        assert node['parameters']['text'] == '={{ $json.message }}'
        node.update(type='n8n-nodes-base.httpRequest', typeVersion=4.5,
                    credentials={'httpHeaderAuth': copy.deepcopy(credential)}, onError='continueRegularOutput')
        node['parameters'] = {'method': 'POST', 'url': base + '/workflow-notify',
            'authentication': 'genericCredentialType', 'genericAuthType': 'httpHeaderAuth',
            'sendBody': True, 'specifyBody': 'json',
            'jsonBody': "={{ { submissionId: $('Playlist desk webhook').first().json.body.submissionId, message: $json.message } }}",
            'options': {'timeout': 15000}}
    return {key: workflow[key] for key in ('name', 'nodes', 'connections', 'settings')}


if __name__ == '__main__':
    Path(sys.argv[2]).write_text(json.dumps(patch(json.loads(Path(sys.argv[1]).read_text())), indent=2))
