"""Patch an exported live workflow: python3 workflow_youtube.py before.json after.json.

Deploy the app with yt-dlp before applying the resulting n8n update body.
"""
import copy
import json
from pathlib import Path
import sys


CHECK = r"""
let summary = $('Summary').first().json;
try { summary = $('Wait for Downloads').last().json; } catch (_) { /* first poll */ }
const now = Date.now(), wait = 5 * 60 * 1000;
const elapsed = now - Date.parse(summary.queuedAt);
const input = $input.all();
if (input.some(i => i.json?.error)) return [{json: {...summary, ready: false, pendingFallback: null, pendingRemoteReports: []}}];
const users = input.flatMap(i => Array.isArray(i.json) ? i.json : [i.json]);
const files = users.flatMap(u => (u.directories ?? []).flatMap(d => (d.files ?? []).map(f => ({...f, username:u.username}))));
const complete = f => typeof f.state === 'number' ? Boolean(f.state & 16) : /completed/i.test(String(f.state));
const succeeded = f => complete(f) && (typeof f.state === 'number' ? Boolean(f.state & 32) : /succeeded/i.test(String(f.state)));
const metadata = new Map($('Normalize Tracks').all().map(i => [i.json.spotifyId, i.json]));
const downloads = (summary.downloads ?? []).map(d => {
  const batch = files.filter(f => f.batchId === d.batchId);
  const bytes = batch.reduce((n,f) => n + Number(f.bytesTransferred ?? 0), 0);
  return {...d, progressBytes:bytes, progressAt:bytes > (d.progressBytes ?? 0) ? now : (d.progressAt ?? Date.parse(summary.queuedAt))};
});
let pendingFallback = null;
let unresolved = false;
for (const d of downloads) {
  if (d.youtubeStatus === 'completed' || d.youtubeAttempted) continue;
  const batch = files.filter(f => f.batchId === d.batchId);
  if (batch.some(succeeded)) continue;
  unresolved = true;
  if (elapsed < wait || pendingFallback) continue;
  // Unknown/multiple transfers cannot safely be replaced: never guess that they failed.
  if (batch.length !== 1) continue;
  const f = batch[0];
  const stalled = now - d.progressAt >= wait;
  if (!complete(f) && !stalled) continue;
  if (d.fallbackAttempted && !d.youtubePending && now - (d.fallbackAt ?? now) < wait) continue;
  const alternate = d.fastFallbackCandidate ?? d.fallbackCandidate;
  const different = alternate && (alternate.username !== f.username || alternate.filename !== f.filename);
  const target = !d.fallbackAttempted && different ? 'soulseek' : 'youtube';
  if (!complete(f) && (!f.id || !f.username)) continue;
  if (d.cancelRetryAt && now - d.cancelRetryAt < wait) continue;
  pendingFallback = {...metadata.get(d.spotifyId), ...d, target, fallbackCandidate:alternate,
    kind:complete(f) ? 'failed' : 'remote', cancelUsername:f.username, cancelId:f.id};
}
const eligible = new Set(['no_match','review','search_timeout','error']);
for (const p of summary.problems ?? []) {
  if (!eligible.has(p.status) || p.youtubeAttempted) continue;
  unresolved = true;
  if (elapsed >= wait && !pendingFallback)
    pendingFallback = {...metadata.get(p.spotifyId), ...p, target:'youtube', kind:'failed'};
}
const ids = new Set(downloads.map(d => d.batchId).filter(Boolean));
const tracked = files.filter(f => ids.has(f.batchId));
const youtubeCompleted = downloads.filter(d => d.youtubeStatus === 'completed').length;
const timedOut = elapsed >= 48 * 60 * 60 * 1000;
return [{json:{...summary, downloads, batchIds:[...ids],
  completedFiles:tracked.filter(succeeded).length + youtubeCompleted,
  failedFiles:downloads.filter(d => d.youtubeAttempted && d.youtubeStatus !== 'completed').length,
  pendingFiles:tracked.filter(f => !complete(f)).length,
  missingBatches:ids.size - new Set(tracked.map(f=>f.batchId)).size,
  downloadingFiles:tracked.filter(f=>/downloading|transferring/i.test(String(f.state))).length,
  queuedRemotely:tracked.filter(f=>/queued.*remote/i.test(String(f.state))).length,
  queuedLocally:tracked.filter(f=>/queued.*local/i.test(String(f.state))).length,
  pendingFallback:timedOut ? null : pendingFallback, pendingRemoteReports:[],
  ready:timedOut || (!unresolved && !pendingFallback), timedOut}}];
"""

