"""Retarget a fresh n8n export: python3 workflow_railway.py before.json after.json https://slskd.example.com."""
import copy
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


def patch(workflow, slskd_url):
    parsed = urlsplit(slskd_url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Supply the public HTTPS origin for Railway slskd')
    slskd_url = slskd_url.rstrip('/')
    workflow = copy.deepcopy(workflow)
    nodes = {node['name']: node for node in workflow['nodes']}
    changed = 0
    for node in workflow['nodes']:
        params = node.get('parameters', {})
        url = params.get('url', '')
        if isinstance(url, str):
            params['url'], count = re.subn(r'https?://[^/\s\'"{}]+(?=/api/v0(?:/|\?|$))', slskd_url, url)
            changed += count
            if not url:
                params.pop('url', None)
    if not changed:
        raise ValueError('No literal slskd API URLs found; inspect the export before changing it')
    if 'Download YouTube Audio' in nodes:
        reporter = nodes['Report Queue Status']
        callback = reporter['parameters']['url']
        if not callback.startswith('https://') or not callback.endswith('/workflow-status'):
            raise ValueError('Report Queue Status must use the public dashboard HTTPS URL')
        download = nodes['Download YouTube Audio']
        download.update(type='n8n-nodes-base.httpRequest', typeVersion=4.5,
                        credentials=copy.deepcopy(reporter['credentials']))
        download['parameters'] = {
            'method': 'POST', 'url': callback.removesuffix('/workflow-status') + '/youtube-download',
            'authentication': 'genericCredentialType', 'genericAuthType': 'httpHeaderAuth',
            'sendBody': True, 'specifyBody': 'json',
            'jsonBody': "={{ {profileId: $('Playlist desk webhook').first().json.body.profileId ?? 'owner', track: $json.pendingFallback} }}",
            'options': {'timeout': 300000}}
        record = nodes['Record YouTube Result']['parameters']
        previous = 'JSON.parse($json.stdout)'
        if previous not in record['jsCode']:
            raise ValueError('Unrecognized YouTube recorder or already patched')
        record['jsCode'] = record['jsCode'].replace(previous, '$json')
    return {key: workflow[key] for key in ('name', 'nodes', 'connections', 'settings')}


if __name__ == '__main__':
    Path(sys.argv[2]).write_text(json.dumps(patch(json.loads(Path(sys.argv[1]).read_text()), sys.argv[3]), indent=2))
