"""The Wi-Fi sheets on the home page: name the bridge's network and set its passphrase
(apsetup), the first-boot "Secure This Wi-Fi" sheet, and joining another network (wifijoin).

The home shell serves these at /wifi too. While the access point is still open only
/api/wifi answers, so the secure sheet uses nothing else.
"""

from .wificountry import CODES

WIFI_JS = r"""
// ---- Wi-Fi: the name and passphrase form both sheets share ----
function wifiSuffix() {
  const w = S.wifi || {}, p = w.prefix || 'VectorBridge-', s = w.ssid || '';
  return s.startsWith(p) ? s.slice(p.length) : '';
}
function wifiFormHTML(hint, fresh) {  // fresh: no current name filled in, the first-boot sheet needs a new one
  const w = S.wifi || {}, max = w.suffix_max || 19;
  return '<label class="fl" for="wsuffix">Wi-Fi name</label>' +
    `<div style="display:flex;align-items:center;gap:6px"><span class="mono" id="wprefix" style="white-space:nowrap">${esc(w.prefix || 'VectorBridge-')}</span>` +
    `<input type="text" id="wsuffix" value="${fresh ? '' : esc(wifiSuffix())}" maxlength="${esc(max)}" autocapitalize="off" autocomplete="off" spellcheck="false" aria-describedby="wsuffixh"></div>` +
    `<div class="note" id="wsuffixh">${esc(hint.replace('{max}', max))}</div>` +
    '<label class="fl" for="wp1">Passphrase</label>' +
    '<input type="password" id="wp1" placeholder="At least 8 characters" maxlength="63" autocapitalize="off" autocomplete="new-password">' +
    '<label class="fl" for="wp2">Passphrase again</label>' +
    '<input type="password" id="wp2" maxlength="63" autocapitalize="off" autocomplete="new-password">';
}
async function wifiSave(btn, errEl) {  // resolves to the new name, or null after showing why not
  errEl.textContent = '';
  if ($('wp1').value !== $('wp2').value) { errEl.textContent = 'The two passphrases differ.'; return null; }
  btn.disabled = true;
  const body = {suffix: $('wsuffix').value, passphrase: $('wp1').value};
  if ($('wcountry') && $('wcountry').value) body.country = $('wcountry').value;  // the first-boot sheet only
  const r = await post('/api/wifi', body);
  btn.disabled = false;
  if (!r.ok) { errEl.textContent = (r.errors || [r.error || 'Not saved.']).join(' '); return null; }
  return r.ssid;
}
// R-D7: the Wi-Fi country decides the 5 GHz channels; none, 2.4 GHz only. Names come from the phone's own language.
const WIFI_COUNTRIES = '__COUNTRIES__'.split(' ');
let regionNames;
function countryName(c) {
  try { return (regionNames = regionNames || new Intl.DisplayNames(undefined, {type: 'region'})).of(c) || c; } catch (e) { return c; }
}
function countrySelectHTML(id, hint) {
  const sel = (S.wifi && S.wifi.country) || '';
  const opts = WIFI_COUNTRIES.map(c => [countryName(c), c]).sort((a, b) => a[0].localeCompare(b[0]));
  return `<label class="fl" for="${id}">Wi-Fi country</label><select id="${id}" aria-describedby="${id}h">` +
    (sel ? '' : '<option value="" selected>Not set (2.4 GHz only)</option>') +
    opts.map(([n, c]) => `<option value="${c}"${c === sel ? ' selected' : ''}>${esc(n)}</option>`).join('') + '</select>' +
    `<div class="note" id="${id}h">${esc(hint)}</div>`;
}
function wifiPendingHTML() {
  return S.wifi && S.wifi.pending ? '<p class="note">A change is waiting; the Wi-Fi is about to restart under its new name.</p>' : '';
}

// ---- V2-33: first boot, the access point is still open ----
SHEETS.secure = {
  render(el) {
    const w = S.wifi || {};
    if (w.ok && !w.unsecured) {
      el.innerHTML = sheetHead({id: 's33h', title: 'Secure This Wi-Fi'}) +
        `<div class="head c-ok">${icon('ok', 22)}<span>This Wi-Fi has a passphrase</span></div>` +
        '<a href="#wifi" data-sheet="wifi" class="btn secondary">Open Wi-Fi Settings</a>';
      return;
    }
    el.innerHTML = sheetHead({id: 's33h', title: 'Secure This Wi-Fi', sub: 'First step. Takes one minute.'}) +
      '<div id="secbody" style="display:flex;flex-direction:column;gap:12px">' +
      `<section class="card b-warn" role="region" aria-label="This Wi-Fi is open"><div class="head c-warn">${icon('warn', 22)}<span>This Wi-Fi is open</span></div>` +
      `<div>${esc(w.ssid || 'This Wi-Fi')} has no passphrase. Until it has one, settings cannot be saved and the bridge will not key the radio.</div></section>` +
      '<div style="display:flex;flex-direction:column;gap:8px">' + wifiFormHTML('A new name, not the open one: phones that joined it will not rejoin it with a passphrase. 1 to {max} letters, digits, hyphen or underscore.', true) +
      countrySelectHTML('wcountry', 'Where you use the bridge. It sets which 5 GHz channels a USB Wi-Fi adapter may use. Until it is set, the Wi-Fi stays on 2.4 GHz.') + '</div>' +
      '<div class="box"><h3>After you save</h3><ul>' +
      '<li>The Wi-Fi restarts with the passphrase. This phone drops off and has to rejoin. A brand-new card also reboots once.</li>' +
      '<li>Rejoin the Wi-Fi with the new passphrase and reopen <b class="mono">vectorbridge.local</b> or <b class="mono">10.42.0.1</b>.</li>' +
      '<li>The home page then offers Set Up a Radio.</li></ul></div>' +
      '<p class="err" id="secerr" role="alert"></p>' +
      '<button type="button" class="btn primary" id="secgo">Save and Secure Wi-Fi</button>' +
      '<div id="secpend">' + wifiPendingHTML() + '</div>' +
      '<p class="note">Write the passphrase on the label under the bridge. If it is lost, put the card in a computer and open <b class="mono">vector-bridge-wifi.txt</b> on its boot partition.</p></div>';
    $('secgo').onclick = async () => {
      const ssid = await wifiSave($('secgo'), $('secerr'));
      if (!ssid) return;
      $('secbody').innerHTML = `<div class="head c-ok" role="status">${icon('ok', 22)}<span>Saved as ${esc(ssid)}</span></div>` +
        '<p>The Wi-Fi restarts with its passphrase within a minute, and this phone drops off. A brand-new card also reboots once.</p>' +
        `<p>Rejoin the Wi-Fi with the new passphrase and reopen <b class="mono">vectorbridge.local</b> or <b class="mono">10.42.0.1</b>.</p>` +
        '<p class="note">If the passphrase is lost, put the card in a computer and open <b class="mono">vector-bridge-wifi.txt</b> on its boot partition.</p>';
      $('s33h').focus();
    };
  },
  update() { setHTML($('secpend'), wifiPendingHTML()); },
};

// ---- V2-31: the bridge's own Wi-Fi, and joining another network (V2-32 confirms) ----
function joinResultHTML() {
  const j = S.join;
  if (!j) return '';
  if (j.pending) return `<p class="note" role="status">${icon('busy', 16, 'spin')} Working... this page may lose its connection.</p>`;
  if (j.result && j.result.text) return `<p class="${j.result.ok ? 'note' : 'err'}" role="status">${esc(j.result.text)}</p>`;
  return '';
}
function joinSavedHTML() {
  const j = S.join;
  if (!j || !(j.saved || []).length) return '<p class="note">None yet. A network the bridge joins is remembered here.</p>';
  const b = 'class="btn secondary" style="width:auto;min-height:44px;font-size:15px"';
  return '<ul class="rows" style="padding:0">' + j.saved.map(n => {
    const home = j.home === n, here = !j.on_ap && j.connection === n;
    return `<li><div><div class="t">${esc(n)}</div>` + (here || home ? `<div class="s">${[here ? 'Joined now' : '', home ? 'Rejoins at boot when in range' : ''].filter(Boolean).join('. ')}</div>` : '') + '</div>' +
      '<div style="display:flex;gap:8px;flex:none;flex-wrap:wrap;justify-content:flex-end">' +
      (here ? '' : `<button type="button" ${b} data-jup="${esc(n)}" aria-label="Join ${esc(n)}">Join</button>`) +
      `<button type="button" ${b} data-jhome="${esc(home ? '' : n)}">${home ? 'Rejoins at boot: turn off' : 'Rejoin at boot'}</button></div></li>`;
  }).join('') + '</ul>';
}
function wifiStatusHTML() {
  const j = S.join;
  if (S.wifi && S.wifi.unsecured) return `<div class="head c-warn">${icon('warn', 22)}<span>Bridge Wi-Fi is open</span></div>`;
  if (j && !j.on_ap) return `<div class="head c-warn">${icon('warn', 22)}<span>Bridge Wi-Fi is off. Joined ${esc(j.connection || 'another network')}</span></div>`;
  if (!S.wifi || !S.wifi.ok) return `<div class="head c-dim">${icon('dot', 22)}<span>Bridge Wi-Fi not reported</span></div>`;
  return `<div class="head c-ok">${icon('ok', 22)}<span>Bridge Wi-Fi, secured</span></div>`;
}
function wifiRowsHTML() {
  const row = (k, v) => `<div class="kv" style="justify-content:space-between;border-bottom:1px solid var(--line)"><dt class="k">${esc(k)}</dt><dd class="v mono">${esc(v)}</dd></div>`;
  return row('Name', (S.wifi && S.wifi.ssid) || 'Not reported') + row('Address', address()) + row('Also', 'vectorbridge.local') +
    row('Radio', (S.join && S.join.radio) || 'Not reported');
}
function joinLeave(ssid, body) {  // V2-32: leaving the bridge Wi-Fi drops this page
  const own = (S.wifi && S.wifi.ssid) || 'this Wi-Fi', j = S.join;
  if (j && !j.on_ap) {  // already off the bridge Wi-Fi: switching networks still drops this page
    const now = j.connection || 'this network';
    confirmBox({title: `Leave ${now}?`, lead: `The bridge will join ${ssid}. You will need to reconnect to reach this page.`,
      listTitle: 'What happens next', items: [
        `Join ${esc(ssid)} on this phone, then open <b class="mono">vectorbridge.local</b>. If that fails, the router's device list shows the new address.`,
        'Any app that was transmitting is unkeyed.',
        'If the join fails, the bridge Wi-Fi comes back by itself.'],
      cancel: `Stay on ${now}`, ok: `Join ${ssid}`, onOk: () => SHEETS.wifi.join(body)});
    return;
  }
  confirmBox({title: 'Leave the bridge Wi-Fi?', lead: `The bridge will join ${ssid}. You will need to reconnect to reach this page.`,
    listTitle: 'What happens next', items: [
      `The bridge Wi-Fi ${esc(own)} turns off. This phone loses this page.`,
      `Join ${esc(ssid)} on this phone, then open <b class="mono">vectorbridge.local</b>. If that fails, the router's device list shows the new address.`,
      'Any app that was transmitting is unkeyed.',
      'To come back, open this page and tap Back to Bridge Wi-Fi. If the home network is not found at boot, the bridge Wi-Fi comes back by itself.'],
    cancel: 'Stay on Bridge Wi-Fi', ok: `Join ${ssid}`, onOk: () => SHEETS.wifi.join(body)});
}
// R-D4: SSH is off on a fresh card. Turning it on sets a new password for the login account.
function sshHTML() {
  const x = S.ssh;
  if (!x) return '';
  if (x.pending) return `<p class="note" role="status">${icon('busy', 16, 'spin')} Working...</p>`;
  const head = x.on ? `<div class="head c-warn">${icon('warn', 22)}<span>SSH is on</span></div>` +
      `<p class="note">Anyone on this network who knows the password can log in as <b class="mono">${esc(x.user || 'the login account')}</b> and change anything on the bridge.</p>`
    : `<div class="head c-dim">${icon('dot', 22)}<span>SSH is off</span></div>`;
  return head + (x.result && x.result.text ? `<p class="${x.result.ok ? 'note' : 'err'}" role="status">${esc(x.result.text)}</p>` : '');
}
SHEETS.wifi = {
  render(el) {
    el.innerHTML = sheetHead({id: 's31h', title: 'Wi-Fi'}) +
      '<div id="wstat"></div><dl id="wrows" style="border-top:1px solid var(--line)"></dl>' +
      '<div class="sec" id="wqr"><div style="display:flex;gap:12px;align-items:center">' +
      `<img src="/wifi-qr.svg" width="132" height="132" alt="QR code to join ${esc((S.wifi && S.wifi.ssid) || 'the bridge Wi-Fi')}" style="flex:none;background:#ffffff;border-radius:8px;padding:6px">` +
      '<div style="display:flex;flex-direction:column;gap:4px"><h3>Join from another phone</h3><p class="note" id="wqrnote"></p></div></div></div>' +
      '<div class="sec"><h3>Change name or passphrase</h3>' + wifiFormHTML('1 to {max} letters, digits, hyphen or underscore after the fixed start. Shows on every phone nearby.') +
      '<p class="why">Saving restarts the Wi-Fi. Every joined device drops off and has to rejoin with the new details. Apps that were transmitting are unkeyed.</p>' +
      '<p class="err" id="werr" role="alert"></p><div id="wdone" role="status"></div>' +
      '<button type="button" class="btn primary" id="wgo">Save and Restart Wi-Fi</button><div id="wpend"></div></div>' +
      '<div class="sec"><h3>Wi-Fi country</h3>' +
      countrySelectHTML('ccountry', 'Sets which 5 GHz channels a USB Wi-Fi adapter may use, by the rules where you are. Until it is set, the bridge Wi-Fi stays on 2.4 GHz.') +
      '<p class="why">With an adapter plugged in, saving can restart the bridge Wi-Fi on 5 GHz. Joined devices drop off for a few seconds and rejoin by themselves.</p>' +
      '<p class="err" id="cerr" role="alert"></p><div id="cdone" role="status"></div>' +
      '<button type="button" class="btn secondary" id="cgo">Save Country</button></div>' +
      '<div class="sec" id="jsec"><h3>Join a home network</h3>' +
      '<div id="jform" style="display:flex;flex-direction:column;gap:8px"><p>At home, the bridge can join your Wi-Fi so it reaches the internet for updates and the clock, and so your laptop can use it without switching networks.</p>' +
      '<label class="fl" for="homessid">Network</label><input type="text" id="homessid" maxlength="32" autocapitalize="off" autocomplete="off" spellcheck="false">' +
      '<label class="fl" for="homepass">Network passphrase</label><input type="password" id="homepass" maxlength="63" autocapitalize="off" autocomplete="off" aria-describedby="homepassh">' +
      '<div class="note" id="homepassh">Leave it empty for an open network.</div>' +
      '<button type="button" class="btn secondary" id="jgo">Join Network</button>' +
      '<p class="note">The bridge Wi-Fi turns off while on a home network. If the join fails (router down, wrong passphrase), the bridge Wi-Fi comes back by itself.</p></div>' +
      '<div id="jback" style="display:flex;flex-direction:column;gap:8px"><p id="jwhere"></p>' +
      '<button type="button" class="btn primary" id="jap">Back to Bridge Wi-Fi</button>' +
      '<p class="note">Anyone on this network can use the bridge. Restarting the bridge also brings its own Wi-Fi back, unless this network is set to rejoin at boot and is in range.</p></div>' +
      '<p class="err" id="jerr" role="alert"></p><div id="jres"></div></div>' +
      '<div class="sec" id="jsaved"><h3>Saved home networks</h3><div id="jlist"></div></div>' +
      '<div class="sec" id="ssec"><h3>SSH login</h3><div id="sstat"></div>' +
      '<div id="sonform" style="display:flex;flex-direction:column;gap:8px"><p>For advanced users: log in to the bridge from a computer with SSH. Turning it on sets a new password for the login account, kept across restarts.</p>' +
      '<label class="fl" for="sshp1">New login password</label><input type="password" id="sshp1" maxlength="64" autocapitalize="off" autocomplete="new-password" aria-describedby="sshph">' +
      '<div class="note" id="sshph">8 to 64 characters.</div>' +
      '<label class="fl" for="sshp2">Type it again</label><input type="password" id="sshp2" maxlength="64" autocapitalize="off" autocomplete="new-password">' +
      '<button type="button" class="btn secondary" id="sshon">Turn SSH On</button></div>' +
      '<button type="button" class="btn secondary" id="sshoff">Turn SSH Off</button>' +
      '<p class="err" id="ssherr" role="alert"></p></div>' +
      '<div class="sec"><h3>Admin PIN</h3><p class="note">The PIN guards every settings change on this bridge, and apps ' +
      'that key or tune the radio need it too. Forgot it? Connect a monitor and keyboard to the bridge and choose ' +
      '"Reset the admin PIN".</p><button type="button" class="btn secondary" id="pinchange">Change Admin PIN</button></div>';
    this.qrFailed = false;
    el.querySelector('#wqr img').onerror = () => { this.qrFailed = true; if (cur && cur.name === 'wifi') this.update(); };
    $('wgo').onclick = async () => {
      $('wdone').innerHTML = '';
      const ssid = await wifiSave($('wgo'), $('werr'));
      if (!ssid) return;
      $('wp1').value = $('wp2').value = '';
      $('wdone').innerHTML = `<p class="head c-ok">${icon('ok', 22)}<span>Saved as ${esc(ssid)}</span></p>` +
        '<p>The Wi-Fi restarts within a minute and this phone drops off. Rejoin the Wi-Fi with the new passphrase, then reopen <b class="mono">vectorbridge.local</b>.</p>';
    };
    $('jgo').onclick = () => {
      const ssid = $('homessid').value.trim();
      $('jerr').textContent = ssid ? '' : 'Type the network name.';
      if (ssid) joinLeave(ssid, {action: 'join', ssid, passphrase: $('homepass').value});
    };
    $('jap').onclick = () => this.join({action: 'ap'});
    $('cgo').onclick = async () => {
      $('cerr').textContent = $('cdone').innerHTML = '';
      if (!$('ccountry').value) { $('cerr').textContent = 'Choose a country.'; return; }
      $('cgo').disabled = true;
      const r = await post('/api/wifi_country', {country: $('ccountry').value});
      if (!$('cgo')) return;  // the sheet closed while the request was out
      $('cgo').disabled = false;
      if (!r.ok) { $('cerr').textContent = (r.errors || [r.error || 'Not saved.']).join(' '); return; }
      if (S.wifi) S.wifi.country = r.country;
      $('cdone').innerHTML = `<p class="head c-ok">${icon('ok', 22)}<span>Saved: ${esc(countryName(r.country))}</span></p>`;
    };
    $('sshon').onclick = () => {
      const pw = $('sshp1').value;
      $('ssherr').textContent = pw.length < 8 ? 'At least 8 characters.' : pw !== $('sshp2').value ? 'The two passwords do not match.' : '';
      if (!$('ssherr').textContent) this.ssh({action: 'on', password: pw});
    };
    $('sshoff').onclick = () => this.ssh({action: 'off'});
    $('pinchange').onclick = () => window.vbAdmin && window.vbAdmin.change();
    $('jlist').onclick = e => {
      const up = e.target.closest('[data-jup]'), home = e.target.closest('[data-jhome]');
      if (up) joinLeave(up.dataset.jup, {action: 'up', ssid: up.dataset.jup});
      else if (home) this.join({action: 'home', ssid: home.dataset.jhome});
    };
    this.timer = setInterval(() => this.poll(), 5000);  // the shell reads /api/join only every 30 s
    this.update();
    this.poll();  // only this sheet reads /api/ssh
  },
  async join(body) {
    $('jerr').textContent = '';
    const r = await post('/api/join', body);
    if (!$('jerr')) return;  // the sheet closed while the request was out
    if (!r.ok) { $('jerr').textContent = (r.errors || [r.error || 'Not saved.']).join(' '); return; }
    // The passphrase stays: the join is only queued, and a failed one should not need it typed again.
    this.poll();
  },
  async ssh(body) {
    $('ssherr').textContent = '';
    const r = await post('/api/ssh', body);
    if (!$('ssherr')) return;  // the sheet closed while the request was out
    if (!r.ok) { $('ssherr').textContent = (r.errors || [r.error || 'Not saved.']).join(' '); return; }
    $('sshp1').value = $('sshp2').value = '';
    this.poll();
  },
  async poll() {
    const [j, x] = await Promise.all([getJSON('/api/join'), getJSON('/api/ssh')]);
    if (j && j.ok) S.join = j;
    if (x && x.ok) S.ssh = x;
    if (cur && cur.name === 'wifi') this.update();
  },
  update() {
    const j = S.join, off = !!(j && !j.on_ap);
    setHTML($('wstat'), wifiStatusHTML());
    setHTML($('wrows'), wifiRowsHTML());
    setHTML($('wqrnote'), `Point the camera at the code to join the Wi-Fi, then open vectorbridge.local or ${esc(off ? '10.42.0.1' : address())}. Both are also printed on the label under the bridge.`);
    $('wqr').style.display = off || this.qrFailed ? 'none' : '';
    setHTML($('wpend'), wifiPendingHTML());
    $('jsec').style.display = $('jsaved').style.display = j ? '' : 'none';
    $('jform').style.display = off ? 'none' : '';
    $('jback').style.display = off ? '' : 'none';
    setHTML($('jwhere'), off ? `The bridge is on ${esc(j.connection || 'another network')}` + (j.ip ? ` at <b class="mono">${esc(j.ip)}</b>` : '') + '. Its own Wi-Fi is off.' : '');
    setHTML($('jres'), joinResultHTML());
    setHTML($('jlist'), joinSavedHTML());
    const x = S.ssh;
    $('ssec').style.display = x ? '' : 'none';
    setHTML($('sstat'), sshHTML());
    $('sonform').style.display = x && !x.on && !x.pending ? '' : 'none';
    $('sshoff').style.display = x && x.on && !x.pending ? '' : 'none';
  },
  close() { clearInterval(this.timer); },
};
""".replace("__COUNTRIES__", " ".join(sorted(CODES)))
