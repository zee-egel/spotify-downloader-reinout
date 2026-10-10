"""Add per-track recovery notices and truthful final outcomes to an n8n export."""
import copy
import json
from pathlib import Path
import sys


HELPERS = r"""
const clean = value => String(value ?? '').replace(/[\r\n\t]+/g, ' ').trim();
const metadata = new Map($('Normalize Tracks').all().map(i => [i.json.spotifyId, i.json]));
const describe = track => {
  const t = {...metadata.get(track.spotifyId), ...track};
  return `${clean(t.artist || t.primaryArtist) || 'Unknown artist'} — ${clean(t.title) || 'Untitled track'}`;
};
const reasonFor = t => clean(t.youtubeResult?.reason || t.downloadReason || t.reason || t.fallbackError).slice(0, 500) || ({
  no_match:'No matching Soulseek file', review:'No confident Soulseek match',
  search_timeout:'Soulseek search timed out', error:'Soulseek search or queue request failed',
  'already/duplicate':'Already queued elsewhere; not downloaded by this run',
  queued:'Waiting for the Soulseek source', pending:'Waiting for transfer status',
}[t.status] || 'No verified download; source did not provide a failure reason');
function messages(lines) {
  // Keep every track, splitting at Telegram-safe boundaries rather than dropping a tail.
  const chunks = [];
  let current = '';
  for (const line of lines) {
    for (let offset = 0; offset < line.length || offset === 0;) {
      let end = Math.min(offset + 3400, line.length);
      if (end < line.length && /[\uD800-\uDBFF]/.test(line[end - 1])) end--;
      const part = line.slice(offset, end);
      offset = end || 1;
      if (current && current.length + part.length + 1 > 3500) { chunks.push(current); current = ''; }
      current += (current ? '\n' : '') + part;
    }
  }
  if (current) chunks.push(current);
  return chunks.map((message, i) => ({json:{message:chunks.length > 1 ? `(${i + 1}/${chunks.length})\n${message}` : message}, pairedItem:{item:0}}));
}
"""

FINAL = HELPERS + r"""
const s = $json;
const tracks = new Map();
const priority = t => t.youtubeStatus === 'completed' || t.downloadOutcome === 'completed' || t.status === 'completed' ? 3 : t.youtubeAttempted ? 2 : t.status === 'already/duplicate' ? 0 : 1;
for (const t of [...(s.downloads ?? []), ...(s.problems ?? [])]) {
  const key = t.spotifyId || `${t.artist}:${t.title}`;
  if (!tracks.has(key) || priority(t) > priority(tracks.get(key))) tracks.set(key, t);
}
for (const t of metadata.values())
  if (!tracks.has(t.spotifyId)) tracks.set(t.spotifyId, {...t, status:'unknown', reason:'No track result was reported by the workflow'});
const all = [...tracks.values()];
const complete = all.filter(t => t.youtubeStatus === 'completed' || t.downloadOutcome === 'completed' || t.status === 'completed');
const recovered = complete.filter(t => t.youtubeStatus === 'completed');
const missing = all.filter(t => !complete.includes(t));
const recoveryFailed = missing.filter(t => t.youtubeAttempted && t.youtubeStatus !== 'completed');
const other = missing.filter(t => !recoveryFailed.includes(t));
const lines = [
  `${missing.length ? '⚠️ Playlist finished with issues' : '✅ Playlist downloads complete'}: ${clean(s.playlistName)}`,
  `${complete.length} of ${all.length} unique songs downloaded; ${missing.length} not downloaded.`,
];
if (s.timedOut) lines.push('Monitoring ended after 48 hours. Pending transfers may still finish later; they are not confirmed downloads.');
if (recovered.length) lines.push('', `✅ Recovered on YouTube (${recovered.length}):`, ...recovered.map(t => `• ${describe(t)}`));
if (recoveryFailed.length) lines.push('', `❌ Still not downloaded after YouTube (${recoveryFailed.length}):`, ...recoveryFailed.map(t => `• ${describe(t)} — ${reasonFor(t)}`));
if (other.length) lines.push('', `Not downloaded (${other.length}):`, ...other.map(t => `• ${describe(t)} — ${reasonFor(t)}`));
return messages(lines);
"""


QUEUE = HELPERS + r"""
const s = $json;
const all = [...(s.downloads ?? []), ...(s.problems ?? [])];
const complete = all.filter(t => t.youtubeStatus === 'completed' || t.downloadOutcome === 'completed' || t.status === 'completed');
const pending = all.filter(t => !complete.includes(t));
return messages([
  `📋 Playlist still processing: ${clean(s.playlistName)}`,
  `${complete.length} songs downloaded; ${pending.length} tracks not downloaded yet.`,
  ...pending.map(t => `• ${describe(t)} — ${reasonFor(t)}`),
  'Downloads and recovery continue. A final update will list recovered and unavailable tracks.',
]);
"""