RECORD = r"""
const state = $('Check Playlist Completion').last().json;
const original = state.pendingFallback;
const result = $json;
const known = ['pending','completed','no_match','failed'].includes(result.status);
const errors = known ? 0 : (original.youtubeErrors ?? 0) + 1;
const status = known ? result.status : errors >= 3 ? 'failed' : 'pending';
const done = status !== 'pending';
const success = status === 'completed';
const update = {...original, youtubeErrors:errors, youtubeStatus:status, youtubePending:!done,
  youtubeAttempted:done, youtubeResult:result, fallbackSource:'YouTube', fallbackAttempted:true,
  fallbackAt:original.fallbackAt ?? Date.now(),
  ...(success ? {status:'completed', batchId:null, username:'YouTube', filename:result.filename} : {})};
delete update.target;
delete update.kind;
let downloads = state.downloads.map(d => d.spotifyId === original.spotifyId ? {...d,...update} : d);
let problems = state.problems.map(p => p.spotifyId === original.spotifyId ? {...p,...update} : p);
if (success && !downloads.some(d => d.spotifyId === original.spotifyId)) downloads.push(update);
if (success) problems = problems.filter(p => p.spotifyId !== original.spotifyId);
return [{json:{...state, downloads, problems, batchIds:downloads.map(d=>d.batchId).filter(Boolean),
  queuedDownloads:downloads.length, noMatch:problems.filter(p=>p.status==='no_match').length,
  reviewRequired:problems.filter(p=>p.status==='review').length,
  searchTimeouts:problems.filter(p=>p.status==='search_timeout').length,
  errors:problems.filter(p=>p.status==='error').length,
  fallbackAttempts:(state.fallbackAttempts ?? 0) + (done ? 1 : 0), pendingFallback:null, ready:false}}];
"""


def patch(workflow):
    workflow = copy.deepcopy(workflow)
    nodes = {n['name']: n for n in workflow['nodes']}
    if 'YouTube Fallback' in nodes:
        raise ValueError('YouTube fallback already installed')
    connections = workflow['connections']
    nodes['Check Playlist Completion']['parameters']['jsCode'] = CHECK
    code = nodes['Record Fallback']['parameters']['jsCode']
    if 'fallbackAttempted: true,' not in code:
        raise ValueError('Unrecognized fallback recorder')
    nodes['Record Fallback']['parameters']['jsCode'] = code.replace('fallbackAttempted: true,', 'fallbackAttempted: true, fallbackAt: Date.now(), progressAt: Date.now(), progressBytes: 0,')
    # Cancellation failure is retryable, and must never authorize a second download.
    nodes['Record Cancel Failure']['parameters']['jsCode'] = """
const state = $('Check Playlist Completion').last().json;
const downloads = state.downloads.map(d => d.batchId === state.pendingFallback.batchId
  ? {...d, cancelRetryAt:Date.now()} : d);
return [{json:{...state, downloads, pendingFallback:null, ready:false}}];
"""
    def edge(name):
        return {'node': name, 'type': 'main', 'index': 0}
    choice = copy.deepcopy(nodes['Remote Queue Fallback'])
    choice.update(id='youtube-fallback-choice', name='YouTube Fallback', position=[5000, 800])
    choice['parameters']['conditions']['conditions'][0]['leftValue'] = "={{ $json.pendingFallback.target === 'youtube' }}"
    request = copy.deepcopy(nodes['Report Queue Status'])
    base = request['parameters']['url'].removesuffix('/workflow-status')
    request.update(id='youtube-start-poll', name='Download YouTube Audio', position=[5200, 700], onError='continueRegularOutput')
    request['credentials'] = {'httpHeaderAuth': copy.deepcopy(nodes['Playlist desk webhook']['credentials']['httpHeaderAuth'])}
    request['parameters'] = {'method':'POST', 'url':base + '/workflow-youtube',
        'authentication':'genericCredentialType', 'genericAuthType':'httpHeaderAuth',
        'sendBody':True, 'specifyBody':'json', 'options':{'timeout':15000},
        'jsonBody':"={{ { submissionId: $('Playlist desk webhook').first().json.body.submissionId, track: $json.pendingFallback } }}"}
    record = {'id':'youtube-record', 'name':'Record YouTube Result', 'type':'n8n-nodes-base.code',
              'typeVersion':2, 'position':[5400,700], 'parameters':{'jsCode':RECORD}}
    report = copy.deepcopy(nodes['Report Fallback Status'])
    report.update(id='youtube-report', name='Report YouTube Status', position=[5600,700])
    restore = {'id':'youtube-restore', 'name':'Restore YouTube State', 'type':'n8n-nodes-base.code',
               'typeVersion':2, 'position':[5800,700],
               'parameters':{'jsCode':"return [{json:$('Record YouTube Result').last().json}];"}}
    for source, outlet in [('Remote Queue Fallback',1), ('Cancelled and Safe',0)]:
        if connections[source]['main'][outlet] != [edge('Queue Fallback')]:
            raise ValueError('Unexpected queue routing: ' + source)
        connections[source]['main'][outlet] = [edge('YouTube Fallback')]
    connections['YouTube Fallback'] = {'main':[[edge('Download YouTube Audio')],[edge('Queue Fallback')]]}
    for source, target in [('Download YouTube Audio','Record YouTube Result'),('Record YouTube Result','Report YouTube Status'),
                           ('Report YouTube Status','Restore YouTube State'),('Restore YouTube State','Wait for Downloads')]:
        connections[source] = {'main':[[edge(target)]]}
    workflow['nodes'].extend([choice,request,record,report,restore])
    return {k: workflow[k] for k in ('name','nodes','connections','settings')}


if __name__ == '__main__':
    Path(sys.argv[2]).write_text(json.dumps(patch(json.loads(Path(sys.argv[1]).read_text())), indent=2))
