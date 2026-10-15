"""HTTP adapters on the dedicated Serve ingress; no proxy header grants access."""
import hashlib
import logging
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit
import uuid

from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from server.app import create_app
from server.recipes.db import Problem, digest
from .contract import DOCUMENT, OPERATIONS, LIMITS, deref, validate, validate_named
from .errors import ApiError
from .uploads import load_json, multipart

ROOT=Path(__file__).resolve().parent


class WholeAssetResponse(FileResponse):
    """The v1 asset contract offers full responses and conditional GET, not ranges."""
    async def __call__(self,scope,receive,send):
        scope={**scope,'headers':[(k,v) for k,v in scope['headers'] if k.lower() not in (b'range',b'if-range')]}
        await super().__call__(scope,receive,send)


def application_origin(value):
    if not value:return ''
    url=urlsplit(value)
    try:port=url.port
    except ValueError:raise ValueError('Application origin has an invalid port')
    if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('','/'):
        raise ValueError('Application origin must be an HTTPS origin without credentials, path, query or fragment')
    if any(c.isspace() for c in value) or '%' in url.netloc:
        raise ValueError('Application origin has an invalid hostname')
    host=url.hostname.encode('idna').decode().lower()
    if ':' in host:host='['+host+']'
    return 'https://'+host+(f':{port}' if port and port!=443 else '')


class Ingress:
    def __init__(self,origin):self.origin=origin

    def __call__(self,request,controls=True):
        # The app object is mounted only on a dedicated loopback socket. Do not
        # enable Uvicorn proxy_headers: socket peer must remain observable.
        peer=request.client.host if request.client else ''
        if peer not in ('127.0.0.1','::1','testclient'):
            raise ApiError('forbidden','Application listener only accepts its local Serve proxy.',403)
        if not self.origin:raise ApiError('service_unavailable','Configure the Tailscale Serve HTTPS origin.',503)
        hosts=request.headers.getlist('host');origins=request.headers.getlist('origin')
        if len(hosts)!=1 or hosts[0].lower()!=urlsplit(self.origin).netloc:
            raise ApiError('forbidden','Host does not match the configured application origin.',403)
        if len(origins)>1 or (origins and origins[0]!=self.origin):
            raise ApiError('forbidden','Cross-origin requests are disabled.',403)
        if controls and request.method not in ('GET','HEAD'):
            if origins!=[self.origin] or request.headers.getlist('x-erecipe-control')!=['1']:
                raise ApiError('forbidden','Dashboard changes require the same-origin control header.',403)


class RateLimiter:
    def __init__(self):self.entries=defaultdict(deque);self.lock=Lock()
    def redemption(self,request):
        # Identity is optional rate-limit metadata, never authentication. Tagged
        # devices fall back to the local proxy peer and still enroll successfully.
        source=request.headers.get('tailscale-user-login') or request.client.host
        source=digest(source.encode())
        stamp=time.monotonic()
        with self.lock:
            for key,limit in [('all',100),(source,20)]:
                queue=self.entries[key]
                while queue and queue[0]<=stamp-60:queue.popleft()
                if len(queue)>=limit:
                    raise ApiError('rate_limited','Too many enrollment attempts. Retry later.',429,retryable=True,headers={'Retry-After':str(max(1,int(61-(stamp-queue[0]))))})
            for key in ('all',source):self.entries[key].append(stamp)
            for key in list(self.entries):
                if self.entries[key] and self.entries[key][-1]<stamp-60:del self.entries[key]


def error_response(request,error):
    if not isinstance(error,ApiError):
        code=getattr(error,'code',None) or ('not_found' if error.status==404 else 'revision_conflict' if error.status==409 else 'invalid_request')
        error=ApiError(code,str(error),error.status,retryable=getattr(error,'retryable',False),details=getattr(error,'details',{}))
    return JSONResponse({'error':error.detail(),'request_id':request.state.request_id},status_code=error.status,
                        headers={'Cache-Control':'no-store','X-Request-ID':request.state.request_id,**error.headers})


