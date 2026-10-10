"""Add immediate YouTube recovery and faster completion updates to a live export."""
import copy
import json
from pathlib import Path
import sys
from workflow_recovery import patch as recovery_patch


def patch(workflow):
    workflow = copy.deepcopy(workflow)
    nodes = {n['name']: n for n in workflow['nodes']}
    if 'Immediate YouTube Fallback' in nodes:
        raise ValueError('Immediate fallback already installed')
    def edge(name):
        return {'node': name, 'type': 'main', 'index': 0}
    def clone(source, name, x, y):
        node = copy.deepcopy(nodes[source])
        node.update(id=name.lower().replace(' ', '-'), name=name, position=[x, y])
        workflow['nodes'].append(node)
        return node
    choice = clone('Remote Queue Fallback', 'Immediate YouTube Fallback', 6040, 1296)
    choice['parameters']['conditions']['conditions'][0]['leftValue'] = "={{ ['no_match', 'review', 'search_timeout', 'error'].includes($json.status) && !$json.youtubeAttempted }}"
    request = clone('Download YouTube Audio', 'Download Unmatched Track', 6270, 1200)
    request['parameters']['jsonBody'] = "={{ {profileId: $('Playlist desk webhook').first().json.body.profileId ?? 'owner', submissionId: $('Playlist desk webhook').first().json.body.submissionId, track: $('Normalize Tracks').all().find(i => i.json.spotifyId === $json.spotifyId).json} }}"
    record = clone('Restore Track Result', 'Record Immediate YouTube Result', 6500, 1200)
    record['parameters']['jsCode'] = """
const original = $('Restore Track Result').itemMatching(0).json;
const result = $json;
const status = ['completed','no_match','failed'].includes(result?.status) ? result.status : 'failed';
return [{json:{...original, status:status === 'completed' ? 'completed' : original.status,
  youtubeStatus:status, youtubeAttempted:true, youtubeResult:result,
  reason:result.reason ?? (status === 'failed' ? 'YouTube download failed' : ''),
  fallbackSource:'YouTube', fallbackAttempted:true,
  ...(status === 'completed' ? {batchId:null, username:'YouTube', filename:result.filename} : {})},
  pairedItem:{item:0}}];
"""
    report = clone('Report Track Result', 'Report Immediate YouTube Result', 6740, 1200)
    restore = clone('Restore Track Result', 'Restore Immediate YouTube Result', 6970, 1200)
    restore['parameters']['jsCode'] = "return [{json:$('Record Immediate YouTube Result').itemMatching(0).json, pairedItem:{item:0}}];"
    connections = workflow['connections']
    connections['Restore Track Result'] = {'main': [[edge(choice['name'])]]}
    connections[choice['name']] = {'main': [[edge(request['name'])], [edge('Loop Over Items')]]}
    for source, target in [(request['name'], record['name']), (record['name'], report['name']), (report['name'], restore['name']), (restore['name'], 'Loop Over Items')]:
        connections[source] = {'main': [[edge(target)]]}
    nodes['Summary']['parameters']['jsCode'] = """
const items = $input.all().map(item => item.json);
const counts = items.reduce((out,item) => {out[item.status]=(out[item.status]??0)+1; return out;}, {});
const downloads = items.filter(i => ['queued','completed'].includes(i.status));
const problems = items.filter(i => !['queued','completed'].includes(i.status));
return [{json:{playlistName:items[0]?.playlistName ?? $('Get Playlist').first().json.name,
  totalSpotifyTracks:items.length, queuedDownloads:downloads.length, downloads, problems,
  batchIds:[...new Set(downloads.map(i=>i.batchId).filter(Boolean))], queuedAt:new Date().toISOString(),
  duplicates:counts['already/duplicate']??0, reviewRequired:counts.review??0,
  noMatch:counts.no_match??0, searchTimeouts:counts.search_timeout??0, errors:counts.error??0,
  fallbackAttempts:items.filter(i=>i.youtubeAttempted).length}}];
"""
    check = nodes['Check Playlist Completion']['parameters']['jsCode']
    check = check.replace("const now = Date.now(), wait = 5 * 60 * 1000;", "try { const recovered = $('Restore YouTube State').last().json; if ((recovered.fallbackAttempts ?? 0) > (summary.fallbackAttempts ?? 0)) summary = recovered; } catch (_) { /* no recovery yet */ }\nconst now = Date.now(), wait = 5 * 60 * 1000;")
    check = check.replace("if (input.some(i => i.json?.error)) return [{json: {...summary, ready: false, pendingFallback: null, pendingRemoteReports: []}}];", "")
    check = check.replace('if (elapsed < wait || pendingFallback) continue;', 'if (pendingFallback) continue;')
    check = check.replace('if (elapsed >= wait && !pendingFallback)', 'if (!pendingFallback)')
    check = check.replace("failedFiles:downloads.filter(d => d.youtubeAttempted && d.youtubeStatus !== 'completed').length,", "failedFiles:[...downloads, ...(summary.problems ?? [])].filter(d => d.youtubeAttempted && d.youtubeStatus !== 'completed').length,")
    nodes['Check Playlist Completion']['parameters']['jsCode'] = check
    nodes['Wait for Downloads']['parameters'].update(amount=10, unit='seconds')
    connections['Restore YouTube State'] = {'main': [[edge('Get Download Transfers')]]}
    params = nodes['Download YouTube Audio']['parameters']
    params['jsonBody'] = params['jsonBody'].replace('track: $json.pendingFallback', "submissionId: $('Playlist desk webhook').first().json.body.submissionId, track: $json.pendingFallback")
    return recovery_patch(workflow)


if __name__ == '__main__':
    Path(sys.argv[2]).write_text(json.dumps(patch(json.loads(Path(sys.argv[1]).read_text())), indent=2))
