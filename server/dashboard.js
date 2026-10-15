'use strict';
const $ = id => document.getElementById(id);
const page = document.body.dataset.page;
const show = (id, value) => { $(id).textContent = value; };
let current, previewVersion, recipes = [], libraryReady = false, busy = false, refreshing = false;
let settingsDirty = false, librarySignature = '', lastLibraryRefresh = 0, logSignature = '';
const selectedPages = new Map();
document.querySelector(`[data-nav="${page}"]`).setAttribute('aria-current', 'page');
document.title = `eRecipe · ${{recipes:'Recipes',settings:'Settings',logs:'Logs & errors'}[page]}`;

function message(text, error = false) {
  show('message', text); $('message').hidden = !text; $('message').classList.toggle('error', error);
}
async function request(path, body) {
  const response = await fetch('/control/' + path, body === undefined ? {cache:'no-store'} : {
    method:'POST', headers:{'Content-Type':'application/json','X-eRecipe-Control':'1'}, body:JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || (typeof data.detail === 'string' ? data.detail : Array.isArray(data.detail) ? data.detail.map(item => item.msg).join(' ') : 'The request failed. Please refresh and try again.'));
  return data;
}
const time = value => value ? new Date(value).toLocaleString() : 'Unavailable';
function controls() {
  const cal = current?.calendar_status;
  $('calendar-show').disabled = busy || !(cal?.configured && cal?.available) || cal.content_mode === 'calendar';
  $('save-settings').disabled = busy || !current;
  $('poll').disabled = busy || !current;
  $('mode').disabled = busy || !current;
  $('night-enabled').disabled = busy || !current;
  $('night-fields').disabled = busy || !current || !$('night-enabled').checked;
  $('refresh-library').disabled = busy;
  document.querySelectorAll('[data-send]').forEach(button => {
    button.disabled = busy || !current?.device_id || !libraryReady || !button.dataset.render;
  });
}
async function command(path, body, success) {
  if (busy) return;
  busy = true; controls();
  try {
    await request(path, body);
    if (path === 'settings') settingsDirty = false;
    message(success);
    await refreshStatus();
  } catch (error) { message(error.message, true); }
  finally { busy = false; controls(); }
}
function modeNote() {
  show('mode-note', {
    soak:'Normal polling keeps the current recipe or calendar selected. Calendar content updates automatically.',
    snapshot:'Keeps the selected screen unchanged. New diagnostic snapshots are not generated from this dashboard.',
    stress:'Refresh stress replaces the selected screen with diagnostics after each check-in and uses more battery.',
    tap_trial:'A legacy tap trial is active. Choose Normal to stop advancing trial pages.'
  }[$('mode').value]);
}
function validateNighttime() {
  const enabled = $('night-enabled').checked;
  $('night-end').setCustomValidity(enabled && $('night-start').value === $('night-end').value ? 'Choose different start and end times.' : '');
  $('night-poll').min = String(Math.max(5, Number($('poll').value) || 5));
}
async function refreshStatus() {
  try {
    current = await request('status');
    const s = current, cal = s.calendar_status;
    const interval = s.effective_poll_seconds ?? s.poll_seconds;
    const stale = s.sample_age_seconds != null && s.sample_age_seconds > Math.max((s.last_requested_poll_seconds ?? interval) * 3, 120);
    show('connection', !s.device_id ? 'Reader not paired' : !s.last_checkin ? 'Waiting for reader' : stale ? 'Check-in overdue' : 'Reader connected');
    show('battery', s.battery_percent == null ? 'Unavailable' : `${s.battery_percent.toFixed(0)}%`);
    show('battery-source', s.battery_source === 'GAUGE_SOC' ? 'Fuel gauge' : s.battery_percent == null ? 'No reported charge' : 'Firmware estimate');
    show('last-seen', s.sample_age_seconds == null ? 'Not yet' : s.sample_age_seconds < 60 ? `${Math.max(0,Math.round(s.sample_age_seconds))}s ago` : `${Math.round(s.sample_age_seconds / 60)}m ago`);
    show('poll-summary', `${s.nighttime_active ? 'Nighttime · ' : ''}Next requested interval: ${interval}s`);
    show('screen-kind', s.current.kind === 'CALENDAR' ? 'Calendar' : s.current.kind === 'RECIPE' ? 'Recipe' : 'Saved screen');
    show('screen-title', s.current.label);
    const fetched = s.last_fetch?.version === s.current.version && s.last_fetch.at >= s.published_at;
    show('delivery', !s.device_id ? 'Pair the reader to receive this screen.' : fetched ? 'Image fetched by device · screen update unconfirmed' : 'Selected · waiting for device to fetch');
    show('screen-page', s.current.page_number ? `Recipe page ${s.current.page_number}` : '');
    if (previewVersion !== s.current.version) {
      $('preview').src = '/control/preview?v=' + encodeURIComponent(s.current.version);
      $('preview').hidden = false; previewVersion = s.current.version;
    }
    const calendarText = !cal?.configured ? 'Calendar is not configured. Add ERECIPE_TRMNL_API_KEY to server/.env and restart.' :
      !cal.available ? 'Waiting for the first calendar image.' : cal.content_mode === 'calendar' ? 'Calendar is selected.' :
      cal.dismissal_ready ? 'Ready to return here or with a center tap on the reader.' : 'Return here at any time. Center-tap dismissal is ready after the recipe is fetched.';
    show('calendar-status', calendarText);
    show('calendar-settings', !cal?.configured ? calendarText : `Calendar is configured. ${cal.available ? 'Last updated ' + time(cal.synced_at) + '.' : 'Waiting for the first image.'}`);
    $('calendar-preview').hidden = !cal?.available;
    const night = s.nighttime;
    if (!settingsDirty) {
      $('poll').value = s.poll_seconds; $('mode').value = s.mode;
      if (night) {
        $('night-enabled').checked = night.enabled;
        $('night-start').value = night.start; $('night-end').value = night.end;
        $('night-poll').value = night.poll_seconds; $('night-timezone').value = night.timezone;
      }
      validateNighttime();
    }
    show('night-status', !night?.enabled ? 'Nighttime polling is off.' :
      `${s.nighttime_active ? 'Nighttime polling is active' : 'Normal polling is active'}. Saved schedule: ${night.start}–${night.end} (${night.timezone}), ${night.poll_seconds}s overnight. Next requested interval: ${interval}s.`);
    modeNote();
    if (page === 'logs') renderDiagnostics(s);
    controls();
  } catch (error) {
    current = null; controls(); show('connection', 'Server unavailable'); throw error;
  }
}
function renderRecipes() {
  const query = $('search').value.trim().toLocaleLowerCase();
  const visible = recipes.filter(recipe => (recipe.title || '').toLocaleLowerCase().includes(query));
  show('recipe-count', recipes.length);
  show('library-state', !recipes.length ? 'No recipes yet. Save a recipe from your phone to get started.' : !visible.length ? 'No recipes match your search.' : '');
  $('library-state').hidden = visible.length > 0;
  $('recipes-list').replaceChildren();
  for (const recipe of visible) {
    const row = document.createElement('article'); row.className = 'recipe';
    const info = document.createElement('div'), title = document.createElement('h3'), detail = document.createElement('small');
    title.textContent = recipe.title || 'Recipe being prepared';
    const p = recipe.processing, ready = p.current_preview === 'ready';
    detail.textContent = ready ? `${recipe.page_count} ${recipe.page_count === 1 ? 'page' : 'pages'} · Ready to send` :
      p.active_job_count ? 'Processing… Check back shortly.' : p.failed_job_count ? 'Processing failed · open this recipe on your phone to retry.' :
      recipe.candidate_count ? 'Review this recipe on your phone before sending.' : 'No rendered pages available yet.';
    info.append(title, detail);
    const actions = document.createElement('div'); actions.className = 'recipe-actions';
    const select = document.createElement('select'); select.setAttribute('aria-label', `Page to send for ${recipe.title || 'recipe'}`);
    for (let i = 1; i <= recipe.page_count; i++) select.add(new Option(`Page ${i}`, String(i)));
    select.value = String(Math.min(selectedPages.get(recipe.id) || 1, recipe.page_count));
    select.onchange = () => selectedPages.set(recipe.id, Number(select.value));
    if (recipe.page_count > 1) actions.append(select);
    const button = document.createElement('button'); button.className = 'primary'; button.type = 'button';
    button.textContent = ready ? 'Send to device' : 'Not ready'; button.dataset.send = recipe.id; button.dataset.render = p.current_render_set_id || '';
    button.setAttribute('aria-label', `Send ${recipe.title || 'recipe'} to device`);
    button.onclick = async () => {
      const pageNumber = Number(select.value) || 1;
      button.textContent = 'Sending…';
      await command(`recipes/${encodeURIComponent(recipe.id)}/send`, {render_set_id:p.current_render_set_id,page_number:pageNumber},
        `${recipe.title} · page ${pageNumber} selected for the next device check-in.`);
      button.textContent = 'Send to device';
      try { await refreshLibrary(); } catch (error) { message(error.message, true); }
    };
    actions.append(button); row.append(info, actions); $('recipes-list').append(row);
  }
  controls();
}
async function refreshLibrary() {
  try {
    let cursor = null, items = [];
    do {
      const result = await request('recipes' + (cursor ? '?cursor=' + encodeURIComponent(cursor) : ''));
      items.push(...result.items); cursor = result.next_cursor;
    } while (cursor);
    recipes = items; libraryReady = true; lastLibraryRefresh = Date.now();
    const signature = JSON.stringify(items);
    if (signature !== librarySignature) { librarySignature = signature; renderRecipes(); }
    else if (!$('library-state').hidden && $('library-state').dataset.failed) renderRecipes();
    delete $('library-state').dataset.failed;
    controls();
  } catch (error) {
    libraryReady = false; controls(); $('library-state').hidden = false; $('library-state').dataset.failed = 'true';
    show('library-state', 'Unable to load recipes. ' + error.message); throw error;
  }
}
function renderDiagnostics(s) {
  const errors = s.errors.map(error => `${time(error.at)}  ${error.message}`);
  if (s.calendar_status?.last_error) errors.unshift(`Calendar refresh: ${s.calendar_status.last_error}`);
  show('errors', errors.length ? errors.join('\n') : 'No recent errors.');
  show('telemetry', Object.keys(s.telemetry).length ? JSON.stringify(s.telemetry, null, 2) : 'No telemetry received.');
  $('details').replaceChildren();
  for (const [label,value] of Object.entries({'Device':s.device_id || 'Not paired','Last check-in':time(s.last_checkin),'Normal interval':`${s.poll_seconds}s`,'Next requested interval':`${s.effective_poll_seconds ?? s.poll_seconds}s`,'Last requested interval':s.last_requested_poll_seconds == null ? 'Unavailable' : `${s.last_requested_poll_seconds}s`,'Nighttime':s.nighttime?.enabled ? `${s.nighttime_active ? 'Active' : 'Scheduled'} · ${s.nighttime.start}–${s.nighttime.end} ${s.nighttime.timezone}` : 'Off','Observed interval':s.observed_interval_seconds == null ? 'Unavailable' : `${s.observed_interval_seconds.toFixed(1)}s`,'Mode':{soak:'Normal',snapshot:'Diagnostic snapshot',stress:'Refresh stress',tap_trial:'Legacy tap trial'}[s.mode] || s.mode,'Publication':s.publication_id,'Published':time(s.published_at),'Last image fetch':time(s.last_fetch?.at)})) {
    const dt = document.createElement('dt'), dd = document.createElement('dd'); dt.textContent = label; dd.textContent = value; $('details').append(dt,dd);
  }
  $('history').replaceChildren();
  for (const publication of [...s.publications].reverse().slice(0,15)) {
    const row = document.createElement('tr');
    for (const value of [time(publication.at),publication.label]) { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }
    $('history').append(row);
  }
}
async function refreshLogs() {
  try {
    const {items} = await request('logs');
    $('logs-state').hidden = items.length > 0; show('logs-state', 'No activity recorded yet.');
    const signature = JSON.stringify(items);
    if (signature === logSignature) return;
    logSignature = signature;
    const expanded = new Set([...$('events').querySelectorAll('details[open]')].map(detail => detail.dataset.key));
    $('events').replaceChildren();
    for (const item of items) {
      const row = document.createElement('tr');
      for (const value of [time(item.at),item.event]) { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }
      const cell = document.createElement('td'), details = document.createElement('details'), summary = document.createElement('summary'), pre = document.createElement('pre');
      details.dataset.key = item.at + ':' + item.event;
      details.open = expanded.has(details.dataset.key);
      summary.textContent = 'View details'; pre.textContent = JSON.stringify(item,null,2); details.append(summary,pre); cell.append(details); row.append(cell); $('events').append(row);
    }
  } catch (error) { $('logs-state').hidden = false; show('logs-state', 'Unable to load activity. ' + error.message); }
}
$('search').oninput = renderRecipes;
$('refresh-library').onclick = async () => { try { await refreshLibrary(); } catch (error) { message(error.message,true); } };
$('calendar-show').onclick = () => command('calendar/show', {}, 'Calendar selected for the next device check-in.');
$('settings-form').oninput = () => { settingsDirty = true; modeNote(); validateNighttime(); controls(); };
$('settings-form').onsubmit = event => {
  event.preventDefault();
  command('settings', {mode:$('mode').value,poll_seconds:Number($('poll').value),nighttime:{
    enabled:$('night-enabled').checked,start:$('night-start').value,end:$('night-end').value,
    poll_seconds:Number($('night-poll').value),timezone:$('night-timezone').value.trim()
  }}, 'Settings saved. They take effect on the next device check-in.');
};
async function refresh() {
  if (refreshing || busy) return;
  refreshing = true;
  try {
    await refreshStatus();
    if (page === 'recipes' && Date.now() - lastLibraryRefresh > 10000) await refreshLibrary();
    if (page === 'logs') await refreshLogs();
  } catch (error) { message(error.message, true); }
  finally { refreshing = false; }
}
refresh();
setInterval(refresh, 5000);