def create_web_app(service):
    ingress=Ingress(service.origin)
    app=create_app(service.db.directory,service.store.base_url,store=service.store,control_guard=ingress,service=service)
    app.state.service=service
    # Publish the reviewed document, not a second generated wire contract.
    app.router.routes=[r for r in app.router.routes if getattr(r,'path','') not in ('/openapi.json','/docs','/docs/oauth2-redirect','/redoc')]
    app.router.redirect_slashes=False
    limiter=RateLimiter()

    @app.exception_handler(Problem)
    async def domain_error(request,error):return error_response(request,error)

    @app.exception_handler(HTTPException)
    async def http_error(request,error):
        code={404:'not_found',405:'method_not_allowed',403:'forbidden',401:'invalid_credentials'}.get(error.status_code,'invalid_request')
        message={404:'Resource not found.',405:'Method not allowed.'}.get(error.status_code,str(error.detail))
        return error_response(request,ApiError(code,message,error.status_code,headers=error.headers))

    @app.middleware('http')
    async def boundary(request,call_next):
        request.state.request_id=uuid.uuid4().hex
        try:
            ingress(request,controls=request.url.path=='/' or request.url.path.startswith(('/control/','/admin','/openapi.json')))
            if request.url.path in ('/api/setup','/api/display','/api/log') or request.url.path.startswith('/images/'):
                raise ApiError('not_found','Firmware routes use the reader LAN origin.',404)
            if request.method in ('POST','PUT','PATCH') and request.headers.get('content-encoding'):
                raise ApiError('unsupported_media_type','Encoded request bodies are not accepted.',415)
            response=await call_next(request)
        except Problem as error:response=error_response(request,error)
        except Exception as error:
            logging.getLogger(__name__).error('Application request failed (%s); request_id=%s',type(error).__name__,request.state.request_id)
            response=error_response(request,ApiError('internal_error','The server could not complete this request.',500,retryable=True))
        response.headers['X-Request-ID']=request.state.request_id
        response.headers.setdefault('Cache-Control','no-store')
        response.headers['X-Content-Type-Options']='nosniff'
        return response

    @app.get('/admin',include_in_schema=False)
    async def admin():
        return HTMLResponse((ROOT/'admin.html').read_text(),headers={'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'"})

    @app.get('/admin.js',include_in_schema=False)
    async def admin_script():return FileResponse(ROOT/'admin.js',media_type='text/javascript')

    @app.get('/openapi.json',include_in_schema=False)
    async def openapi():return JSONResponse(DOCUMENT)

    async def authenticate(request):
        values=request.headers.getlist('authorization')
        if len(values)!=1 or not re.fullmatch(r'(?i:Bearer) [A-Za-z0-9_-]{43}',values[0]):
            raise ApiError('invalid_credentials','Supply an application bearer token.',401,headers={'WWW-Authenticate':'Bearer'})
        return await run_in_threadpool(service.authenticate,values[0].split(' ',1)[1])

    async def body_bytes(request,limit):
        lengths=request.headers.getlist('content-length')
        if len(lengths)>1:raise ApiError('invalid_request','Duplicate Content-Length.',400)
        if lengths:
            try:length=int(lengths[0])
            except ValueError:raise ApiError('invalid_request','Invalid Content-Length.',400)
            if length<0:raise ApiError('invalid_request','Invalid Content-Length.',400)
            if length>limit:raise ApiError('payload_too_large','Request exceeds the body limit.',413)
        data=bytearray()
        async for chunk in request.stream():
            if len(data)+len(chunk)>limit:raise ApiError('payload_too_large','Request exceeds the body limit.',413)
            data.extend(chunk)
        return bytes(data)

    def endpoint(operation):
        path,method,definition=OPERATIONS[operation]
        async def handle(request:Request):
            actor=None
            if path.startswith('/control/'):
                ingress(request)
            elif operation=='redeemEnrollment':limiter.redemption(request)
            else:actor=await authenticate(request)
            params=dict(request.path_params);query={};key=None
            for name,value in params.items():validate_named('Id',value)
            allowed_query=set()
            for raw in definition['parameters']:
                parameter=deref(raw);name=parameter['name'];schema=parameter['schema']
                if parameter['in']=='query':
                    allowed_query.add(name);values=request.query_params.getlist(name)
                    if len(values)>1:raise ApiError('invalid_request','Duplicate query parameter.',400)
                    if values:
                        value=values[0]
                        if schema.get('type')=='integer':
                            try:value=int(value)
                            except ValueError:raise ApiError('invalid_request','Query parameter must be an integer.',400)
                        try:validate(schema,value)
                        except ApiError:
                            if name=='cursor':raise ApiError('invalid_cursor','Cursor is invalid or expired. Restart the list.',400)
                            raise
                        query[name]=value
                    elif 'default' in schema:query[name]=schema['default']
                elif parameter['in']=='header' and parameter.get('required'):
                    values=request.headers.getlist(name)
                    if len(values)!=1:raise ApiError('invalid_request','Missing or duplicate required header: '+name,400)
                    try:validate(schema,values[0])
                    except ApiError:
                        if name=='Idempotency-Key':raise ApiError('invalid_request','Idempotency-Key must be a UUID.',400)
                        raise
                    if name=='Idempotency-Key':key=values[0]
            if set(request.query_params)-allowed_query:raise ApiError('invalid_request','Unknown query parameter.',400)
            if operation=='getAsset':
                return await run_in_threadpool(asset_response,request,params['asset_id'],actor)
            body=None;files=None
            if method=='post':
                types=request.headers.getlist('content-type')
                if len(types)>1:raise ApiError('invalid_request','Duplicate Content-Type.',400)
                content_type=types[0] if types else ''
                if len(content_type)>1024:raise ApiError('invalid_request','Content-Type is too long.',400)
                mime=content_type.split(';',1)[0].lower()
                limit=LIMITS['multipart_body_bytes'] if operation=='createImport' and mime=='multipart/form-data' else LIMITS['json_body_bytes']
                data=await body_bytes(request,limit)
                if 'requestBody' in definition:
                    if operation=='createImport' and mime=='multipart/form-data':
                        body,files=await run_in_threadpool(multipart,content_type,data)
                    elif mime=='application/json':
                        body=await run_in_threadpool(load_json,data)
                        # Field-size failures are 413 rather than framework 422.
                        if isinstance(body,dict) and any(isinstance(body.get(field),str) and len(body[field])>100000 for field in ('text','markdown')):
                            raise ApiError('payload_too_large','Text exceeds the character limit.',413)
                        await run_in_threadpool(validate,definition['requestBody']['content']['application/json']['schema'],body)
                    else:raise ApiError('unsupported_media_type','Use a supported Content-Type.',415)
                elif data and (mime!='application/json' or load_json(data)!={}):
                    raise ApiError('invalid_request','This operation has no request body.',400)
            status,payload,location,replayed=await run_in_threadpool(service.execute,operation,params,query,body,actor,key=key,files=files)
            headers={'Cache-Control':'no-store'}
            if key:headers['Idempotency-Replayed']='true' if replayed else 'false'
            if location:headers['Location']=location
            return Response(status_code=204,headers=headers) if status==204 else JSONResponse(payload,status_code=status,headers=headers)
        return handle

    def asset_response(request,identifier,actor):
        with service.db.connect():
            # Recheck authentication inside the same read snapshot as asset lookup.
            if not service.db.one('SELECT 1 FROM user_tokens t JOIN users u ON u.id=t.user_id WHERE t.id=? AND t.revoked_at IS NULL AND u.disabled_at IS NULL',(actor['credential_id'],)):
                raise ApiError('invalid_credentials','Application credentials are invalid.',401,headers={'WWW-Authenticate':'Bearer'})
            if not re.fullmatch('[a-f0-9]{64}',identifier):raise ApiError('not_found','Asset not found.',404)
            asset=service.views.record('assets',identifier);path=service.db.asset_path(identifier)
            if not path.is_file():raise ApiError('service_unavailable','Asset bytes are unavailable. Restore the source or regenerate the render.',503)
            with path.open('rb') as source:
                if hashlib.file_digest(source,'sha256').hexdigest()!=identifier:
                    raise ApiError('service_unavailable','Asset bytes are damaged. Restore the source or regenerate the render.',503)
            etag='"'+identifier+'"'
            headers={'ETag':etag,'Cache-Control':'private, max-age=31536000, immutable','X-Content-Type-Options':'nosniff'}
            validators=[v.strip().removeprefix('W/') for v in request.headers.get('if-none-match','').split(',')]
            if '*' in validators or etag in validators:return Response(status_code=304,headers=headers)
            inline=asset['media_type'] in ('image/png','image/jpeg')
            headers['Content-Disposition']=('inline' if inline else 'attachment')+'; filename="'+identifier+('"' if inline else '.bin"')
            if not inline:headers['Content-Security-Policy']="sandbox; default-src 'none'"
            headers['Accept-Ranges']='none'
            return WholeAssetResponse(path,media_type=asset['media_type'],headers=headers)

    for operation,(path,method,definition) in OPERATIONS.items():
        app.add_api_route(path,endpoint(operation),methods=[method.upper()],operation_id=operation,include_in_schema=False)
    return app
