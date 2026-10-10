"""Read-only runtime, recovery and optional networking diagnostics. No credentials printed."""
import argparse
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import urllib.error
import urllib.request


def diagnose(args):
    report = {'runtime': {}}
    for package in ('yt-dlp', 'yt-dlp-ejs'):
        try:
            report['runtime'][package] = version(package)
        except PackageNotFoundError:
            report['runtime'][package] = 'MISSING'
    for command in ('node', 'ffprobe'):
        try:
            if not shutil.which(command):
                raise FileNotFoundError(command)
            flag = '--version' if command == 'node' else '-version'
            result = subprocess.run([command, flag], capture_output=True, text=True, check=True, timeout=5)
            report['runtime'][command] = result.stdout.splitlines()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            report['runtime'][command] = 'MISSING or not runnable'
    state = Path(os.environ.get('STATE_FILE', '/state/runs.json'))
    if state.is_file():
        try:
            runs = json.loads(state.read_text())
            counts = Counter()
            for run in runs:
                for progress in (run.get('youtubeProgress') or {}).values():
                    if progress.get('code'):
                        counts[progress['code']] += 1
            report['recordedYouTubeOutcomes'] = dict(counts)
        except (OSError, ValueError, TypeError, AttributeError):
            report['recordedYouTubeOutcomes'] = 'State could not be read'
    if args.slskd:
        origin = os.environ.get('SLSKD_URL', '').rstrip('/')
        key = os.environ.get('SLSKD_API_KEY', '')
        if not origin or not key:
            report['slskd'] = 'Set SLSKD_URL and SLSKD_API_KEY'
        else:
            try:
                request = urllib.request.Request(origin + '/api/v0/transfers/downloads', headers={'X-API-Key': key})
                with urllib.request.urlopen(request, timeout=5) as response:
                    report['slskd'] = 'API reachable; verify Soulseek login and peer connectivity in slskd'
            except urllib.error.HTTPError as error:
                report['slskd'] = 'API returned HTTP ' + str(error.code)
            except (OSError, ValueError):
                report['slskd'] = 'API unreachable'
    if args.peer_host:
        try:
            with socket.create_connection((args.peer_host, args.peer_port), timeout=5):
                report['peerTCP'] = 'TCP reachable from this machine; this does not verify the advertised Soulseek address or handshake'
        except OSError:
            report['peerTCP'] = 'TCP unreachable from this machine; check proxy, firewall and port forwarding'
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--slskd', action='store_true', help='Check the configured slskd API using environment credentials')
    parser.add_argument('--peer-host', help='Probe the public peer host from this machine (prefer a separate network)')
    parser.add_argument('--peer-port', type=int)
    args = parser.parse_args()
    if bool(args.peer_host) != (args.peer_port is not None) or (args.peer_port is not None and not 0 < args.peer_port < 65536):
        parser.error('Supply both --peer-host and --peer-port (1–65535)')
    print(json.dumps(diagnose(args), indent=2))
