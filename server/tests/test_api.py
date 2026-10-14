"""API integration and race tests with isolated data and a mocked provider."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
import base64
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from server.application import create_apps
from server.app import create_app
from server.api.contract import DOCUMENT, OPERATIONS, deref, validate
from server.api.library import ApiLibrary
from server.recipes.config import ProcessingConfig
from server.recipes.db import dumps, digest
from server.recipes.document import ExtractedRecipe, to_markdown
from server.recipes.openai import OpenAIExtractor

ORIGIN='https://recipes.example.invalid'
LAN='http://192.168.1.20:8000'
MAC='AA:BB:CC:DD:EE:01'
RECIPE=dict(is_recipe=True,title='Lemon salad',servings='4',prep_time=None,cook_time=None,
    ingredients=[dict(name=None,items=['2 cans chickpeas','2 tbsp lemon juice'])],
    method=[dict(name=None,items=['Mix the chickpeas and lemon juice.','Serve chilled.'])],notes=[],footer=None,warnings=[])
MARKDOWN=to_markdown(ExtractedRecipe.model_validate(RECIPE))


def key():return str(uuid.uuid4())


def checked(response,operation,status=200):
    assert response.status_code==status,response.text
    definition=OPERATIONS[operation][2]
    shape=deref(definition['responses'].get(str(status),definition['responses']['default']))
    if status!=204:
        validate(shape['content']['application/json']['schema'],response.json())
    assert response.headers['X-Request-ID']
    return response.json() if status!=204 else None


class Harness:
    def __init__(self,path):
        self.lan_app,self.web_app,self.service,self.worker=create_apps(path,LAN,ORIGIN,config=ProcessingConfig())
        self.web=TestClient(self.web_app,base_url=ORIGIN)
        self.lan=TestClient(self.lan_app)
        self.calls=[]
        def provider(request):
            self.calls.append(request)
            return httpx.Response(200,json={'id':'response_test','status':'completed','model':'test-model',
                'output':[{'type':'message','content':[{'type':'output_text','text':dumps(RECIPE)}]}],
                'usage':{'input_tokens':15,'output_tokens':30}})
        self.worker.extractor=OpenAIExtractor(ProcessingConfig(api_key='fake-test-provider-secret'),httpx.MockTransport(provider))
        self.user=self.control('/control/v1/users',{'display_name':'Alex'},'createUser',201)
        enrollment=self.enroll()
        self.enrollment=enrollment['enrollment']
        self.code=parse_qs(urlsplit(enrollment['enrollment_url']).query)['code'][0]
        response=self.web.post('/api/v1/enrollments/redeem',json={'code':self.code})
        credentials=checked(response,'redeemEnrollment',201)
        self.token=credentials['access_token'];self.credential=credentials['credential']

    @property
    def db(self):return self.service.db

    def control(self,path,body,op,status=200,idem=None):
        h={'Origin':ORIGIN,'X-eRecipe-Control':'1','Idempotency-Key':idem or key()}
        return checked(self.web.post(path,json=body,headers=h),op,status)

    def enroll(self):return self.control(f"/control/v1/users/{self.user['id']}/enrollments",{'device_label':'Test phone'},'createEnrollment',201)

    def headers(self,idem=None):return {'Authorization':'Bearer '+self.token,'Idempotency-Key':idem or key()}

    def post(self,path,body,op,status=200,idem=None):return checked(self.web.post(path,json=body,headers=self.headers(idem)),op,status)

    def get(self,path,op):return checked(self.web.get(path,headers=self.headers()),op)

    def imported(self,idem=None):return self.post('/api/v1/imports',{'kind':'text','text':'Mix chickpeas and lemon juice.'},'createImport',202,idem)

    def ready(self):
        item=self.imported();assert self.worker.run_once();assert self.worker.run_once()
        return item,self.get('/api/v1/recipes/'+item['recipe_id'],'getRecipe')

    def pair(self):
        result=self.lan.get('/api/setup',headers={'ID':MAC}).json()
        self.firmware_auth={'ID':MAC,'Access-Token':result['api_key']}


@pytest.fixture
def api(tmp_path):
    h=Harness(tmp_path)
    yield h
    h.web.close();h.lan.close()


def test_enrollment_hashes_and_reuse(api):
    row=api.db.one('SELECT * FROM enrollments WHERE id=?',(api.enrollment['id'],))
    assert row['code_hash']==digest(api.code.encode()) and row['credential_id']==api.credential['id']
    assert api.db.one('SELECT token_hash FROM user_tokens WHERE id=?',(api.credential['id'],))['token_hash']==digest(api.token.encode())
    checked(api.web.post('/api/v1/enrollments/redeem',json={'code':api.code}),'redeemEnrollment',410)
    status=checked(api.web.get('/control/v1/enrollments/'+api.enrollment['id']),'getEnrollment')
    assert status['credential_id']==api.credential['id'] and 'code' not in status
    events=(api.db.directory/'events.jsonl').read_text()
    assert api.token not in events and api.code not in events
    assert api.db.all("SELECT * FROM api_receipts WHERE body_json LIKE '%access_token%'")==[]
    assert '/admin' in api.web.get('/').text
    assert api.web.get('/admin').status_code==200
    assert 'script-src' in api.web.get('/admin').headers['Content-Security-Policy']


def test_enrollment_replacement_expiry_race_and_revoke(api):
    first=api.enroll();second=api.enroll()
    oldcode=parse_qs(urlsplit(first['enrollment_url']).query)['code'][0]
    checked(api.web.post('/api/v1/enrollments/redeem',json={'code':oldcode}),'redeemEnrollment',410)
    newcode=parse_qs(urlsplit(second['enrollment_url']).query)['code'][0]
    def redeem(_):return api.web.post('/api/v1/enrollments/redeem',json={'code':newcode}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(redeem,range(2)))==[201,410]
    assert api.db.one('SELECT count(*) n FROM user_tokens WHERE user_id=?',(api.user['id'],))['n']==2
    expired=api.enroll();expiredcode=parse_qs(urlsplit(expired['enrollment_url']).query)['code'][0]
    api.db.execute("UPDATE enrollments SET expires_at='2000-01-01T00:00:00.000+00:00' WHERE id=?",(expired['enrollment']['id'],))
    checked(api.web.post('/api/v1/enrollments/redeem',json={'code':expiredcode}),'redeemEnrollment',410)
    pending=api.enroll()
    revoked=api.control('/control/v1/enrollments/'+pending['enrollment']['id']+'/revoke',{},'revokeEnrollment')
    assert revoked['state']=='revoked'


def test_serve_boundary_has_no_identity_requirement(api):
    checked(api.web.get('/control/v1/users'),'listUsers')  # No login headers or dashboard credentials.
    for headers in [{},{'X-eRecipe-Control':'1'},{'X-eRecipe-Control':'1','Origin':'https://evil.invalid'}]:
        response=api.web.post('/control/v1/users',json={'display_name':'Denied'},headers=headers)
        checked(response,'createUser',403)
    checked(api.web.get('/control/v1/users',headers={'Host':'localhost'}),'listUsers',403)
    with TestClient(api.web_app,base_url=ORIGIN,client=('192.168.1.99',4567)) as remote:
        checked(remote.get('/control/v1/users',headers={'Tailscale-User-Login':'anything','X-Forwarded-For':'127.0.0.1'}),'listUsers',403)
    assert api.lan.post('/control/v1/users',json={'display_name':'Denied'},headers={'Host':urlsplit(ORIGIN).netloc,'X-eRecipe-Control':'1','Origin':ORIGIN}).status_code==404
    assert api.web.get('/api/setup',headers={'ID':MAC}).status_code==404
    checked(api.web.get('/api/v1/status'),'getStatus',401)


def test_import_replay_conflicts_and_restart(api):
    idem=key();first=api.imported(idem);second=api.imported(idem)
    assert first==second and api.db.one('SELECT count(*) n FROM recipes')['n']==1
    bad=api.post('/api/v1/imports',{'kind':'text','text':'Changed'},'createImport',409,idem)
    assert bad['error']['code']=='idempotency_conflict'
    other=Harness(api.db.directory)
    try:
        response=other.web.post('/api/v1/imports',json={'kind':'text','text':'Mix chickpeas and lemon juice.'},headers=api.headers(idem))
        assert checked(response,'createImport',202)==first
        assert response.headers['Idempotency-Replayed']=='true'
    finally:other.web.close();other.lan.close()


def test_full_text_edit_preview_candidate_and_publication(api):
    api.pair();original=api.service.store.state['publication_id']
    imported,recipe=api.ready();rid=recipe['id'];rev=recipe['current_revision'];render_id=recipe['processing']['current_render_set_id']
    assert len(api.calls)==1 and api.service.store.state['publication_id']==original
    job=api.get('/api/v1/jobs/'+imported['job_id'],'getJob')
    assert job['status']=='succeeded' and len(job['downstream_job_ids'])==1
    child=api.get('/api/v1/jobs/'+job['downstream_job_ids'][0],'getJob')
    assert child['result']['render_set_id']==render_id
    captures=api.get(f'/api/v1/recipes/{rid}/source-captures','listSourceCaptures')['items']
    capture=api.get('/api/v1/source-captures/'+captures[0]['id'],'getSourceCapture')
    assert api.web.get(capture['assets'][0]['asset']['href'],headers=api.headers()).text=='Mix chickpeas and lemon juice.'
    attempts=api.get('/api/v1/jobs/'+job['id']+'/attempts','listAttempts')['items']
    calls=api.get(attempts[0]['llm_calls_href'],'listLlmCalls')['items']
    call=api.get('/api/v1/llm-calls/'+calls[0]['id'],'getLlmCall')
    assert call['request_asset'] and call['response_asset'] and call['structured_output_asset']
    render=api.get('/api/v1/render-sets/'+render_id,'getRenderSet')
    preview=api.web.get(render['pages'][0]['asset']['href'],headers=api.headers())
    assert preview.status_code==200 and preview.headers['Cache-Control'].startswith('private')
    with Image.open(BytesIO(preview.content)) as image:assert image.size==(1872,1404) and image.mode=='1'
    assert api.get('/api/v1/device','getDevice')['last_image_fetch'] is None
    saved_key=key()
    body={'expected_lock_version':1,'markdown':rev['markdown'].replace('Lemon salad','Edited salad')}
    saved=api.post(f'/api/v1/recipes/{rid}/revisions',body,'saveRevision',201,saved_key)
    assert api.post(f'/api/v1/recipes/{rid}/revisions',body,'saveRevision',201,saved_key)==saved
    conflict=api.post(f'/api/v1/recipes/{rid}/revisions',body,'saveRevision',409)
    assert conflict['error']['details']=={'current_revision_id':saved['revision_id'],'lock_version':2}
    assert api.worker.run_once() and len(api.calls)==1
    extracted=api.post(f'/api/v1/recipes/{rid}/extract',{'source_capture_id':capture['id']},'extractRecipe',202)
    assert api.worker.run_once() and api.worker.run_once()
    latest=api.get(f'/api/v1/recipes/{rid}','getRecipe')
    assert latest['current_revision_id']==saved['revision_id'] and latest['candidate_count']==1
    candidate=api.get(f'/api/v1/recipes/{rid}/revisions?scope=candidates','listRevisions')['items'][0]
    accepted=api.post(f'/api/v1/recipes/{rid}/accept',{'expected_lock_version':2,'revision_id':candidate['id']},'acceptCandidate')
    assert accepted['lock_version']==3 and accepted['render_set_id']
    assert api.service.store.state['publication_id']==original
    send_key=key();body={'render_set_id':accepted['render_set_id'],'page_number':1}
    publication=api.post('/api/v1/publications',body,'createPublication',201,send_key)['publication']
    assert api.get('/api/v1/device','getDevice')['selected_publication']==publication
    displayed=api.lan.get('/api/display',headers=api.firmware_auth).json()
    assert displayed['image_url'].startswith(LAN)
    image=api.lan.get('/images/'+displayed['filename'])
    assert image.status_code==200
    assert api.get('/api/v1/device','getDevice')['selected_asset_fetched_at']
    # Sending an older revision explicitly is permitted; retrying the old request
    # must not make it the selected image again.
    newer=api.post('/api/v1/publications',{'render_set_id':render_id,'page_number':1},'createPublication',201)['publication']
    replay=api.post('/api/v1/publications',body,'createPublication',201,send_key)['publication']
    assert replay==publication and api.get('/api/v1/device','getDevice')['selected_publication']==newer
    assert api.db.one('PRAGMA foreign_key_check') is None


def test_candidate_history_remains_history_after_edits(api):
    _,recipe=api.ready();rid=recipe['id'];first=recipe['current_revision']['id']
    api.post(f'/api/v1/recipes/{rid}/revisions',{'expected_lock_version':1,'markdown':MARKDOWN.replace('Lemon salad','New title')},'saveRevision',201)
    history=api.get(f'/api/v1/recipes/{rid}/revisions','listRevisions')['items']
    old=next(item for item in history if item['id']==first)
    assert not old['is_current'] and not old['is_candidate']
    error=api.post(f'/api/v1/recipes/{rid}/accept',{'expected_lock_version':2,'revision_id':first},'acceptCandidate',409)
    assert error['error']['code']=='candidate_not_pending'


def test_assets_auth_conditional_and_html_attachment(api):
    html=api.db.asset(b'<script>alert("example")</script>','text/html')
    path='/api/v1/assets/'+html
    response=api.web.get(path,headers=api.headers())
    assert response.headers['Content-Disposition'].startswith('attachment') and response.headers['Content-Security-Policy'].startswith('sandbox')
    etag=response.headers['ETag']
    assert api.web.get(path,headers={**api.headers(),'Range':'bytes=0-3'}).status_code==200
    assert api.web.get(path,headers={**api.headers(),'If-None-Match':'W/'+etag}).status_code==304
    assert api.web.get(path,headers={'If-None-Match':etag}).status_code==401
    api.control('/control/v1/credentials/'+api.credential['id']+'/revoke',{},'revokeCredential')
    assert api.web.get(path,headers={**api.headers(),'If-None-Match':etag}).status_code==401


def test_render_cache_repair_and_corrupt_asset_conditional(api):
    _,recipe=api.ready();revision=recipe['current_revision_id'];render_id=recipe['processing']['current_render_set_id']
    cached=api.post('/api/v1/revisions/'+revision+'/render',{'profile_id':'trmnl-x-landscape-v1'},'ensureRender')
    assert cached['status']=='ready' and cached['render_set_id']==render_id
    asset=api.get('/api/v1/render-sets/'+render_id,'getRenderSet')['pages'][0]['asset']
    path=api.db.asset_path(asset['id']);path.write_bytes(b'corrupt')
    response=api.web.get(asset['href'],headers={**api.headers(),'If-None-Match':'"'+asset['id']+'"'})
    assert response.status_code==503
    queued=api.post('/api/v1/revisions/'+revision+'/render',{'profile_id':'trmnl-x-landscape-v1'},'ensureRender',202)
    assert api.worker.run_once() and len(api.calls)==1
    assert digest(path.read_bytes())==asset['id']
    assert api.get('/api/v1/jobs/'+queued['job_id'],'getJob')['result']['render_set_id']==render_id


def test_concurrent_edits_and_per_user_idempotency(api):
    _,recipe=api.ready();rid=recipe['id']
    def edit(number):
        return api.web.post('/api/v1/recipes/'+rid+'/revisions',json={'expected_lock_version':1,'markdown':MARKDOWN.replace('Lemon salad','Edit '+str(number))},headers=api.headers()).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(edit,range(2)))==[201,409]
    same_key=key();api.imported(same_key)
    user,token=api.db.create_user('Second reader')
    response=api.web.post('/api/v1/imports',json={'kind':'text','text':'Other input'},headers={'Authorization':'Bearer '+token,'Idempotency-Key':same_key})
    other=checked(response,'createImport',202)
    assert api.get('/api/v1/recipes/'+other['recipe_id'],'getRecipe')['created_by']['id']==user


def test_self_revocation_and_cross_recipe_candidate_rejection(api):
    _,one=api.ready();_,two=api.ready()
    wrong=api.post('/api/v1/recipes/'+one['id']+'/accept',{'expected_lock_version':1,'revision_id':two['current_revision_id']},'acceptCandidate',404)
    assert wrong['error']['code']=='not_found'
    api.post('/api/v1/credentials/current/revoke',{},'revokeCurrentCredential',204)
    checked(api.web.get('/api/v1/status',headers=api.headers()),'getStatus',401)


def test_archive_fences_worker_and_retains_selected_page(api):
    api.pair();_,recipe=api.ready();rid=recipe['id'];current=recipe['processing']['current_render_set_id']
    api.post('/api/v1/publications',{'render_set_id':current,'page_number':1},'createPublication',201)
    selection=api.service.store.state['publication_id']
    saved=api.post(f'/api/v1/recipes/{rid}/revisions',{'expected_lock_version':1,'markdown':MARKDOWN.replace('Lemon salad','Archived')},'saveRevision',201)
    claimed=api.db.claim();assert claimed['id']==saved['render_job_id']
    archive=api.post(f'/api/v1/recipes/{rid}/archive',{'expected_lock_version':2},'archiveRecipe')
    assert archive['lock_version']==3
    from server.recipes.db import Problem
    with pytest.raises(Problem):api.worker.render(claimed)
    assert api.get('/api/v1/jobs/'+claimed['id'],'getJob')['status']=='cancelled'
    assert api.get('/api/v1/recipes','listRecipes')['items']==[]
    assert api.get('/api/v1/recipes?visibility=all','listRecipes')['items'][0]['archived_at']
    assert api.service.store.state['publication_id']==selection
    assert api.post('/api/v1/publications',{'render_set_id':current,'page_number':1},'createPublication',409)['error']['code']=='recipe_archived'


def test_cancel_succeeded_parent_cancels_active_child(api):
    item=api.imported();assert api.worker.run_once()
    parent=api.get('/api/v1/jobs/'+item['job_id'],'getJob');child=parent['downstream_job_ids'][0]
    cancelled=api.post('/api/v1/jobs/'+parent['id']+'/cancel',{},'cancelJob')
    assert cancelled['status']=='succeeded' and not cancelled['cancel_allowed']
    assert api.get('/api/v1/jobs/'+child,'getJob')['status']=='cancelled'
    assert not api.worker.run_once()


def test_retry_missing_credentials_and_disable_user(api):
    from server.recipes.processing import Worker
    item=api.imported();assert Worker(api.db,ProcessingConfig()).run_once()
    failed=api.get('/api/v1/jobs/'+item['job_id'],'getJob')
    assert failed['error']['code']=='configuration_missing' and failed['retry_allowed']
    retried=api.post('/api/v1/jobs/'+item['job_id']+'/retry',{},'retryJob',202)
    assert retried['status']=='queued' and api.worker.run_once()
    api.control('/control/v1/users/'+api.user['id']+'/disable',{},'disableUser')
    checked(api.web.get('/api/v1/status',headers=api.headers()),'getStatus',401)
    assert api.db.one('SELECT disabled_at FROM users WHERE id=?',(api.user['id'],))['disabled_at']


def test_keyset_pagination_bound_ties_and_tampering(api):
    recipes=[api.imported() for _ in range(4)]
    api.db.execute("UPDATE recipes SET created_at='2026-01-01T00:00:00.000+00:00'")
    page=api.get('/api/v1/recipes?limit=2&visibility=all','listRecipes')
    assert page['has_more']
    later=api.imported()
    second=api.get('/api/v1/recipes?limit=2&visibility=all&cursor='+page['next_cursor'],'listRecipes')
    ids=[i['id'] for i in page['items']+second['items']]
    assert len(set(ids))==4 and later['recipe_id'] not in ids and not second['has_more']
    for cursor,limit in [(page['next_cursor']+'x',2),(page['next_cursor'],1)]:
        response=api.web.get(f'/api/v1/recipes?limit={limit}&visibility=all&cursor='+cursor,headers=api.headers())
        assert checked(response,'listRecipes',400)['error']['code']=='invalid_cursor'


def test_atomic_receipt_rolls_back_domain_effect(api):
    api.db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON api_receipts BEGIN SELECT RAISE(ABORT,'test receipt failure'); END")
    response=api.web.post('/api/v1/imports',json={'kind':'text','text':'Saved only if transaction commits'},headers=api.headers())
    checked(response,'createImport',500)
    assert api.db.one('SELECT count(*) n FROM recipes')['n']==0
    assert api.db.one('SELECT count(*) n FROM jobs')['n']==0


def test_publication_failure_restores_memory_and_sql_selection(api):
    api.pair();_,recipe=api.ready();before=deepcopy(api.service.store.state)
    api.db.execute("CREATE TRIGGER reject_send_receipt BEFORE INSERT ON api_receipts BEGIN SELECT RAISE(ABORT,'test receipt failure'); END")
    checked(api.web.post('/api/v1/publications',json={'render_set_id':recipe['processing']['current_render_set_id'],'page_number':1},headers=api.headers()),'createPublication',500)
    assert api.service.store.state['publication_id']==before['publication_id']
    persisted=json.loads(api.db.one('SELECT state_json FROM device_state WHERE id=1')['state_json'])
    assert persisted['publication_id']==before['publication_id']
    assert api.db.one('SELECT count(*) n FROM publications WHERE render_set_id IS NOT NULL')['n']==0


def test_one_time_json_state_migration_and_recipe_refresh(tmp_path):
    stage=TestClient(create_app(tmp_path,LAN));stage.get('/api/setup',headers={'ID':MAC})
    stage.post('/control/publish',json={'fixture':'B'},headers={'X-eRecipe-Control':'1'})
    original=(tmp_path/'state.json').read_bytes();stage.close()
    api=Harness(tmp_path)
    try:
        assert (tmp_path/'state.json.pre-sqlite.bak').read_bytes()==original
        assert api.service.store.state['current']['kind']=='B'
        _,recipe=api.ready()
        publication=api.post('/api/v1/publications',{'render_set_id':recipe['processing']['current_render_set_id'],'page_number':1},'createPublication',201)
        before=api.service.store.state['current'];before_bytes=(tmp_path/'images'/before['filename']).read_bytes()
        refresh=api.web.post('/control/force-refresh',json={},headers={'Origin':ORIGIN,'X-eRecipe-Control':'1'})
        assert refresh.status_code==200,refresh.text
        after=api.service.store.state['current'];after_bytes=(tmp_path/'images'/after['filename']).read_bytes()
        assert after['filename']!=before['filename']
        with Image.open(BytesIO(before_bytes)) as a,Image.open(BytesIO(after_bytes)) as b:assert a.tobytes()==b.tobytes()
        assert (tmp_path/'state.json').read_bytes()==original
        _,_,restored,_=create_apps(tmp_path,LAN,ORIGIN,config=ProcessingConfig())
        assert restored.store.state['current']==after
        assert restored.db.server_id==api.db.server_id
    finally:api.web.close();api.lan.close()


def test_tap_trial_shared_listeners_sqlite_restore_and_recipe_override(api, tmp_path):
    api.pair()
    _,recipe=api.ready()
    body={'render_set_id':recipe['processing']['current_render_set_id'],'page_number':1}
    api.post('/api/v1/publications',body,'createPublication',201)
    previous=deepcopy(api.service.store.state['current'])
    headers={'Origin':ORIGIN,'X-eRecipe-Control':'1'}
    started=api.web.post('/control/tap-trial/start',json={},headers=headers)
    assert started.status_code==200,started.text
    trial=started.json()['tap_trial']
    auth={'Access-Token':api.service.store.state['api_key'],'Update-Source':'EXT1'}
    page_one=api.lan.get('/api/display',headers=auth).json()['filename']
    assert page_one==trial['pages'][0]['filename']
    api.lan.get('/images/'+page_one)
    page_two=api.lan.get('/api/display',headers=auth).json()['filename']
    assert page_two==trial['pages'][1]['filename']
    persisted=json.loads(api.db.one('SELECT state_json FROM device_state WHERE id=1')['state_json'])
    assert persisted['tap_trial']['page']==2
    publication=api.db.one('SELECT reason FROM publications WHERE id=?',(persisted['publication_id'],))
    assert publication['reason']=='tap-trial-advance'
    api.get('/api/v1/device','getDevice')  # Trial selection still satisfies iOS contract.
    _,_,restored,_=create_apps(tmp_path,LAN,ORIGIN,config=ProcessingConfig())
    assert restored.store.state['tap_trial']['page']==2
    restored.store.stop_tap_trial()
    assert restored.store.state['current']==previous
    # Original process still has its active trial; a new phone send replaces it.
    api.post('/api/v1/publications',body,'createPublication',201)
    assert api.service.store.state['tap_trial'] is None
    assert api.service.store.state['current']['kind']=='RECIPE'
    assert api.web.post('/control/tap-trial/stop',json={},headers=headers).json()['current']==api.service.store.state['current']


def test_trial_publication_failure_keeps_page_and_selection_together(api):
    api.pair()
    store=api.service.store
    store.start_tap_trial()
    api.lan.get('/images/'+store.state['current']['filename'])
    before=deepcopy(store.state)
    api.db.execute("CREATE TRIGGER reject_trial BEFORE UPDATE ON publications BEGIN SELECT RAISE(ABORT,'trial failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.trial_checkin({'UPDATE_SOURCE':'EXT1'})
    assert store.state==before
    persisted=json.loads(api.db.one('SELECT state_json FROM device_state WHERE id=1')['state_json'])
    assert persisted==before
    with pytest.raises(sqlite3.IntegrityError):
        store.stop_tap_trial()
    assert store.state==before


def test_limits_errors_unsafe_url_and_rate_limit(api):
    assert api.post('/api/v1/imports',{'kind':'url','url':'http://127.0.0.1/recipe'},'createImport',422)['error']['code']=='unsafe_url'
    response=api.web.post('/api/v1/imports',content=b'{"kind":"text","text":"one","text":"two"}',headers={**api.headers(),'Content-Type':'application/json'})
    checked(response,'createImport',400)
    checked(api.web.post('/api/v1/imports',json={'kind':'text','text':'x'*100001},headers=api.headers()),'createImport',413)
    checked(api.web.post('/api/v1/imports',content=b'x'*(1048576+1),headers={**api.headers(),'Content-Type':'application/json'}),'createImport',413)
    assert api.db.one('SELECT count(*) n FROM recipes')['n']==0
    statuses=[api.web.post('/api/v1/enrollments/redeem',json={'code':'A'*43}) for _ in range(21)]
    assert statuses[-1].status_code==429 and int(statuses[-1].headers['Retry-After'])>0
    assert api.web.get('/api/v1/does-not-exist',headers=api.headers()).json()['error']['code']=='not_found'
    assert api.web.delete('/api/v1/status',headers=api.headers()).json()['error']['code']=='method_not_allowed'


def test_image_manifest_order_and_replay(api):
    assert 'images' in api.get('/api/v1/status','getStatus')['capabilities']['import_kinds']
    api.pair();original=api.service.store.state['publication_id']
    images=[]
    for color in ('red','blue'):
        data=BytesIO();Image.new('RGB',(10,10),color).save(data,format='PNG');images.append(data.getvalue())
    metadata={'kind':'images','attachments':[{'part_name':'image_2','filename':'second.png'},{'part_name':'image_1','filename':'first.png'}]}
    parts=[('metadata',(None,json.dumps(metadata),'application/json')),('image_1',('first.png',images[0],'image/png')),('image_2',('second.png',images[1],'image/png'))]
    idem=key();one=api.web.post('/api/v1/imports',files=parts,headers=api.headers(idem));item=checked(one,'createImport',202)
    two=api.web.post('/api/v1/imports',files=list(reversed(parts)),headers=api.headers(idem))
    assert checked(two,'createImport',202)==item and two.headers['Idempotency-Replayed']=='true'
    capture=api.get('/api/v1/source-captures/'+item['source_capture_id'],'getSourceCapture')
    assert [a['original_filename'] for a in capture['assets']]==['second.png','first.png']
    assert [a['asset']['id'] for a in capture['assets']]==[digest(images[1]),digest(images[0])]
    duplicate=api.web.post('/api/v1/imports',files=parts+[parts[1]],headers=api.headers())
    checked(duplicate,'createImport',400)
    wrong=api.web.post('/api/v1/imports',files=[parts[0],('image_1',('x',images[0],'image/jpeg')),parts[2]],headers=api.headers())
    checked(wrong,'createImport',415)
    assert api.worker.run_once() and api.worker.run_once()
    # Follow the same image upload -> extraction -> preview -> explicit send path
    # used by iOS. The model receives pages in manifest order, not multipart order.
    evidence=json.loads(api.calls[0].content)['input'][1]['content']
    assert len(evidence)==2
    for part,color in zip(evidence,[(0,0,255),(255,0,0)]):
        assert part['type']=='input_image'
        with Image.open(BytesIO(base64.b64decode(part['image_url'].split(',',1)[1]))) as im:
            assert im.getpixel((0,0))==color
    recipe=api.get('/api/v1/recipes/'+item['recipe_id'],'getRecipe')
    render_id=recipe['processing']['current_render_set_id']
    render=api.get('/api/v1/render-sets/'+render_id,'getRenderSet')
    preview=api.web.get(render['pages'][0]['asset']['href'],headers=api.headers())
    with Image.open(BytesIO(preview.content)) as im:assert im.size==(1872,1404) and im.mode=='1'
    assert api.service.store.state['publication_id']==original
    publish_key=key();body={'render_set_id':render_id,'page_number':1}
    receipt=api.post('/api/v1/publications',body,'createPublication',201,publish_key)
    assert api.post('/api/v1/publications',body,'createPublication',201,publish_key)==receipt
    displayed=api.lan.get('/api/display',headers=api.firmware_auth).json()
    assert api.lan.get('/images/'+displayed['filename']).content==preview.content


@pytest.mark.parametrize('kind',['truncated_jpeg','corrupt_png','crc_png','animated_png','empty','wrong_mime'])
def test_invalid_image_upload_creates_no_recipe(api,kind):
    out=BytesIO();Image.new('RGB',(32,32),'red').save(out,format='JPEG' if kind=='truncated_jpeg' else 'PNG')
    data=out.getvalue();mime='image/png'
    if kind=='truncated_jpeg':data=data[:-10];mime='image/jpeg'
    if kind=='corrupt_png':data=data[:-20]
    if kind=='crc_png':
        broken=bytearray(data);broken[data.index(b'IDAT')+5]^=1;data=bytes(broken)
    if kind=='empty':data=b''
    if kind=='wrong_mime':mime='image/jpeg'
    if kind=='animated_png':
        out=BytesIO();Image.new('RGB',(32,32),'red').save(out,format='PNG',save_all=True,append_images=[Image.new('RGB',(32,32),'blue')]);data=out.getvalue()
    metadata={'kind':'images','attachments':[{'part_name':'image_1','filename':'photo'}]}
    parts=[('metadata',(None,json.dumps(metadata),'application/json')),('image_1',('photo',data,mime))]
    result=checked(api.web.post('/api/v1/imports',files=parts,headers=api.headers()),'createImport',415)
    assert result['error']['code']=='unsupported_media_type'
    assert api.db.one('SELECT count(*) n FROM recipes')['n']==0
    assert api.db.one('SELECT count(*) n FROM jobs')['n']==0


@pytest.mark.parametrize('limit',['image_pixels','image_file_bytes','image_total_bytes'])
def test_image_upload_limits_are_enforced_before_persistence(api,monkeypatch,limit):
    from server.api import uploads
    monkeypatch.setattr(uploads,'LIMITS',{**uploads.LIMITS,limit:1})
    out=BytesIO();Image.new('RGB',(10,10),'red').save(out,format='PNG')
    metadata={'kind':'images','attachments':[{'part_name':'image_1','filename':'photo.png'}]}
    parts=[('metadata',(None,json.dumps(metadata),'application/json')),('image_1',('photo.png',out.getvalue(),'image/png'))]
    result=checked(api.web.post('/api/v1/imports',files=parts,headers=api.headers()),'createImport',413)
    assert result['error']['code']=='payload_too_large'
    assert api.db.one('SELECT count(*) n FROM recipes')['n']==0


def test_url_capture_extract_publish_refresh_and_replay(api):
    from server.recipes.capture import Page
    from test_capture import HTML
    calls=[]
    class Fetcher:
        def fetch(self,url):
            calls.append(url)
            return Page('https://example.com/final',HTML,'text/html; charset=utf-8',[])
    api.worker.fetcher=Fetcher()
    status=api.get('/api/v1/status','getStatus')
    assert 'url' in status['capabilities']['import_kinds'] and status['capabilities']['source_refresh']
    api.pair();previous=api.service.store.state['publication_id']
    url='https://example.com/recipe?tracking=a%20b#recipe'
    idem=key();body={'kind':'url','url':url}
    item=api.post('/api/v1/imports',body,'createImport',202,idem)
    assert api.post('/api/v1/imports',body,'createImport',202,idem)==item
    assert not calls and not api.calls
    capture_path='/api/v1/source-captures/'+item['source_capture_id']
    assert api.get(capture_path,'getSourceCapture')['state']=='pending'
    assert api.worker.run_once()
    saved=api.get(capture_path,'getSourceCapture')
    assert saved['state']=='complete' and saved['submitted_url']==url and saved['resolved_url']=='https://example.com/final'
    raw=next(a['asset'] for a in saved['assets'] if a['role']=='response_html')
    download=api.web.get(raw['href'],headers=api.headers())
    assert download.content==HTML and 'attachment' in download.headers['Content-Disposition']
    job=api.get('/api/v1/jobs/'+item['job_id'],'getJob')
    assert job['kind']=='capture' and len(job['downstream_job_ids'])==1
    assert api.worker.run_once() and api.worker.run_once()
    assert calls==[url] and len(api.calls)==1
    evidence=json.loads(api.calls[0].content)['input'][1]['content']
    assert len(evidence)==2 and all(part['type']=='input_text' for part in evidence)
    recipe=api.get('/api/v1/recipes/'+item['recipe_id'],'getRecipe')
    render_id=recipe['processing']['current_render_set_id']
    assert render_id and api.service.store.state['publication_id']==previous
    receipt=api.post('/api/v1/publications',{'render_set_id':render_id,'page_number':1},'createPublication',201)
    assert receipt['status']=='selected'
    # Refresh creates immutable linked evidence and a candidate, never replaces
    # a current revision or reselects a reader page on its own.
    refresh_key=key();refresh_body={'source_capture_id':saved['id']}
    path='/api/v1/recipes/'+recipe['id']+'/refresh'
    refreshed=api.post(path,refresh_body,'refreshRecipe',202,refresh_key)
    assert api.post(path,refresh_body,'refreshRecipe',202,refresh_key)==refreshed
    assert api.worker.run_once() and api.worker.run_once() and api.worker.run_once()
    assert api.get(capture_path,'getSourceCapture')==saved
    new_capture=api.get('/api/v1/source-captures/'+refreshed['source_capture_id'],'getSourceCapture')
    assert new_capture['previous_capture_id']==saved['id'] and new_capture['submitted_url']==url
    new_recipe=api.get('/api/v1/recipes/'+recipe['id'],'getRecipe')
    assert new_recipe['current_revision_id']==recipe['current_revision_id'] and new_recipe['candidate_count']==1


def test_failed_url_capture_refresh_and_cancellation(api):
    from server.recipes.capture import Page
    from server.recipes.document import RecipeError
    from test_capture import HTML
    class FailingFetcher:
        def fetch(self,url):raise RecipeError('capture_failed','Temporary website failure',retryable=True)
    api.worker.fetcher=FailingFetcher()
    item=api.post('/api/v1/imports',{'kind':'url','url':'https://example.com/recipe'},'createImport',202)
    assert api.worker.run_once()
    failed=api.get('/api/v1/source-captures/'+item['source_capture_id'],'getSourceCapture')
    assert failed['state']=='failed' and not api.calls
    job=api.get('/api/v1/jobs/'+item['job_id'],'getJob')
    assert job['status']=='failed' and not job['retry_allowed']
    refreshed=api.post('/api/v1/recipes/'+item['recipe_id']+'/refresh',{'source_capture_id':failed['id']},'refreshRecipe',202)
    class CancellingFetcher:
        def fetch(self,url):
            api.db.cancel(refreshed['job_id'])
            return Page(url,HTML,'text/html',[])
    api.worker.fetcher=CancellingFetcher()
    assert api.worker.run_once()
    cancelled=api.get('/api/v1/jobs/'+refreshed['job_id'],'getJob')
    assert cancelled['status']=='cancelled' and not cancelled['downstream_job_ids']
    assert api.get('/api/v1/source-captures/'+failed['id'],'getSourceCapture')==failed


def test_url_capture_retains_html_on_parse_failure_and_resumes_saved_response(api):
    from server.recipes.capture import Page
    from test_capture import HTML
    class Fetcher:
        def fetch(self,url):return Page(url,b'<script>render()</script>','text/html',[])
    api.worker.fetcher=Fetcher()
    item=api.post('/api/v1/imports',{'kind':'url','url':'https://example.com/empty'},'createImport',202)
    assert api.worker.run_once()
    failed=api.get('/api/v1/source-captures/'+item['source_capture_id'],'getSourceCapture')
    assert failed['state']=='failed' and [a['role'] for a in failed['assets']]==['response_html']
    item=api.post('/api/v1/imports',{'kind':'url','url':'https://example.com/recipe'},'createImport',202)
    claimed=api.db.claim(kinds=('capture',))
    api.db.record_capture_response(claimed,'https://example.com/final',api.db.asset(HTML,'text/html'),dict(content_type='text/html',redirects=[]))
    api.db.execute("UPDATE jobs SET lease_expires_at='2000-01-01' WHERE id=?",(claimed['id'],))
    class NoFetcher:
        def fetch(self,url):raise AssertionError('Must resume from saved HTML')
    api.worker.fetcher=NoFetcher()
    assert api.worker.run_once()
    recovered=api.get('/api/v1/jobs/'+item['job_id'],'getJob')
    assert recovered['status']=='succeeded' and recovered['attempt_count']==2 and len(recovered['downstream_job_ids'])==1
