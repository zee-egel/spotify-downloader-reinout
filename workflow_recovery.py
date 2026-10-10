"""Upgrade recovery in a workflow export, without contacting or changing n8n.

python3 workflow_recovery.py before.json after.json
"""
import copy
import json
from pathlib import Path
import sys


RETRY = """const retryable = status === 'failed' && result?.retryable === true;
const retries = (original.youtubeRetries ?? 0) + (retryable ? 1 : 0);
const done = !retryable || retries >= 8;
const retryAt = done ? null : Date.now() + 60000;
if (retryable && done) result = {...result, reason:'YouTube downloader remained busy after eight attempts; try this track again later.'};"""


def recovery_record(code, immediate=False):
    if 'const retryable =' in code:
        return code
    anchor = "const status = ['completed','no_match','failed'].includes(result?.status) ? result.status : 'failed';"
    if anchor not in code:
        raise ValueError('Unrecognized YouTube result recorder')
    code = code.replace('const result = $json;', 'let result = $json;')
    code = code.replace(anchor, anchor + '\n' + RETRY)
    code = code.replace('const done = true;\n', '')
    code = code.replace('youtubeAttempted:true', 'youtubeAttempted:done')
    code = code.replace('youtubeResult:result,', 'youtubeResult:result, youtubeRetries:retries, youtubeRetryAt:retryAt,')
    if not immediate:
        code = code.replace('...state, downloads, problems,', '...state, youtubeUpdatedAt:Date.now(), downloads, problems,')
    return code


def recovery_check(code):
    code = code.replace('if ((recovered.fallbackAttempts ?? 0) > (summary.fallbackAttempts ?? 0))',
                        'if ((recovered.youtubeUpdatedAt ?? 0) > (summary.youtubeUpdatedAt ?? 0) || (recovered.fallbackAttempts ?? 0) > (summary.fallbackAttempts ?? 0))')
    if 'd.youtubeRetryAt > now' in code:
        return code
    if 'const now = Date.now(), wait = 5 * 60 * 1000;' not in code:
        raise ValueError('Unrecognized completion poller')
    code = code.replace('if (elapsed < wait || pendingFallback) continue;',
                        'if (elapsed < wait || pendingFallback) continue;\n  if (d.youtubeRetryAt > now) continue;')
    # The faster completion patch removes the elapsed guard.
    if 'd.youtubeRetryAt > now' not in code:
        code = code.replace('if (pendingFallback) continue;', 'if (pendingFallback) continue;\n  if (d.youtubeRetryAt > now) continue;')
    code = code.replace('if (d.fallbackAttempted &&', 'if (!d.youtubeRetryAt && d.fallbackAttempted &&')
    code = code.replace('if (!eligible.has(p.status) || p.youtubeAttempted) continue;',
                        'if (!eligible.has(p.status) || p.youtubeAttempted) continue;\n  if (p.youtubeRetryAt > now) { unresolved = true; continue; }')
    return code


def patch(workflow):
    workflow = copy.deepcopy(workflow)
    nodes = {node['name']: node for node in workflow['nodes']}
    for name in ('Check Playlist Completion', 'Record YouTube Result'):
        if name not in nodes:
            raise ValueError('Install YouTube recovery before this patch: missing ' + name)
    params = nodes['Check Playlist Completion']['parameters']
    params['jsCode'] = recovery_check(params['jsCode'])
    for name in ('Record YouTube Result', 'Record Immediate YouTube Result'):
        if name in nodes:
            params = nodes[name]['parameters']
            params['jsCode'] = recovery_record(params['jsCode'], immediate=name.startswith('Record Immediate'))
    choice = nodes.get('Immediate YouTube Fallback')
    if choice:
        for condition in choice['parameters']['conditions']['conditions']:
            if isinstance(condition.get('leftValue'), str):
                condition['leftValue'] = condition['leftValue'].replace("['no_match', 'review', 'search_timeout']", "['no_match', 'review', 'search_timeout', 'error']")
    for name in ('Download YouTube Audio', 'Download Unmatched Track'):
        node = nodes.get(name)
        if node and node['type'] == 'n8n-nodes-base.httpRequest':
            # Avoid the failing nested expression; the downloader needs only normalized metadata.
            params = node['parameters']
            if 'jsonBody' in params:
                params['jsonBody'] = params['jsonBody'].replace(
                    "{...$('Normalize Tracks').all().find(i => i.json.spotifyId === $json.spotifyId).json, ...$json}",
                    "$('Normalize Tracks').all().find(i => i.json.spotifyId === $json.spotifyId).json")
            # Keep HTTP 409's explicit retryable body available to the recorder.
            options = node['parameters'].setdefault('options', {})
            response = options.setdefault('response', {}).setdefault('response', {})
            response.update(neverError=True, responseFormat='json', fullResponse=False)
    return {key: workflow[key] for key in ('name', 'nodes', 'connections', 'settings')}


if __name__ == '__main__':
    Path(sys.argv[2]).write_text(json.dumps(patch(json.loads(Path(sys.argv[1]).read_text())), indent=2))
