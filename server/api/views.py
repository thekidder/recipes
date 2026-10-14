"""Explicit public projections and signed keyset cursors. Never serialize SQL rows."""
import base64
import hashlib
import hmac
import json
import time
from server.recipes.db import dumps
from .errors import ApiError, error_detail

PROFILE = {'id': 'trmnl-x-landscape-v1', 'width': 1872, 'height': 1404, 'encoding': 'png-1bit'}


class Views:
    def __init__(self, library):
        self.db = library

    def record(self, table, identifier):
        if table not in {'users','user_tokens','enrollments','recipes','recipe_revisions','source_captures',
                         'jobs','job_attempts','llm_calls','render_sets','publications','assets'}:
            raise ValueError('Unknown table')
        item = self.db.one(f'SELECT * FROM {table} WHERE id=?', (identifier,))
        if not item:
            raise ApiError('not_found', 'Resource not found.', 404)
        return item

    def user(self, identifier):
        item = self.record('users', identifier)
        return {'id': item['id'], 'display_name': item['display_name']}

    def admin_user(self, item):
        return {key: item[key] for key in ('id','display_name','created_at','disabled_at')}

    def credential(self, item):
        return {key: item[key] for key in ('id','user_id','label','created_at','revoked_at')}

    def enrollment(self, item):
        from server.recipes.db import now
        state = 'redeemed' if item['credential_id'] else 'revoked' if item['revoked_at'] else 'expired' if item['expires_at'] <= now() else 'pending'
        return {**{key: item[key] for key in ('id','user_id','device_label','created_at','expires_at','credential_id')}, 'state': state}

    def asset(self, identifier):
        item = self.record('assets', identifier)
        return dict(id=item['id'], media_type=item['media_type'], byte_size=item['byte_size'], href='/api/v1/assets/'+item['id'])

    def candidate(self, revision):
        return revision['origin'] == 'extraction' and not self.db.one('SELECT 1 FROM revision_acceptances WHERE revision_id=?', (revision['id'],))

    def revision(self, item, full=False):
        recipe = self.record('recipes', item['recipe_id'])
        result = {k: item[k] for k in ('id','recipe_id','parent_revision_id','source_capture_id','origin','title','created_at')}
        result.update(generated_by_attempt_id=item['generated_by_attempt'], warnings=json.loads(item['warnings_json']),
                      created_by=self.user(item['created_by']), is_current=recipe['current_revision_id']==item['id'],
                      is_candidate=bool(self.candidate(item)))
        if full:
            result.update(markdown=item['markdown'], markdown_sha256=item['markdown_sha256'], format=item['format_version'])
        return result

    def complete(self, item):
        pages = self.db.all('SELECT * FROM render_pages WHERE render_set_id=? ORDER BY page_number', (item['id'],))
        return len(pages)==item['page_count'] and [p['page_number'] for p in pages]==list(range(1,len(pages)+1)) and all(self.db.asset_path(p['asset_id']).is_file() for p in pages)

    def current_render(self, revision):
        if revision:
            for item in self.db.all('SELECT * FROM render_sets WHERE revision_id=? ORDER BY created_at DESC,id DESC', (revision,)):
                if self.complete(item):
                    return item
        return None

    def recipe(self, item, full=False):
        revision = self.record('recipe_revisions',item['current_revision_id']) if item['current_revision_id'] else None
        render = self.current_render(item['current_revision_id'])
        counts = self.db.one("SELECT sum(status IN ('queued','running')) active,sum(status='failed') failed FROM jobs WHERE recipe_id=?", (item['id'],))
        latest = self.db.one('SELECT id FROM jobs WHERE recipe_id=? ORDER BY created_at DESC,id DESC LIMIT 1', (item['id'],))
        latest_render = self.db.one("SELECT status FROM jobs WHERE input_revision_id=? AND kind='render' ORDER BY created_at DESC,id DESC LIMIT 1", (item['current_revision_id'],))
        active_render = self.db.one("SELECT 1 FROM jobs WHERE input_revision_id=? AND kind='render' AND status IN ('queued','running')", (item['current_revision_id'],))
        preview = 'ready' if render else 'pending' if revision and active_render else 'failed' if revision and latest_render and latest_render['status']=='failed' else 'none'
        candidates = self.db.one("SELECT count(*) n FROM recipe_revisions v WHERE v.recipe_id=? AND v.origin='extraction' AND NOT EXISTS(SELECT 1 FROM revision_acceptances a WHERE a.revision_id=v.id)", (item['id'],))['n']
        result = {k:item[k] for k in ('id','current_revision_id','lock_version','created_at','updated_at','archived_at')}
        prefix = '/api/v1/recipes/'+item['id']
        result.update(title=revision['title'] if revision else None,created_by=self.user(item['created_by']),candidate_count=candidates,
                      processing=dict(active_job_count=counts['active'] or 0,failed_job_count=counts['failed'] or 0,
                                      latest_job_id=latest['id'] if latest else None,jobs_href=prefix+'/jobs',
                                      current_preview=preview,current_render_set_id=render['id'] if render else None))
        if full:
            result.update(current_revision=self.revision(revision,True) if revision else None,revisions_href=prefix+'/revisions',
                          candidates_href=prefix+'/revisions?scope=candidates',source_captures_href=prefix+'/source-captures',jobs_href=prefix+'/jobs')
        return result

    def capture(self, item):
        result = {k:item[k] for k in ('id','recipe_id','previous_capture_id','kind','submitted_url','resolved_url','state','submitted_at','captured_at')}
        links = self.db.all('SELECT * FROM source_assets WHERE capture_id=? ORDER BY role,ordinal', (item['id'],))
        result.update(created_by=self.user(item['created_by']),metadata=json.loads(item['capture_metadata_json']),error=error_detail(item['error_json']),
                      assets=[dict(role=a['role'],ordinal=a['ordinal'],original_filename=a['original_filename'],asset=self.asset(a['asset_id'])) for a in links])
        return result

    def tree(self, identifier):
        return self.db.all('''WITH RECURSIVE tree(id) AS (SELECT id FROM jobs WHERE id=?
            UNION ALL SELECT j.id FROM jobs j JOIN tree t ON j.parent_job_id=t.id)
            SELECT j.* FROM jobs j JOIN tree t ON j.id=t.id''', (identifier,))

    def job(self, item):
        count = self.db.one('SELECT count(*) n FROM job_attempts WHERE job_id=?', (item['id'],))['n']
        recipe = self.record('recipes',item['recipe_id'])
        revision = self.db.one('SELECT r.id FROM recipe_revisions r JOIN job_attempts a ON a.id=r.generated_by_attempt WHERE a.job_id=? ORDER BY r.created_at DESC LIMIT 1', (item['id'],))
        rendered = self.db.one('SELECT render_set_id FROM api_job_results WHERE job_id=?', (item['id'],))
        # Pre-API successful render jobs may not have an exact result receipt.
        if not rendered and item['kind']=='render' and item['status']=='succeeded':
            rendered = self.db.one('SELECT rs.id render_set_id FROM render_sets rs JOIN job_attempts a ON a.id=rs.generated_by_attempt WHERE a.job_id=? ORDER BY rs.created_at DESC LIMIT 1', (item['id'],))
        result = {k:item[k] for k in ('id','recipe_id','parent_job_id','kind','status','source_capture_id','input_revision_id','base_revision_id','created_at','finished_at')}
        result.update(requested_by=self.user(item['requested_by']),attempt_count=count,
                      retry_allowed=not recipe['archived_at'] and item['status']=='failed' and item['kind']!='capture' and count<5,
                      cancel_allowed=not recipe['archived_at'] and any(j['status'] in ('queued','running') for j in self.tree(item['id'])),
                      error=error_detail(item['error_json']),result=dict(source_capture_id=item['source_capture_id'] if item['status']=='succeeded' else None,
                      revision_id=revision['id'] if revision else None,render_set_id=rendered['render_set_id'] if rendered else None),
                      downstream_job_ids=[j['id'] for j in self.db.all('SELECT id FROM jobs WHERE parent_job_id=? ORDER BY created_at,id',(item['id'],))],attempts_href='/api/v1/jobs/'+item['id']+'/attempts')
        return result

    def attempt(self, item):
        return {**{k:item[k] for k in ('id','job_id','attempt_number','status','started_at','finished_at')},'error':error_detail(item['error_json']),'llm_calls_href':'/api/v1/attempts/'+item['id']+'/llm-calls'}

    def call(self,item):
        result={k:item[k] for k in ('id','attempt_id','sequence','provider','requested_model','reported_model','provider_request_id','prompt_version','adapter_version','status','started_at','finished_at')}
        for field in ('request','response','structured_output'):
            result[field+'_asset']=self.asset(item[field+'_asset_id']) if item[field+'_asset_id'] else None
        result.update(usage=json.loads(item['usage_json']) if item['usage_json'] else None,error=error_detail(item['error_json']))
        return result

    def render(self,item):
        if not self.complete(item):
            raise ApiError('render_unavailable','Render pages are unavailable. Request a render to regenerate them.',409)
        return dict(id=item['id'],revision_id=item['revision_id'],renderer_version=item['renderer_version'],profile=PROFILE,
                    preferences=json.loads(item['preferences_json']),page_count=item['page_count'],created_at=item['created_at'],
                    pages=[dict(page_number=p['page_number'],asset=self.asset(p['asset_id'])) for p in self.db.all('SELECT * FROM render_pages WHERE render_set_id=? ORDER BY page_number',(item['id'],))])

    def publication(self,item):
        descriptor=json.loads(item['asset_descriptor_json'])
        render=self.record('render_sets',item['render_set_id']) if item['render_set_id'] else None
        return dict(id=item['id'],kind='recipe' if render else 'legacy',render_set_id=item['render_set_id'],revision_id=render['revision_id'] if render else None,
                    page_number=item['page_number'],legacy_label=None if render else descriptor.get('label','Legacy image'),
                    selected_by=self.user(item['requested_by']) if item['requested_by'] else None,selected_at=item['created_at'])

    def page(self,sql,args,mapper,query,context,order='created_at',ascending=False):
        limit=query.get('limit',50)
        binding=[context,limit,order,ascending]
        cursor=query.get('cursor')
        expiry=int(time.time())+86400
        high=None; last=None
        if cursor:
            try:
                payload,signature=cursor.split('.')
                wanted=hmac.new(self.db.cursor_secret,payload.encode(),hashlib.sha256).hexdigest()
                if not hmac.compare_digest(wanted,signature):raise ValueError()
                data=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
                if data['binding']!=binding or data['expires']<=time.time():raise ValueError()
                expiry,high,last=data['expires'],data['high'],data['last']
            except (ValueError,KeyError,TypeError):
                raise ApiError('invalid_cursor','Cursor is invalid or expired. Restart the list.',400)
        if high is None:
            row=self.db.one(f'SELECT * FROM ({sql}) q ORDER BY {order} DESC,id DESC LIMIT 1',args)
            if row is None:return dict(items=[],next_cursor=None,has_more=False)
            high=[row[order],row['id']]
        where=f'({order},id)<=(?,?)'
        values=[*args,*high]
        if last:
            where+=f" AND ({order},id){'>' if ascending else '<'}(?,?)";values+=last
        direction='ASC' if ascending else 'DESC'
        rows=self.db.all(f'SELECT * FROM ({sql}) q WHERE {where} ORDER BY {order} {direction},id {direction} LIMIT ?',(*values,limit+1))
        more=len(rows)>limit;rows=rows[:limit];next_cursor=None
        if more:
            data=dict(binding=binding,expires=expiry,high=high,last=[rows[-1][order],rows[-1]['id']])
            payload=base64.urlsafe_b64encode(dumps(data).encode()).rstrip(b'=').decode()
            next_cursor=payload+'.'+hmac.new(self.db.cursor_secret,payload.encode(),hashlib.sha256).hexdigest()
        return dict(items=[mapper(row) for row in rows],next_cursor=next_cursor,has_more=more)
