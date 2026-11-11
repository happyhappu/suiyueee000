"""Run voice with only explicitly whitelisted Pepper expression gestures."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

root = Path(__file__).resolve().parent
config = json.loads((root / 'voice-config.json').read_text(encoding='utf-8-sig'))
parser = argparse.ArgumentParser()
parser.add_argument('--test-text')
args = parser.parse_args()
with socket.socket() as port_check:
    port_check.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        port_check.bind(('127.0.0.1', 9560))
    except OSError:
        raise SystemExit('Bridge port 9560 is in use. Stop the existing gateway first.')
runtime = root / '.runtime'
runtime.mkdir(exist_ok=True)
env = dict(os.environ)
sdk = Path(config['sdk'])
env['PYTHONPATH'] = str(sdk / 'lib')
env['PATH'] = str(sdk / 'bin') + os.pathsep + str(sdk / 'lib') + os.pathsep + env.get('PATH', '')
env['PYTHONIOENCODING'] = 'utf-8'
env['PYTHONUNBUFFERED'] = '1'
env['PEPPER_QI_URL'] = 'tcp://' + config['pepper_ip'] + ':9559'
stop_file = root / '.runtime' / 'voice-stop'
stop_file.unlink(missing_ok=True)
env['PEPPER_STOP_FILE'] = str(stop_file)
project = root / 'suiyue' / 'pepper'
command = [sys.executable, '-u', str(project / 'gateway.py'), '--audio-only', '--allow-gestures',
           '--auto-bridge', '--py2-exe', config['python2'],
           '--cloud-ws', config['brain_ws'], '--device-id', config['device_id']]
if args.test_text:
    command.extend(['--test-text', args.test_text])
with (runtime / 'voice-gateway.log').open('ab') as log:
    child = subprocess.Popen(command, cwd=str(project), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for line in iter(child.stdout.readline, b''):
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            log.write(line)
            log.flush()
        code = child.wait()
    except KeyboardInterrupt:
        stop_file.touch()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait()
        code = 130
raise SystemExit(code)
