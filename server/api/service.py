"""Versioned commands and queries, composed over the durable processing domain."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import secrets
from urllib.parse import urlencode

import qrcode
import qrcode.image.svg

from server.recipes.db import digest, dumps, now, uid
from server.recipes.processing import ProcessingService
from server.recipes import renderer
from .contract import DOCUMENT, LIMITS
from .errors import ApiError
from .views import Views, PROFILE


class Service:
    def __init__(self,library,store,origin,processing_config):
        self.db,self.store,self.origin,self.config=library,store,origin,processing_config
        self.views=Views(library)
        self.processing=ProcessingService(library)
        self.worker_ready=False

    def authenticate(self,token):
        item=self.db.one('''SELECT u.id,u.display_name,t.id credential_id FROM user_tokens t JOIN users u ON u.id=t.user_id
            WHERE t.token_hash=? AND t.revoked_at IS NULL AND u.disabled_at IS NULL''',(digest(token.encode()),))
        if not item:raise ApiError('invalid_credentials','Application credentials are invalid.',401,headers={'WWW-Authenticate':'Bearer'})
        return item

    def active(self,recipe_id,expected=None):
        item=self.views.record('recipes',recipe_id)
        if item['archived_at']:raise ApiError('recipe_archived','This recipe is archived.',409)
        if expected is not None and item['lock_version']!=expected:
            raise ApiError('revision_conflict','Recipe changed. Reload and compare your edits before saving.',409,
                           details={'current_revision_id':item['current_revision_id'],'lock_version':item['lock_version']})
        return item

    @staticmethod
    def name(value):
        if not value.strip() or '\n' in value or '\r' in value:
            raise ApiError('invalid_request','Names must be nonblank and fit on one line.')
        return value

    def execute(self,operation,params,query,body,actor,*,key=None,files=None):
        read=operation in {'dashboardRecipes','getStatus','listRecipes','getRecipe','listRevisions','getRevision','listRecipeJobs','getJob',
            'listAttempts','listLlmCalls','getLlmCall','listSourceCaptures','getSourceCapture','listRenderSets','getRenderSet',
            'getDevice','listUsers','getUser','listCredentials','getEnrollment'}
        # Always acquire store before SQLite; firmware requests use the same order.
        # This also keeps mutable in-memory telemetry coherent with publication.
        with self.store.lock:
            previous=deepcopy(self.store.state) if not read else None
            try:
                with self.db.connect(write=not read):
                    if actor:
                        current=self.db.one('''SELECT 1 FROM user_tokens t JOIN users u ON u.id=t.user_id
                            WHERE t.id=? AND t.revoked_at IS NULL AND u.disabled_at IS NULL''',(actor['credential_id'],))
                        if not current:raise ApiError('invalid_credentials','Application credentials are invalid.',401,headers={'WWW-Authenticate':'Bearer'})
                    namespace='user:'+actor['id'] if actor else 'dashboard-control'
                    fingerprint=digest(dumps([operation,params,body,[(name,digest(data),mime) for name,data,mime in files or []]]).encode())
                    if key:
                        receipt=self.db.one('SELECT * FROM api_receipts WHERE namespace=? AND key=?',(namespace,key))
                        if receipt:
                            if receipt['fingerprint']!=fingerprint:raise ApiError('idempotency_conflict','This key was used for a different request.',409)
                            return receipt['status'],json.loads(receipt['body_json']),receipt['location'],True
                    result=self.read(operation,params,query,actor) if read else self.command(operation,params,body,actor,files)
                    status,payload,location=result
                    if key:
                        self.db.execute('INSERT INTO api_receipts VALUES(?,?,?,?,?,?,?)',
                                        (namespace,key,fingerprint,status,dumps(payload),location,now()))
                    return status,payload,location,False
            except BaseException:
                if previous is not None:self.store.state=previous
                raise

    def ensure_render(self,revision_id,user):
        revision=self.views.record('recipe_revisions',revision_id)
        self.active(revision['recipe_id'])
        cache=renderer.cache_key(revision,renderer.profile())
        result=self.db.one('SELECT * FROM render_sets WHERE cache_key=?',(cache,))
        if result and self.views.complete(result):
            pages=self.db.all('SELECT asset_id FROM render_pages WHERE render_set_id=? ORDER BY page_number',(result['id'],))
            if all(digest(self.db.asset_bytes(p['asset_id']))==p['asset_id'] for p in pages):
                return dict(status='ready',revision_id=revision_id,job_id=None,render_set_id=result['id'])
        job=self.db.one("SELECT * FROM jobs WHERE input_revision_id=? AND kind='render' AND status IN ('queued','running') ORDER BY created_at,id LIMIT 1",(revision_id,))
        if not job:job=self.processing.render(revision['recipe_id'],revision_id,user=user,key='api:'+uid())
        return dict(status=job['status'],revision_id=revision_id,job_id=job['id'],render_set_id=None)

    def cancel_active(self,jobs):
        for job in jobs:
            if job['status'] in ('queued','running'):
                self.db.cancel(job['id'])
                if job['kind']=='capture':
                    self.db.execute('UPDATE source_captures SET error_json=? WHERE id=?',
                        (dumps(dict(code='capture_cancelled',message='Source capture was cancelled.',retryable=False,details={})),job['source_capture_id']))

    def command(self,op,p,b,actor,files):
        v=self.views; user=actor['id'] if actor else 'local'
        if op=='sendDashboardRecipe':
            recipe=self.active(p['recipe_id'])
            render=v.record('render_sets',b['render_set_id'])
            if render['revision_id']!=recipe['current_revision_id']:
                raise ApiError('revision_conflict','This recipe changed. Refresh the library before sending it.',409)
            return self.command('createPublication',{},b,actor,None)
        if op=='createUser':
            identifier=uid();self.db.execute('INSERT INTO users VALUES(?,?,?,NULL)',(identifier,self.name(b['display_name']),now()))
            return 201,v.admin_user(v.record('users',identifier)),'/control/v1/users/'+identifier
        if op=='disableUser':
            item=v.record('users',p['user_id']);stamp=now()
            if not item['disabled_at']:
                self.db.execute('UPDATE users SET disabled_at=? WHERE id=?',(stamp,item['id']))
                self.db.execute('UPDATE user_tokens SET revoked_at=COALESCE(revoked_at,?) WHERE user_id=?',(stamp,item['id']))
                self.db.execute('UPDATE enrollments SET revoked_at=COALESCE(revoked_at,?) WHERE user_id=? AND credential_id IS NULL',(stamp,item['id']))
            return 200,v.admin_user(v.record('users',item['id'])),None
        if op in ('revokeCredential','revokeCurrentCredential'):
            identifier=actor['credential_id'] if op=='revokeCurrentCredential' else p['credential_id']
            v.record('user_tokens',identifier)
            self.db.execute('UPDATE user_tokens SET revoked_at=COALESCE(revoked_at,?) WHERE id=?',(now(),identifier))
            return (204,None,None) if op=='revokeCurrentCredential' else (200,v.credential(v.record('user_tokens',identifier)),None)
        if op=='createEnrollment':
            if not self.origin:raise ApiError('enrollment_origin_unconfigured','Configure the application HTTPS origin.',409)
            account=v.record('users',p['user_id'])
            if account['disabled_at']:raise ApiError('user_disabled','This account is disabled.',409)
            label=self.name(b['device_label']);stamp=now();identifier=uid();code=secrets.token_urlsafe(32)
            expiry=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat(timespec='milliseconds')
            self.db.execute('UPDATE enrollments SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL AND credential_id IS NULL',(stamp,account['id']))
            self.db.execute('INSERT INTO enrollments VALUES(?,?,?,?,?,?,NULL,NULL)',(identifier,account['id'],label,digest(code.encode()),stamp,expiry))
            link='erecipe://enroll?'+urlencode(dict(v='1',server=self.origin,code=code))
            qr=qrcode.make(link,image_factory=qrcode.image.svg.SvgPathImage,border=4)
            return 201,dict(enrollment=v.enrollment(v.record('enrollments',identifier)),enrollment_url=link,qr_svg=qr.to_string().decode()),'/control/v1/enrollments/'+identifier
        if op=='redeemEnrollment':
            code_hash=digest(b['code'].encode())
            item=self.db.one('SELECT * FROM enrollments WHERE code_hash=?',(code_hash,))
            if not item:raise ApiError('enrollment_invalid','Enrollment code is invalid.',401)
            if not secrets.compare_digest(item['code_hash'],code_hash):raise ApiError('enrollment_invalid','Enrollment code is invalid.',401)
            account=v.record('users',item['user_id'])
            if item['revoked_at'] or item['credential_id'] or item['expires_at']<=now() or account['disabled_at']:
                raise ApiError('enrollment_unavailable','Enrollment is expired or already used. Request a new QR code.',410)
            identifier=uid();token=secrets.token_urlsafe(32)
            self.db.execute('INSERT INTO user_tokens VALUES(?,?,?,?,?,NULL)',(identifier,item['user_id'],digest(token.encode()),item['device_label'],now()))
            self.db.execute('UPDATE enrollments SET credential_id=? WHERE id=?',(identifier,item['id']))
            return 201,dict(user=v.user(item['user_id']),credential=v.credential(v.record('user_tokens',identifier)),access_token=token,token_type='Bearer',server_id=self.db.server_id),'/api/v1/status'
        if op=='revokeEnrollment':
            item=v.record('enrollments',p['enrollment_id'])
            if item['credential_id']:raise ApiError('enrollment_already_redeemed','Revoke the linked credential instead.',409)
            if v.enrollment(item)['state']=='pending':self.db.execute('UPDATE enrollments SET revoked_at=? WHERE id=?',(now(),item['id']))
            return 200,v.enrollment(v.record('enrollments',item['id'])),None
        if op=='createImport':
            if b['kind']=='url':
                job=self.processing.submit_url(b['url'],user=user,key='api:'+uid())
            elif b['kind']=='text':
                job=self.processing.submit_text(b['text'],user=user,key='api:'+uid())
            elif b['kind']=='pdf':
                filename,data,_=files[0]
                job=self.processing.submit_pdf(filename,data,user=user,key='api:'+uid())
            else:
                assets=[('original_image',i,self.db.asset(data,mime),filename) for i,(filename,data,mime) in enumerate(files)]
                job=self.db.submit(user,'api:'+uid(),'images',digest(dumps([(a[2],a[3]) for a in assets]).encode()),assets)
            return 202,dict(status='accepted',recipe_id=job['recipe_id'],source_capture_id=job['source_capture_id'],job_id=job['id']),'/api/v1/jobs/'+job['id']
        if op=='saveRevision':
            recipe=self.active(p['recipe_id'],b['expected_lock_version'])
            if not recipe['current_revision_id']:raise ApiError('revision_unavailable','Recipe has no current revision yet.',409)
            result=self.processing.save_markdown(recipe['id'],b['markdown'],expected_lock_version=b['expected_lock_version'],user=user)
            return 201,dict(recipe_id=recipe['id'],revision_id=result['revision_id'],lock_version=recipe['lock_version']+1,render_job_id=result['job_id']),'/api/v1/revisions/'+result['revision_id']
        if op=='acceptCandidate':
            recipe=self.active(p['recipe_id'],b['expected_lock_version']);revision=v.record('recipe_revisions',b['revision_id'])
            if revision['recipe_id']!=recipe['id']:raise ApiError('not_found','Revision not found in this recipe.',404)
            if not v.candidate(revision):raise ApiError('candidate_not_pending','This revision is not a pending candidate.',409)
            self.db.accept(recipe['id'],revision['id'],b['expected_lock_version'])
            result=self.ensure_render(revision['id'],user)
            return 200,dict(recipe_id=recipe['id'],revision_id=revision['id'],lock_version=recipe['lock_version']+1,render_job_id=result['job_id'],render_set_id=result['render_set_id']),None
        if op=='archiveRecipe':
            recipe=self.active(p['recipe_id'],b['expected_lock_version']);stamp=now()
            self.db.execute('UPDATE recipes SET archived_at=?,updated_at=?,lock_version=lock_version+1 WHERE id=?',(stamp,stamp,recipe['id']))
            self.cancel_active(self.db.all('SELECT * FROM jobs WHERE recipe_id=?',(recipe['id'],)))
            return 200,dict(recipe_id=recipe['id'],lock_version=recipe['lock_version']+1,archived_at=stamp),None
        if op in ('extractRecipe','refreshRecipe'):
            self.active(p['recipe_id'])
            capture=v.record('source_captures',b['source_capture_id'])
            if capture['recipe_id']!=p['recipe_id']:raise ApiError('not_found','Capture not found in this recipe.',404)
            if op=='refreshRecipe':
                if capture['kind']!='url':raise ApiError('refresh_unsupported','Only URL sources can be fetched again.',422)
                job=self.processing.refresh(p['recipe_id'],capture['id'],user=user,key='api:'+uid())
                return 202,dict(recipe_id=p['recipe_id'],job_id=job['id'],source_capture_id=job['source_capture_id'],revision_id=None),'/api/v1/jobs/'+job['id']
            if capture['state']!='complete':raise ApiError('capture_incomplete','Source capture is not complete.',409)
            job=self.processing.reextract(p['recipe_id'],capture['id'],user=user,key='api:'+uid())
            return 202,dict(recipe_id=p['recipe_id'],job_id=job['id'],source_capture_id=capture['id'],revision_id=None),'/api/v1/jobs/'+job['id']
        if op=='ensureRender':
            result=self.ensure_render(p['revision_id'],user)
            return (200 if result['status']=='ready' else 202),result,('/api/v1/jobs/'+result['job_id']) if result['job_id'] else None
        if op in ('retryJob','cancelJob'):
            job=v.record('jobs',p['job_id']);self.active(job['recipe_id'])
            if op=='retryJob':
                if not v.job(job)['retry_allowed']:raise ApiError('job_not_retryable','This stage cannot be retried. Request fresh processing if needed.',409)
                self.db.retry(job['id'])
            else:
                tree=v.tree(job['id'])
                if not any(j['status'] in ('queued','running') for j in tree) and job['status']!='cancelled':raise ApiError('job_not_cancellable','This job tree has no active work.',409)
                self.cancel_active(tree)
            return (202 if op=='retryJob' else 200),v.job(v.record('jobs',job['id'])),('/api/v1/jobs/'+job['id']) if op=='retryJob' else None
        if op=='createPublication':
            render=v.record('render_sets',b['render_set_id']);revision=v.record('recipe_revisions',render['revision_id']);self.active(revision['recipe_id'])
            if not self.store.state['device_id']:raise ApiError('device_not_paired','Pair the reader before sending a recipe.',409)
            v.render(render)
            page=self.db.one('SELECT asset_id FROM render_pages WHERE render_set_id=? AND page_number=?',(render['id'],b['page_number']))
            if not page:raise ApiError('page_not_found','Page does not exist in this render set.',422)
            data=self.db.asset_bytes(page['asset_id'])
            if digest(data)!=page['asset_id']:raise ApiError('render_unavailable','Render asset is damaged. Request a render to repair it.',409)
            descriptor=self.store.asset(data,'RECIPE',revision['title'])
            descriptor.update(render_set_id=render['id'],page_number=b['page_number'],asset_id=page['asset_id'],requested_by=user)
            self.store.set_mode('soak')
            published=self.store.publish(descriptor,'explicit-send')
            return 201,dict(status='selected',publication=v.publication(v.record('publications',published['id']))),'/api/v1/device'
        raise RuntimeError('Unimplemented command: '+op)

    def read(self,op,p,q,actor):
        v=self.views
        if op=='dashboardRecipes':
            def summary(item):
                recipe=v.recipe(item)
                render=v.current_render(item['current_revision_id'])
                recipe['page_count']=render['page_count'] if render else 0
                return recipe
            return 200,v.page('SELECT * FROM recipes WHERE archived_at IS NULL',(),summary,q,
                              ['dashboardRecipes'],order='created_at'),None
        if op=='getStatus':
            configured=self.config.readiness()['configured']
            result=dict(api_version='1',contract_version=DOCUMENT['info']['version'],server_id=self.db.server_id,user=v.user(actor['id']),
                capabilities=dict(import_kinds=['text','images','url','pdf'],source_refresh=True,publication=True,archive=True,render_profile_ids=[PROFILE['id']],limits=LIMITS),
                readiness=dict(worker='ready' if self.worker_ready else 'unavailable',extraction='ready' if configured else 'not_configured',rendering='ready',
                               issues=[] if configured else [dict(code='configuration_missing',message=self.config.configuration_issue,retryable=True,
                                                                 details=dict(provider=self.config.provider,model=self.config.model))]))
            return 200,result,None
        if op=='getDevice':return 200,self.device(),None
        direct={'getUser':('users','user_id',v.admin_user),'getEnrollment':('enrollments','enrollment_id',v.enrollment),
            'getRecipe':('recipes','recipe_id',lambda x:v.recipe(x,True)),'getRevision':('recipe_revisions','revision_id',lambda x:v.revision(x,True)),
            'getJob':('jobs','job_id',v.job),'getLlmCall':('llm_calls','call_id',v.call),'getSourceCapture':('source_captures','capture_id',v.capture),
            'getRenderSet':('render_sets','render_set_id',v.render)}
        if op in direct:
            table,key,mapper=direct[op];return 200,mapper(v.record(table,p[key])),None
        args=();order='created_at';asc=False
        if op=='listUsers':sql='SELECT * FROM users';mapper=v.admin_user
        elif op=='listCredentials':
            v.record('users',p['user_id']);sql='SELECT * FROM user_tokens WHERE user_id=?';args=(p['user_id'],);mapper=v.credential
        elif op=='listRecipes':
            visibility=q.get('visibility','active');sql='SELECT * FROM recipes'
            if visibility!='all':sql+=' WHERE archived_at IS '+('NOT NULL' if visibility=='archived' else 'NULL')
            mapper=v.recipe
        elif op in ('listRevisions','listRecipeJobs','listSourceCaptures'):
            v.record('recipes',p['recipe_id']);args=(p['recipe_id'],)
            if op=='listRevisions':
                sql='SELECT * FROM recipe_revisions WHERE recipe_id=?';mapper=v.revision
                if q.get('scope','all')=='candidates':sql+=" AND origin='extraction' AND NOT EXISTS(SELECT 1 FROM revision_acceptances a WHERE a.revision_id=recipe_revisions.id)"
            elif op=='listRecipeJobs':sql='SELECT * FROM jobs WHERE recipe_id=?';mapper=v.job
            else:sql='SELECT * FROM source_captures WHERE recipe_id=?';mapper=v.capture;order='submitted_at'
        elif op=='listRenderSets':
            v.record('recipe_revisions',p['revision_id']);sql='SELECT * FROM render_sets WHERE revision_id=?';args=(p['revision_id'],);mapper=v.render
        elif op=='listAttempts':
            v.record('jobs',p['job_id']);sql='SELECT * FROM job_attempts WHERE job_id=?';args=(p['job_id'],);mapper=v.attempt;order='attempt_number';asc=True
        elif op=='listLlmCalls':
            v.record('job_attempts',p['attempt_id']);sql='SELECT * FROM llm_calls WHERE attempt_id=?';args=(p['attempt_id'],);mapper=v.call;order='sequence';asc=True
        else:raise RuntimeError('Unimplemented query: '+op)
        context=[op,p,{key:value for key,value in q.items() if key!='cursor'},actor['id'] if actor else 'dashboard']
        return 200,v.page(sql,args,mapper,q,context,order,asc),None

    def device(self):
        state=self.store.state;v=self.views
        selected=v.record('publications',state['publication_id'])
        fetch=state.get('last_fetch');evidence=None;selected_at=None
        if fetch:
            known=self.db.one("SELECT asset_descriptor_json FROM publications WHERE json_extract(asset_descriptor_json,'$.version')=? ORDER BY created_at DESC LIMIT 1",(fetch['version'],))
            descriptor=json.loads(known['asset_descriptor_json']) if known else {}
            evidence=dict(asset_id=descriptor.get('asset_id'),legacy_image_version=None if descriptor.get('asset_id') else fetch['version'],fetched_at=fetch['at'])
            if fetch['version']==state['current']['version'] and fetch['at']>=state['published_at']:selected_at=fetch['at']
        return dict(paired=bool(state['device_id']),selected_publication=v.publication(selected),last_checkin_at=state['last_checkin'],last_image_fetch=evidence,selected_asset_fetched_at=selected_at,display_evidence='unavailable',calendar=self.store.calendar_status())
