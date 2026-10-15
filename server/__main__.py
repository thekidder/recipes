"""One process owns the LAN firmware listener, Serve upstream, and worker."""
import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import logging
import os
from pathlib import Path
import signal
import socket
from threading import Thread

import uvicorn
from server.app import ROOT
from server.application import create_apps
from server.recipes.config import ProcessingConfig
from server.calendar import CalendarConfig


class ManagedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield  # Both listeners share the supervisor's signal handlers.


async def serve(lan,web,service,worker,args):
    servers=[ManagedServer(uvicorn.Config(lan,host=args.host,port=args.port,access_log=False,proxy_headers=False)),
             ManagedServer(uvicorn.Config(web,host='127.0.0.1',port=args.serve_port,access_log=False,proxy_headers=False))]
    sockets=[]
    thread=None
    calendar_thread=None
    loop=asyncio.get_running_loop()
    def stop():
        for server in servers:server.should_exit=True
    try:
        # Bind both before starting the worker, so a port conflict cannot leave a
        # half-started service making provider calls in the background.
        for host,port in [(args.host,args.port),('127.0.0.1',args.serve_port)]:
            family=socket.AF_INET6 if ':' in host else socket.AF_INET
            sock=socket.socket(family,socket.SOCK_STREAM)
            sockets.append(sock);sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind((host,port));sock.listen(128);sock.setblocking(False)
        for sig in (signal.SIGINT,signal.SIGTERM):loop.add_signal_handler(sig,stop)
        if service.calendar_worker.config.enabled:
            calendar_thread=Thread(target=service.calendar_worker.run,name='calendar-worker')
            calendar_thread.start()
        if not args.no_worker:
            thread=Thread(target=worker.run,name='recipe-worker')
            thread.start();service.worker_ready=True
        tasks=[asyncio.create_task(server.serve(sockets=[sock])) for server,sock in zip(servers,sockets)]
        await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        stop()
        await asyncio.gather(*tasks)
    finally:
        stop();service.worker_ready=False;worker.stop()
        service.calendar_worker.stop()
        if thread:await asyncio.to_thread(thread.join)
        if calendar_thread:await asyncio.to_thread(calendar_thread.join)
        for sock in sockets:sock.close()
        for sig in (signal.SIGINT,signal.SIGTERM):loop.remove_signal_handler(sig)


def main():
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger('server.recipes').setLevel(logging.INFO)
    p=argparse.ArgumentParser(description='eRecipe server: LAN reader and private Tailscale application API')
    p.add_argument('--base-url',required=True,help='LAN origin reachable by the reader, e.g. http://192.168.1.20:8000')
    p.add_argument('--host',default='0.0.0.0')
    p.add_argument('--port',type=int,default=8000)
    p.add_argument('--app-origin',help='Tailscale Serve HTTPS origin; defaults to ERECIPE_APP_ORIGIN')
    p.add_argument('--serve-port',type=int,default=8001,help='Loopback-only upstream port for Tailscale Serve')
    p.add_argument('--no-worker',action='store_true',help='Accept durable jobs but do not run processing in this process')
    p.add_argument('--poll-seconds',type=int,default=20)
    p.add_argument('--data-dir',type=Path,default=ROOT/'data')
    p.add_argument('--device-id',help='Optional reader MAC allowlist')
    args=p.parse_args()
    if args.port==args.serve_port or not all(1<=port<=65535 for port in (args.port,args.serve_port)):
        p.error('Use distinct ports between 1 and 65535 for LAN and Serve listeners')
    config=ProcessingConfig.from_env()
    origin=args.app_origin if args.app_origin is not None else os.environ.get('ERECIPE_APP_ORIGIN','')
    args.data_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (args.data_dir/'server.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:p.error('Another server is already using this data directory')
        try:lan,web,service,worker=create_apps(args.data_dir,args.base_url,origin,args.poll_seconds,args.device_id,config=config,calendar_config=CalendarConfig.from_env())
        except ValueError as error:p.error(str(error))
        print(f'Reader: {args.base_url}\nApplication: {service.origin or "not configured"}\nServe upstream: http://127.0.0.1:{args.serve_port}\nData: {args.data_dir}')
        try:asyncio.run(serve(lan,web,service,worker,args))
        except OSError as error:p.error(f'Could not bind listeners: {error.strerror}')


if __name__=='__main__':main()
