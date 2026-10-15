"""Real two-listener HTTP/restart smoke test. Temporary data, worker disabled."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
import uuid

import httpx

ORIGIN='https://recipes.example.invalid'


def main():
    project=Path(__file__).resolve().parent.parent
    with socket.socket() as one,socket.socket() as two:
        one.bind(('127.0.0.1',0));two.bind(('127.0.0.1',0))
        lan_port,web_port=one.getsockname()[1],two.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='erecipe-api-smoke-') as directory:
        command=[sys.executable,'-m','server','--base-url',f'http://192.0.2.1:{lan_port}',
                 '--host','127.0.0.1','--port',str(lan_port),'--serve-port',str(web_port),
                 '--app-origin',ORIGIN,'--no-worker','--data-dir',directory]
        process=None
        def start():
            process=subprocess.Popen(command,cwd=project,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                                     env={**os.environ, 'ERECIPE_TRMNL_API_KEY': ''})
            for _ in range(100):
                if process.poll() is not None:raise RuntimeError(process.stderr.read().decode())
                try:
                    if httpx.get(f'http://127.0.0.1:{lan_port}/health',timeout=1).status_code==200:return process
                except httpx.ConnectError:pass
                time.sleep(.05)
            process.terminate();process.wait(timeout=10)
            raise RuntimeError('Server startup timed out')
        def stop():
            if process and process.poll() is None:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait();raise
        try:
            process=start()
            with httpx.Client(base_url=f'http://127.0.0.1:{web_port}',headers={'Host':urlsplit(ORIGIN).netloc},timeout=5) as web:
                controls={'Origin':ORIGIN,'X-eRecipe-Control':'1','Idempotency-Key':str(uuid.uuid4())}
                result=web.post('/control/v1/users',json={'display_name':'Smoke test'},headers=controls);result.raise_for_status();user=result.json()['id']
                result=web.post(f'/control/v1/users/{user}/enrollments',json={'device_label':'Synthetic phone'},headers=controls);result.raise_for_status()
                assert result.json()['qr_svg'].startswith('<svg')
                code=parse_qs(urlsplit(result.json()['enrollment_url']).query)['code'][0]
                result=web.post('/api/v1/enrollments/redeem',json={'code':code});result.raise_for_status()
                auth={'Authorization':'Bearer '+result.json()['access_token'],'Idempotency-Key':str(uuid.uuid4())}
                assert web.post('/api/v1/enrollments/redeem',json={'code':code}).status_code==410
                body={'kind':'text','text':'Smoke test recipe: stir ingredients and serve.'}
                result=web.post('/api/v1/imports',json=body,headers=auth);result.raise_for_status();accepted=result.json()
                assert web.get('/api/v1/jobs/'+accepted['job_id'],headers=auth).json()['status']=='queued'
                assert web.get('/api/v1/status',headers=auth).json()['readiness']['worker']=='unavailable'
                assert web.get('/admin').status_code==200
                assert web.get('/api/setup').status_code==404
                assert web.post('/control/v1/users',json={'display_name':'Denied'}).status_code==403
                with httpx.Client(base_url=f'http://127.0.0.1:{lan_port}',timeout=5) as lan:
                    assert lan.get('/control/v1/users',headers={'Host':urlsplit(ORIGIN).netloc}).status_code==404
                    assert lan.get('/api/v1/status',headers=auth).status_code==404
                    assert lan.get('/api/setup',headers={'ID':'02:00:00:00:00:01'}).status_code==200
                duplicate=subprocess.run(command,cwd=project,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
                assert duplicate.returncode!=0 and b'Another server' in duplicate.stderr
                stop();process=start()
                replay=web.post('/api/v1/imports',json=body,headers=auth)
                assert replay.status_code==202 and replay.json()==accepted and replay.headers['Idempotency-Replayed']=='true'
                assert web.get('/api/v1/device',headers=auth).json()['paired']
                print(json.dumps({'kind':'isolated-loopback-with-simulated-serve-host','checks':[
                    'two listener startup','dashboard without Tailscale identity','single-use QR exchange','authenticated durable import',
                    'Serve/LAN route separation','browser Origin protection','process lock','restart token authentication',
                    'restart idempotency replay','persisted device pairing'],'processing':'disabled; no provider calls',
                    'tailscale_https_and_ios':'not exercised'},indent=2))
        finally:stop()


if __name__=='__main__':main()
