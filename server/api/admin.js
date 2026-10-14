'use strict';
const $ = id => document.getElementById(id);
let users = [], enrollment = null, qrBlob = null, timer = null, busy = false;
const pendingKeys = new Map();
function message(text, error = false) { $('message').textContent = text; $('message').className = error ? 'error' : ''; }
function controls() {
  document.querySelectorAll('button,input,select').forEach(element => { element.disabled = busy; });
  const selected = users.find(user => user.id === $('user').value);
  $('connect').disabled = busy || !selected || !!selected.disabled_at;
  $('disable-user').disabled = busy || !selected || !!selected.disabled_at;
}
async function request(path, body, idempotent = true) {
  const options = {cache: 'no-store', credentials: 'same-origin', redirect: 'error'};
  let pending;
  if (body !== undefined) {
    options.method = 'POST'; options.headers = {'Content-Type': 'application/json', 'X-eRecipe-Control': '1'};
    options.body = JSON.stringify(body);
    if (idempotent) {
      pending = path + options.body;
      if (!pendingKeys.has(pending)) pendingKeys.set(pending, crypto.randomUUID());
      options.headers['Idempotency-Key'] = pendingKeys.get(pending);
    }
  }
  const response = await fetch(path, options);
  const data = response.status === 204 ? null : await response.json();
  if (!response.ok) throw new Error(data?.error?.message || `Request failed (${response.status}).`);
  if (pending) pendingKeys.delete(pending);
  return data;
}
async function pages(path) {
  let result = [], cursor;
  do {
    const page = await request(path + '?limit=100' + (cursor ? '&cursor=' + encodeURIComponent(cursor) : ''));
    result.push(...page.items); cursor = page.next_cursor;
  } while (cursor);
  return result;
}
function hideQR() {
  clearTimeout(timer); timer = null; $('enrollment').hidden = true; $('qr').removeAttribute('src');
  if (qrBlob) URL.revokeObjectURL(qrBlob);
  qrBlob = null;
}
async function credentials() {
  $('credentials').replaceChildren();
  if (!$('user').value) return;
  const selected = $('user').value;
  const records = await pages(`/control/v1/users/${encodeURIComponent(selected)}/credentials`);
  if ($('user').value !== selected) return;
  for (const item of records) {
    const row = document.createElement('tr');
    for (const text of [item.label, new Date(item.created_at).toLocaleString()]) { const cell = document.createElement('td'); cell.textContent = text; row.append(cell); }
    const cell = document.createElement('td');
    if (item.revoked_at) cell.textContent = 'Revoked';
    else {
      const button = document.createElement('button'); button.textContent = 'Revoke';
      button.disabled = busy;
      button.onclick = () => action(async () => { await request(`/control/v1/credentials/${item.id}/revoke`, {}); await credentials(); message('Credential revoked.'); }); cell.append(button);
    }
    row.append(cell); $('credentials').append(row);
  }
}
async function loadUsers(selectId) {
  users = await pages('/control/v1/users'); const old = selectId || $('user').value;
  $('user').replaceChildren();
  for (const user of users) { const opt = document.createElement('option'); opt.value = user.id; opt.textContent = user.display_name + (user.disabled_at ? ' (disabled)' : ''); $('user').append(opt); }
  if (users.some(user => user.id === old)) $('user').value = old;
  controls();
  await credentials();
}
async function action(fn) {
  if (busy) return;
  busy = true;
  controls();
  try { await fn(); } catch (error) { message(error.message || 'Connection failed. Retry the action.', true); }
  finally { busy = false; controls(); }
}
async function poll() {
  if (!enrollment) return;
  if (Date.parse(enrollment.expires_at) <= Date.now()) {
    hideQR(); message('Enrollment expired. Create a new QR when ready.'); return;
  }
  const id = enrollment.id;
  try {
    const status = await request(`/control/v1/enrollments/${id}`);
    if (!enrollment || enrollment.id !== id) return;
    if (status.state !== 'pending') {
      hideQR();
      $('recovery').hidden = status.state !== 'redeemed';
      message(status.state === 'redeemed' ? 'Credential issued. Finish setup on the phone.' : `Enrollment ${status.state}. Create a new QR when ready.`);
      await credentials(); return;
    }
    const seconds = Math.max(0, Math.ceil((Date.parse(status.expires_at) - Date.now()) / 1000));
    $('expiry').textContent = `Expires in ${Math.floor(seconds / 60)}m ${seconds % 60}s. This code can be used once.`;
  } catch (error) { message('Cannot check enrollment status. ' + error.message, true); }
  if (enrollment?.id === id) timer = setTimeout(poll, 2000);
}
$('create-user').onsubmit = event => { event.preventDefault(); action(async () => {
  const user = await request('/control/v1/users', {display_name: $('name').value});
  $('name').value = ''; await loadUsers(user.id); message('User created. Create a QR code to connect their phone.');
}); };
$('create-enrollment').onsubmit = event => { event.preventDefault(); action(async () => {
  hideQR(); enrollment = null; $('recovery').hidden = true;
  const result = await request(`/control/v1/users/${$('user').value}/enrollments`, {device_label: $('label').value}, false);
  enrollment = result.enrollment;
  qrBlob = URL.createObjectURL(new Blob([result.qr_svg], {type: 'image/svg+xml'}));
  $('qr').src = qrBlob; $('enrollment').hidden = false;
  message('Scan this QR code with the phone.'); await poll();
}); };
$('cancel-enrollment').onclick = () => action(async () => {
  if (!enrollment) return;
  await request(`/control/v1/enrollments/${enrollment.id}/revoke`, {}); hideQR(); enrollment = null; message('QR code cancelled.');
});
$('disable-user').onclick = () => action(async () => {
  const user = users.find(user => user.id === $('user').value);
  if (!user || !confirm(`Disable ${user.display_name} and revoke all their phone credentials?`)) return;
  await request(`/control/v1/users/${user.id}/disable`, {}); hideQR(); enrollment = null; await loadUsers(user.id); message('User disabled. Recipe history is retained.');
});
$('user').onchange = () => action(async () => { hideQR(); enrollment = null; $('recovery').hidden = true; await loadUsers(); });
window.addEventListener('pagehide', hideQR);
action(loadUsers);
