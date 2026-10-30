"""Station sheets on the home page: receive audio, power and display."""

STATION_JS = r"""
// ---- V2-19: receive audio ----
function rxStatus() {  // [tone, icon, text] for the line over the meter
  const c = check('rx_audio'), L = rxLevel();
  if (keyedNow()) return ['dim', 'pause', 'Paused while the radio transmits'];
  if (c && c.state === 'fail') return ['bad', 'fail', c.detail];
  if (c && c.state === 'warn') return ['warn', 'warn', c.detail];
  if (!L) return ['dim', 'busy', 'Waiting for the audio service'];
  return {clip: ['bad', 'warn', 'Audio is clipping'], high: ['warn', 'warn', 'Audio is above the target range'],
    low: ['warn', 'warn', 'Audio is below the target range'], silent: ['dim', 'dot', 'Nothing above the noise floor yet'],
    ok: ['ok', 'ok', 'Audio is arriving from the radio']}[L.state];
}
function rxSetForHTML() {
  const t = S.status;
  return setForHTML([...(t && t.alsa_device ? ['Input: ' + t.alsa_device] : []),
    ...(t && t.sample_rate ? [`${t.sample_rate / 1000} kHz mono, sent to the app every 10 ms`] : []),
    ['Receive pauses while the radio transmits', true]]);
}
SHEETS.receive = {
  render(el) {
    el.innerHTML = sheetHead({id: 's19h', title: 'Receive Audio'}) +
      '<div style="display:flex;flex-direction:column;gap:8px"><div id="rxst" role="status"></div>' + meterHTML('') +
      '<p class="note">The brackets mark the target range. The bar moves with band noise and signals.</p></div>' +
      '<div class="sec"><div style="display:flex;justify-content:space-between;align-items:center"><label class="fl" for="rxg">Receive level</label>' +
      '<span class="mono" id="rxgv" aria-hidden="true">--</span></div>' +
      '<input type="range" id="rxg" min="0" max="100" step="1" value="0">' +
      '<p class="note" id="rxkit"></p><p class="err" id="rxerr" role="alert"></p><p class="note" id="rxsaved" role="status"></p>' +
      '<div class="btnrow"><button type="button" class="btn secondary" id="rxsave">Save to Kit</button>' +
      '<button type="button" class="btn secondary" id="rxlevels">Transmit Level</button>' +
      '<a class="btn secondary" href="/tuner">Open Level Tuner</a></div>' +
      '<p class="note">Level Tuner shows a larger meter with peak and RMS readouts for setting the radio\'s output knob.</p></div>' +
      '<div id="rxset"></div>' +
      '<div class="sec"><h3>If the level drops to nothing</h3><p class="note">Check the cable is in the radio\'s headphone or data jack, not the microphone jack. ' +
      'Then turn up the radio\'s volume or data output level. The home card shows the full list when it happens.</p></div>';
    const g = $('rxg');
    this.touched = 0; this.dragging = false; this.timer = null; this.send = null;
    g.addEventListener('pointerdown', () => { this.dragging = true; });
    const release = () => { this.dragging = false; this.touched = performance.now(); };
    g.addEventListener('pointerup', release); g.addEventListener('pointercancel', release); g.addEventListener('change', release);
    g.addEventListener('input', () => {
      this.touched = performance.now(); this.label(+g.value);
      clearTimeout(this.timer);
      const v = +g.value;
      this.send = async () => {
        this.send = null;
        const r = await post('/api/gain', {rx: v});
        if ($('rxerr')) $('rxerr').textContent = r.ok ? '' : (r.error === 'service_unreachable' ? 'The audio service is not answering, so the level was not set.' : r.error || 'Not set.');
      };
      this.timer = setTimeout(() => this.send && this.send(), 150);
    });
    $('rxlevels').onclick = () => openSheet('levels');
    $('rxsave').onclick = async () => {
      const b = $('rxsave'), kit = S.wizard && S.wizard.kit;
      $('rxerr').textContent = ''; $('rxsaved').textContent = ''; b.disabled = true;
      const r = await post('/api/wizard', {action: 'save_kit', name: kit});
      b.disabled = false;
      if (r.ok) { S.wizard = r; $('rxsaved').textContent = `Saved to kit ${kit}.`; } else $('rxerr').textContent = r.error || 'Not saved.';
    };
    this.update(); this.level();
  },
  label(v) {
    const L = rxLevel(), fixed = rxFixed();
    $('rxgv').textContent = fixed ? 'Set on the radio' : v + '%';
    $('rxg').setAttribute('aria-valuetext', (fixed ? 'set on the radio' : `${v} percent`) + (L ? ', ' + L.label.toLowerCase() : ''));
  },
  level() {
    if (rxFixed() !== this.fixed) this.update();  // known only once the audio service answers
    const L = rxLevel(), g = $('rxg');
    // the audio service's value, except while the thumb is held or was just moved (the post may not have landed yet)
    if (L && typeof L.gain === 'number' && !this.dragging && performance.now() - this.touched > 1500) g.value = L.gain;
    this.label(+g.value);
    const [tone, ic, text] = rxStatus();
    setHTML($('rxst'), `<div class="head c-${tone}">${icon(ic, 22)}<span>${esc(text)}</span></div>`);
    setHTML($('rxset'), rxSetForHTML());
  },
  update() {
    const kit = S.wizard && S.wizard.kit;
    this.fixed = rxFixed();
    $('rxg').disabled = this.fixed;
    $('rxkit').textContent = this.fixed ? RX_FIXED_TEXT + ' ' + RX_RADIO_HINT
      : kit ? `Set by hand. Kit ${kit} restores this value when it is applied. The bridge does not adjust it for you.`
      : 'Set by hand. The bridge does not adjust it for you.';
    $('rxsave').style.display = kit && !this.fixed ? '' : 'none';
  },
  close() { clearTimeout(this.timer); if (this.send) this.send(); },  // a move just before closing still lands
};

// ---- Levels sheet: the TX level slider off the Receive card (REQ-WEB-013) ----
SHEETS.levels = {
  render(el) {
    el.innerHTML = sheetHead({id: 's19l', title: 'Levels', sub: 'Transmit level', back: true}) +
      '<p class="note">The level the bridge sends to the radio. The Drive check in Unlock Transmit sets it for radios that report ALC; ' +
      'set it here by the radio\'s own ALC meter when it does not. The meter below moves while an app transmits.</p>' +
      txSliderHTML() +
      '<div class="sec"><h3>Receive level</h3><p class="note">The receive slider is in Receive Audio.</p></div>';
    txSliderBind(el);
    txPaint();
  },
  back() { openSheet('receive'); },
  update() { txPaint(); },
  level() { txPaint(); },
  close() { txFlush(); },
};

// ---- V2-30: power ----
function fmtUptime(s) {
  const m = Math.floor(s / 60), h = Math.floor(m / 60);
  return h ? `${h} h ${m % 60} min` : m ? `${m} min` : 'Under 1 min';
}
function powerRowsHTML() {
  const b = S.battery || {}, t = S.status;
  const row = (k, v) => `<div class="kv" style="justify-content:space-between;border-bottom:1px solid var(--line)"><dt class="k">${esc(k)}</dt><dd class="v mono">${esc(v)}</dd></div>`;
  let h = '';
  if (!S.battery) h += row('Battery', 'Not reported yet');
  else if (!b.present) h += row('Battery', 'None found');
  else {
    if (typeof b.percent === 'number') h += row('Charge', b.percent + '%');
    if (typeof b.volts === 'number') h += row('Voltage', b.volts.toFixed(2) + ' V');
    h += row('Power source', b.plugged === true ? 'External power' : b.plugged === false ? 'Battery' : 'Not reported until power changes once');
  }
  if (t && typeof t.uptime_s === 'number') h += row('Bridge uptime', fmtUptime(t.uptime_s));
  return h;
}
function powerModeHTML() {
  const m = S.mode;
  const st = m === 'field' ? `<div class="head c-ok">${icon('ok', 22)}<span>Safe to unplug</span></div>`
    : m === 'maintenance' ? `<div class="head c-warn">${icon('warn', 22)}<span>Do not pull power</span></div>` : '';
  return st;
}
SHEETS.power = {
  render(el) {
    el.innerHTML = sheetHead({id: 's30h', title: 'Power'}) + '<div id="pst" role="status"></div>' +
      '<dl id="prows" style="border-top:1px solid var(--line)"></dl>' +
      '<p class="note">The bridge warns at 20% and 10%, and locks transmit at 6%.</p>' +
      '<div class="sec"><h3>Maintenance mode</h3><p>Field mode keeps the card read-only so pulling power is always safe. Maintenance mode makes it writable for updates, logs and kit edits.</p>' +
      '<div style="display:flex;align-items:center;gap:12px;min-height:56px"><div style="flex-grow:1"><div class="t" style="font-weight:600">Maintenance mode</div>' +
      '<div class="note" id="pmsub"></div></div>' +
      '<button type="button" role="switch" id="pmsw" aria-label="Maintenance mode" aria-checked="false" aria-describedby="pmhow" disabled ' +
      'style="width:52px;height:32px;flex:none;border-radius:999px;border:1px solid var(--line);background:var(--line);padding:2px;display:flex;cursor:default">' +
      '<span style="width:26px;height:26px;border-radius:999px;background:#ffffff;display:block"></span></button></div>' +
      '<p class="note" id="pmhow">Change it from a shell on the bridge: <b class="mono">vb-maintenance enter</b> turns it on and <b class="mono">vb-maintenance exit</b> turns it off. ' +
      'Each reboots the bridge. Enter refuses while a phone is on the bridge Wi-Fi or an app is streaming.</p>' +
      '<div class="box"><h3>While it is on</h3><ul><li>An amber "Do not pull power" band shows on every page.</li>' +
      '<li>Transmit stays as it is; receive keeps running.</li><li>Turn it off before packing up. The bridge also leaves it on restart.</li></ul></div></div>' +
      '<div class="sec"><h3>Clock</h3><p class="note">This bridge has no clock battery. Times on this page are shown as "3 min ago", never as a time of day, ' +
      'because the clock only syncs when the bridge reaches the internet.</p></div>';
    this.update(); this.level();
  },
  update() {
    const m = S.mode, sw = $('pmsw');
    setHTML($('pst'), powerModeHTML());
    sw.setAttribute('aria-checked', m === 'maintenance' ? 'true' : 'false');
    sw.style.background = m === 'maintenance' ? 'var(--warn)' : 'var(--line)';
    sw.style.justifyContent = m === 'maintenance' ? 'flex-end' : 'flex-start';
    $('pmsub').textContent = m === 'maintenance' ? 'On. Do not pull power.' : m === 'field' ? 'Off. Field mode, safe to unplug.' : 'Not reported.';
    setHTML($('prows'), powerRowsHTML());
  },
  level() { setHTML($('prows'), powerRowsHTML()); },
};

// ---- V2-34: display ----
SHEETS.display = {
  render(el) {
    el.innerHTML = sheetHead({id: 's34h', title: 'Display'}) +
      '<div style="display:flex;flex-direction:column;gap:8px"><h3 id="apph" style="font-size:17px;line-height:22px;font-weight:600">Appearance</h3>' +
      '<div role="radiogroup" aria-labelledby="apph" id="themes" style="display:flex;flex-direction:column;gap:8px">' + choicesHTML('theme', [
        {value: 'auto', label: 'Match the phone', hint: "Dark or light with the phone's setting"},
        {value: 'light', label: 'Daylight', hint: 'Bright, for full sun in the field'},
        {value: 'dark', label: 'Dark', hint: 'The default at a desk'},
        {value: 'night', label: 'Night Ops', hint: 'Red on black. Keeps night vision. Turn the phone brightness down too.'}], themePref()) + '</div>' +
      '<p class="note">Saved on this phone, not on the bridge. Each phone chooses its own.</p></div>' +
      '<div class="sec"><h3>Text size</h3><p>This page follows the phone\'s text size and browser zoom. At the largest sizes cards stack into one column and nothing is cut off.</p>' +
      '<p class="note">Change it in the phone\'s settings, not here.</p></div>' +
      '<div class="sec"><h3>Quick switch</h3><p class="note">On the home page, the appearance icon next to the battery cycles Daylight, Dark and Night Ops with one tap. No menu needed with gloves on.</p></div>';
    $('themes').addEventListener('change', e => { if (e.target.name === 'theme') setTheme(e.target.value); });
  },
};

// ---- transmit audio time limit (txcap.py) ----
SHEETS.txlimit = {
  render(el) {
    el.innerHTML = sheetHead({id: 'tlh', title: 'Transmit Time Limit'}) +
      '<p>A radio on VOX transmits for as long as audio arrives. If an app gets stuck sending audio, this limit mutes it so the radio stops.</p>' +
      '<div class="sec"><label class="fl" for="tlc">Seconds of audio without a pause</label>' +
      '<input type="number" id="tlc" min="0" max="600" step="1" inputmode="numeric">' +
      '<p class="note" id="tlnow" role="status"></p><p class="err" id="tlerr" role="alert"></p>' +
      '<div class="btnrow"><button type="button" class="btn primary" id="tlsave">Save</button></div></div>' +
      '<div class="sec"><h3>Choosing a value</h3><ul class="note"><li>120 s, the default, fits FT8, FT4, every JS8 speed, JT65 and JT9 (about 50 s) and WSPR (about 110 s).</li>' +
      '<li>Long fldigi messages (Olivia, MFSK) and JS8Call multi-frame messages can run longer. Raise the limit, up to 600, for those.</li><li>0 turns the limit off.</li></ul>' +
      '<p class="note">The limit does two things: at the limit the bridge sends silence until the app pauses for 1 s, and a desktop app keying ' +
      'through port 4532 is unkeyed at the same limit until it sends PTT off. Any 1 s pause starts the audio count again, and digital modes pause between transmissions. ' +
      'While the antenna is unverified, transmissions are cut at 15 s regardless of this limit (see the Transmit sheet).</p></div>';
    this.loaded = false;
    $('tlsave').onclick = async () => {
      const b = $('tlsave'), raw = $('tlc').value.trim(), v = Number(raw);
      $('tlerr').textContent = '';
      if (raw === '' || !Number.isInteger(v) || v < 0 || v > 600) { $('tlerr').textContent = 'Enter a whole number of seconds from 0 to 600.'; return; }
      b.disabled = true;
      const r = await post('/api/tx_audio_cap', {seconds: v});
      b.disabled = false;
      if (r.ok) { if (S.status) S.status.tx_audio_cap_s = v; this.update(); }
      else $('tlerr').textContent = r.error === 'service_unreachable' ? 'The audio service is not answering, so the limit was not saved.' 
        : r.error === 'bad_seconds' ? 'Not saved. Enter a whole number of seconds from 0 to 600.' : r.error || 'Not saved.';
    };
    this.update();
  },
  update() {
    const t = S.status, v = t && typeof t.tx_audio_cap_s === 'number' ? t.tx_audio_cap_s : null;
    if (v !== null && !this.loaded) { $('tlc').value = v; this.loaded = true; }  // never overwrite what the operator is typing
    $('tlnow').textContent = v === null ? 'Waiting for the audio service.'
      : (v === 0 ? 'Now off.' : `Now ${v} s.`) + (t.tx_audio_muted ? ' Muting a transmission that ran past the limit.' : '');
  },
  level() { this.update(); },
};
"""
