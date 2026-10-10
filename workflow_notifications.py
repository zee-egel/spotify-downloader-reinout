"""Keep one concise Telegram completion message: python3 workflow_notifications.py before.json after.json."""
import copy
import json
from pathlib import Path
import sys


FINAL = r"""
const clean = value => String(value ?? '').replace(/[\r\n\t]+/g, ' ').trim().slice(0, 120);
const s = $json;
const tracks = new Map();
const completed = t => t.youtubeStatus === 'completed' || t.downloadOutcome === 'completed' || t.status === 'completed';
for (const t of [...(s.downloads ?? []), ...(s.problems ?? [])]) {
  const key = t.spotifyId || `${t.artist}:${t.title}`;
  if (!tracks.has(key) || completed(t)) tracks.set(key, t);
}
for (const item of $('Normalize Tracks').all())
  if (!tracks.has(item.json.spotifyId)) tracks.set(item.json.spotifyId, item.json);
const all = [...tracks.values()];
const count = all.filter(completed).length;
const missing = all.length - count;
const lines = [
  `${missing ? '⚠️ Playlist finished' : '✅ Playlist ready'}: ${clean(s.playlistName)}`,
  `${count} of ${all.length} songs downloaded.${missing ? ` ${missing} ${s.timedOut ? 'not confirmed yet' : 'unavailable'}.` : ''}`,
  `View your library: ${APP_URL}/#files`,
];
return [{json:{message:lines.join('\n')}, pairedItem:{item:0}}];
"""


OUTCOMES = r"""  const successful = d.youtubeStatus === 'completed' || d.status === 'completed' || batch.some(succeeded);
  const failed = d.youtubeAttempted && d.youtubeStatus !== 'completed';
  const remoteFailure = batch.length > 0 && batch.every(complete) && !batch.some(succeeded);
  const failedFile = batch.find(f => complete(f) && !succeeded(f));
  const detail = failed ? (d.youtubeResult?.reason || d.reason || 'YouTube recovery failed')
    : remoteFailure ? String(failedFile?.failureReason || failedFile?.error?.message || failedFile?.exception?.message || (typeof failedFile?.state === 'string' ? failedFile.state : 'Soulseek transfer ended without a completed file'))
    : !batch.length && !successful ? 'Transfer is missing from slskd; download cannot be confirmed'
    : batch.some(f => /queued.*remote/i.test(String(f.state))) ? 'Soulseek source is still queued remotely'
    : 'Soulseek transfer is not finished';
  return {...d, downloadOutcome:successful ? 'completed' : failed || remoteFailure ? 'unavailable' : 'pending', downloadReason:successful ? '' : detail,
    progressBytes:bytes, progressAt:bytes > (d.progressBytes ?? 0) ? now : (d.progressAt ?? Date.parse(summary.queuedAt))};"""


def patch(workflow):
    workflow = copy.deepcopy(workflow)
    nodes = {node['name']: node for node in workflow['nodes']}
    for name in ('Completion Message', 'Send a text message', 'Report Queue Status'):
        if name not in nodes:
            raise ValueError('Missing notification node: ' + name)
    callback = nodes['Report Queue Status']['parameters']['url']
    if not callback.endswith('/workflow-status'):
        raise ValueError('Unrecognized dashboard callback URL')
    nodes['Completion Message']['parameters']['jsCode'] = FINAL.replace('APP_URL', json.dumps(callback.removesuffix('/workflow-status')))
    replacements = {
        'Queue Summary Message': 'Restore Transfer State',
        'Remote Pending Message': 'Record Remote Notice',
    }
    removed = {'Queue Summary Message', 'Remote Pending Message'}
    for prefix in ('YouTube Start', 'Immediate YouTube Start', 'YouTube Result', 'Immediate YouTube Result'):
        replacements[prefix + ' Message'] = prefix + ' Restore'
        removed.add(prefix + ' Message')
        for suffix in (' Send', ' Send Owner?', ' Send Private'):
            removed.add(prefix + suffix)
    for prefix in ('Send Queue Summary', 'Send Remote Pending'):
        removed.update((prefix, prefix + ' Owner?', prefix + ' Private'))
    for source, target in replacements.items():
        if source in nodes and target not in nodes:
            raise ValueError('Missing notification continuation: ' + target)
    for source, connections in workflow['connections'].items():
        for outlet in connections.get('main', []):
            for edge in outlet:
                edge['node'] = replacements.get(edge['node'], edge['node'])
    workflow['nodes'] = [node for node in workflow['nodes'] if node['name'] not in removed]
    workflow['connections'] = {name: value for name, value in workflow['connections'].items() if name not in removed}
    # Keep actual completion outcomes in the dashboard, even for exports predating the verbose notices.
    check = nodes.get('Check Playlist Completion')
    if check:
        params = check['parameters']
        anchor = '  return {...d, progressBytes:bytes, progressAt:bytes > (d.progressBytes ?? 0) ? now : (d.progressAt ?? Date.parse(summary.queuedAt))};'
        params['jsCode'] = params['jsCode'].replace(anchor, OUTCOMES)
    return {key: workflow[key] for key in ('name', 'nodes', 'connections', 'settings')}


if __name__ == '__main__':
    Path(sys.argv[2]).write_text(json.dumps(patch(json.loads(Path(sys.argv[1]).read_text())), indent=2))
