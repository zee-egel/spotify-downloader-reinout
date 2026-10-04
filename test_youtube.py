"""Offline fallback checks: python3 test_youtube.py (requires Node, no downloads)."""
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch as mock_patch
from youtube_fallback import download, duration_matches, matches, validate
from workflow_youtube import CHECK, RECORD, patch

track = {'spotifyId':'a'*22, 'title':'Example Song', 'primaryArtist':'Example Artist',
         'playlistName':'Example playlist', 'durationSeconds':200}
validate(track)
video = {'title':'Example Artist - Example Song (Official Audio)', 'duration':201}
assert matches(track, video)
assert not matches(track, {**video, 'duration':210})
assert not matches(track, {**video, 'title':video['title'] + ' Live'})
assert not matches(track, {**video, 'title':'Other Artist - Example Song'})
assert not matches(track, {**video, 'is_live':True})
assert not duration_matches(200, float('nan'))
try:
    validate({**track, 'durationSeconds':float('inf')})
except ValueError:
    pass
else:
    raise AssertionError('Invalid duration accepted')

# Exercise actual command construction and post-download duration validation without network.
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    job = root / 'job'
    job.mkdir()
    info = {**video, 'id':'abcdefghijk', 'acodec':'opus', 'abr':160}
    measured_duration = 201
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        assert 0 < kwargs['timeout'] <= 90
        if args[0] == 'ffprobe':
            result = {'format':{'duration':str(measured_duration)}}
        elif '--flat-playlist' in args:
            result = {'entries':[info]}
        else:
            result = info
            if '--no-simulate' in args:
                assert args[args.index('-f') + 1] == 'bestaudio'
                assert '--audio-format' not in args
                Path(args[args.index('-o') + 1].replace('%(ext)s','webm')).write_bytes(b'test audio')
        return subprocess.CompletedProcess(args,0,json.dumps(result),'')
    with mock_patch('youtube_fallback.subprocess.run',side_effect=command):
        result = download(job, {'track':track,'root':str(root/'downloads')})
        assert result['status'] == 'completed'
        assert (root/'downloads'/result['filename']).read_bytes() == b'test audio'
        measured_duration = 220
        result = download(job, {'track':track,'root':str(root/'rejected')})
        assert result['status'] == 'no_match' and not (root/'rejected').exists()

subprocess.run(['node','-e',r'''
const assert = require('node:assert/strict');
const {check,record} = JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const now = Date.now(); Date.now = () => now;
const metadata = {spotifyId:'a'.repeat(22), title:'Example Song',primaryArtist:'Example Artist',durationSeconds:200,playlistName:'Test'};
const alternate = {username:'other',filename:'song.flac'};
const download = {...metadata,batchId:'original',fallbackCandidate:alternate,fallbackAttempted:false};
const file = {id:'file',batchId:'original',filename:'song.flac',state:'Queued, Remotely',bytesTransferred:0};
const summary = {queuedAt:new Date(now-301000).toISOString(),downloads:[download],problems:[],batchIds:['original']};
function poll(s, f=[file]) {
 const $ = name => ({first:()=>({json:s}),last:()=>({json:s}),all:()=>[{json:metadata}]});
 return new Function('$','$input',check)($,{all:()=>[{json:{username:'peer',directories:[{files:f}]}}]})[0].json;
}
let out = poll({...summary,queuedAt:new Date(now-299000).toISOString()});
assert.equal(out.pendingFallback,null); assert.equal(out.ready,false);
out = poll(summary); assert.equal(out.pendingFallback.target,'soulseek'); assert.equal(out.pendingFallback.kind,'remote');
assert.equal(out.pendingFallback.fallbackCandidate.username,'other');
out = poll({...summary,downloads:[{...download,fallbackAttempted:true,fallbackAt:now-299000}]});
assert.equal(out.pendingFallback,null);
out = poll({...summary,downloads:[{...download,fallbackAttempted:true,fallbackAt:now-301000}]});
assert.equal(out.pendingFallback.target,'youtube');
out = poll(summary,[{...file,state:'InProgress',bytesTransferred:100}]);
assert.equal(out.pendingFallback,null); assert.equal(out.downloads[0].progressAt,now);
out = poll({...summary,downloads:[{...download,progressBytes:100,progressAt:now-301000}]},[{...file,state:'InProgress',bytesTransferred:100}]);
assert.equal(out.pendingFallback.kind,'remote');
out = poll(summary,[{...file,state:'Completed, Succeeded'}]);
assert.equal(out.ready,true); assert.equal(out.pendingFallback,null);
out = poll(summary,[]); assert.equal(out.ready,false); assert.equal(out.pendingFallback,null);
const problem = {...metadata,status:'no_match'};
out = poll({...summary,downloads:[],batchIds:[],problems:[problem]},[]);
assert.equal(out.pendingFallback.target,'youtube'); assert.equal(out.pendingFallback.durationSeconds,200);
const $ = () => ({last:()=>({json:out})});
const recorded = new Function('$','$json',record)($,{stdout:JSON.stringify({status:'completed',filename:'song.m4a'})})[0].json;
assert.equal(recorded.problems.length,0); assert.equal(recorded.downloads[0].status,'completed');
assert.equal(recorded.downloads[0].batchId,null); assert.equal(recorded.noMatch,0);
out = poll(recorded,[]); assert.equal(out.completedFiles,1); assert.equal(out.ready,true);
// Never restart a failed YouTube attempt forever.
out = poll({...summary,downloads:[{...download,youtubeAttempted:true,youtubeStatus:'failed'}]});
assert.equal(out.pendingFallback,null); assert.equal(out.ready,true);
'''], input=json.dumps({'check':CHECK,'record':RECORD}), text=True, check=True)
print('YouTube matching and retry timing checks passed')