REMOTE = HELPERS + r"""
const s = $json;
return messages([
  `⏳ Tracks still queued remotely: ${clean(s.playlistName)}`,
  ...(s.pendingRemoteReports ?? []).map(t => `• ${describe(t)} — ${t.fallbackAttempted ? 'Alternate source is also queued remotely' : 'No free alternate source'}`),
  'These are still pending, not failed downloads. Monitoring continues.',
]);
"""

START = HELPERS + r"""
const state = $json;
const track = state.pendingFallback ?? state;
return messages([
  `⚠️ Soulseek has not downloaded this track: ${clean(track.playlistName || state.playlistName)}`,
  `• ${describe(track)} — ${clean(track.youtubeOriginalReason) || reasonFor(track)}`,
  (track.youtubeRetries ?? 0) > 0 ? 'Trying YouTube again now…' : 'Trying YouTube now…',
]);
"""

RESULT = HELPERS + r"""
const state = $json;
const original = SOURCE;
const track = state.pendingFallback ?? [...(state.downloads ?? []), ...(state.problems ?? [])].find(t => t.spotifyId === original.spotifyId) ?? state;
const result = track.youtubeResult ?? {};
const success = track.youtubeStatus === 'completed';
const waiting = !track.youtubeAttempted && result.retryable === true;
return messages([
  `${success ? '✅ YouTube recovery succeeded' : waiting ? '⏳ YouTube recovery waiting' : '❌ Still not downloaded after YouTube'}: ${clean(track.playlistName || state.playlistName)}`,
  `• ${describe(track)} — ${success ? 'Downloaded and audio validated' : waiting ? 'Downloader busy; retry scheduled' : reasonFor(track)}`,
]);
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
    nodes = {n['name']: n for n in workflow['nodes']}
    required = ('Check Playlist Completion', 'Completion Message', 'Send a text message',
                'Download YouTube Audio', 'Download Unmatched Track')
    for name in required:
        if name not in nodes:
            raise ValueError('Missing recovery node: ' + name)
    if 'YouTube Start Message' in nodes:
        raise ValueError('Recovery notifications already installed')
    connections = workflow['connections']
    def edge(name):
        return {'node':name, 'type':'main', 'index':0}
    def code_node(name, code, position):
        node = {'id':name.lower().replace(' ', '-'), 'name':name, 'type':'n8n-nodes-base.code',
                'typeVersion':2, 'position':position, 'parameters':{'jsCode':code}}
        workflow['nodes'].append(node)
        nodes[name] = node
        return node
    template = nodes['Send a text message']
    callback = nodes['Report Queue Status']
    # Existing owner bot stays connected. Other profiles use their private app settings.
    def route_sender(sender, continuation):
        name = sender['name']
        route = name + ' Owner?'
        private = name + ' Private'
        x,y = sender['position']
        guard = {'id':route.lower().replace(' ', '-'), 'name':route, 'type':'n8n-nodes-base.if',
                 'typeVersion':2.2, 'position':[x-160,y], 'parameters':{'conditions':{
                     'options':{'caseSensitive':True,'leftValue':'','typeValidation':'strict','version':2},
                     'conditions':[{'id':route,'leftValue':"={{ ($('Playlist desk webhook').first().json.body.profileId ?? 'owner') === 'owner' }}",
                                    'rightValue':'','operator':{'type':'boolean','operation':'true','singleValue':True}}], 'combinator':'and'},'options':{}}}
        if sender['type'] == 'n8n-nodes-base.telegram':
            sender['parameters']['text'] = "={{ $json.message.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') }}"
            sender['parameters'].setdefault('additionalFields', {}).update(appendAttribution=False, parse_mode='HTML')
        sender['onError'] = 'continueRegularOutput'
        personal = {'id':private.lower().replace(' ', '-'),'name':private,'type':'n8n-nodes-base.httpRequest',
                    'typeVersion':4.5,'position':[x,y+140], 'credentials':copy.deepcopy(callback['credentials']),
                    'onError':'continueRegularOutput','parameters':{
                        'method':'POST','url':callback['parameters']['url'].removesuffix('/workflow-status')+'/workflow-notify',
                        'authentication':'genericCredentialType','genericAuthType':'httpHeaderAuth','sendBody':True,'specifyBody':'json',
                        'jsonBody':"={{ {submissionId: $('Playlist desk webhook').first().json.body.submissionId, message: $json.message} }}",'options':{'timeout':15000}}}
        workflow['nodes'].extend([guard,personal]);nodes[route]=guard;nodes[private]=personal
        connections[route] = {'main':[[edge(name)],[edge(private)]]}
        connections[name] = copy.deepcopy(continuation)
        connections[private] = copy.deepcopy(continuation)
        return route
    # Existing message nodes retain their continuations and credentials.
    for name in ('Send a text message','Send Queue Summary','Send Remote Pending'):
        sender = nodes[name]
        continuation = copy.deepcopy(connections.get(name, {'main':[[]]}))
        predecessors = [(source,outlet,index) for source,c in connections.items()
                        for outlet,items in enumerate(c.get('main',[])) for index,e in enumerate(items) if e['node']==name]
        route = route_sender(sender,continuation)
        for source,outlet,index in predecessors:
            connections[source]['main'][outlet][index] = edge(route)
    nodes['Completion Message']['parameters']['jsCode'] = FINAL
    nodes['Queue Summary Message']['parameters']['jsCode'] = QUEUE
    nodes['Remote Pending Message']['parameters']['jsCode'] = REMOTE
    for name in ('Record YouTube Result','Record Immediate YouTube Result'):
        params = nodes[name]['parameters']
        status_line = "const status = ['completed','no_match','failed'].includes(result?.status) ? result.status : 'failed';"
        params['jsCode'] = params['jsCode'].replace(status_line, status_line + "\nif (status === 'failed' && !result?.reason) result = {...result, status, reason:result?.error ? 'YouTube request failed; a disconnected request may still finish on the download host.' : 'YouTube downloader returned no usable result.'};")
        params['jsCode'] = params['jsCode'].replace('youtubeResult:result,',
            "youtubeResult:result, youtubeOriginalReason:original.youtubeOriginalReason ?? original.downloadReason ?? original.reason ?? ({no_match:'No matching Soulseek file',review:'No confident Soulseek match',search_timeout:'Soulseek search timed out',error:'Soulseek search or queue request failed'}[original.status] || 'Soulseek download did not finish'),")
    params = nodes['Check Playlist Completion']['parameters']
    anchor = '  return {...d, progressBytes:bytes, progressAt:bytes > (d.progressBytes ?? 0) ? now : (d.progressAt ?? Date.parse(summary.queuedAt))};'
    if anchor not in params['jsCode']:
        raise ValueError('Unrecognized transfer outcome mapper')
    params['jsCode'] = params['jsCode'].replace(anchor, OUTCOMES)
    def hook(prefix, generator, source, target, restore, position):
        x,y = position
        message_name = prefix + ' Message'
        restore_name = prefix + ' Restore'
        code_node(message_name,generator,[x,y])
        code_node(restore_name,restore,[x+650,y])
        sender = copy.deepcopy(template)
        sender.update(id=prefix.lower().replace(' ','-')+'-send',name=prefix+' Send',position=[x+350,y])
        workflow['nodes'].append(sender);nodes[sender['name']]=sender
        route = route_sender(sender,{'main':[[edge(restore_name)]]})
        connections[message_name] = {'main':[[edge(route)]]}
        connections[restore_name] = {'main':[[edge(target)]]}
        replaced = False
        for outlets in connections[source]['main']:
            for index,item in enumerate(outlets):
                if item['node'] == target:
                    outlets[index] = edge(message_name); replaced = True
        if not replaced:
            raise ValueError('Missing notification insertion edge: '+source+' → '+target)
    hook('YouTube Start', START, 'YouTube Fallback', 'Download YouTube Audio',
         "return [{json:$('Check Playlist Completion').last().json}];", [4700,1900])
    hook('Immediate YouTube Start', START, 'Immediate YouTube Fallback', 'Download Unmatched Track',
         "return [{json:$('Restore Track Result').itemMatching(0).json, pairedItem:{item:0}}];", [6050,2500])
    hook('YouTube Result', RESULT.replace('SOURCE', "$('Check Playlist Completion').last().json.pendingFallback"),
         'Record YouTube Result','Report YouTube Status', "return [{json:$('Record YouTube Result').last().json}];", [5200,3100])
    hook('Immediate YouTube Result', RESULT.replace('SOURCE', "$('Restore Track Result').itemMatching(0).json"),
         'Record Immediate YouTube Result','Report Immediate YouTube Result',
         "return [{json:$('Record Immediate YouTube Result').itemMatching(0).json, pairedItem:{item:0}}];", [6500,3700])
    # Imported stray If node had no false continuation and blocked alternate Soulseek queues.
    stray = nodes.get('Youtube fallback')
    if stray and connections.get('Queue Fallback',{}).get('main') == [[edge('Youtube fallback')]] and connections.get('Youtube fallback',{}).get('main') == [[edge('Record Fallback')]]:
        connections['Queue Fallback'] = {'main':[[edge('Record Fallback')]]}
    return {k:workflow[k] for k in ('name','nodes','connections','settings')}


if __name__ == '__main__':
    Path(sys.argv[2]).write_text(json.dumps(patch(json.loads(Path(sys.argv[1]).read_text())),indent=2))
