"""The Unlock Transmit sheet on the home page: Load, Drive and Antenna (boards V2-20 to V2-25).

All state lives on the Pi (see ladder.py); the sheet renders `GET /api/ladder` and drives it with
`POST /api/ladder`. Drive and carrier are single blocking requests, so the running cards show
only what the page knows while the request is in flight: no live meters and no Stop.
TRANSMIT_JS is appended to the home page script after SHELL_JS and uses its helpers.
"""

TRANSMIT_JS = r"""
// ---- Unlock Transmit sheet (ladder.py) ----
function ulS() { return SHEETS.transmit.s; }
function ulK() { return SHEETS.transmit.k; }
function ulW() { return fmtW(minPower()); }
function ulIsKx2() { const p = profile(); return !!p && p.name === 'kx2'; }
function ulGate(key) { return ((S.ladder && S.ladder.gates) || []).find(g => g.key === key) || {}; }
function ulStep() {  // load | drive | carrier: what the sheet shows now
  const s = ulS(), L = S.ladder;
  if (s.view === 'load') return 'load';
  if (s.view === 'drive' && s.busy !== 'carrier') return 'drive';  // Back to Drive from an antenna failure: raise the level, run Drive again
  if (s.busy === 'drive' || s.busy === 'carrier') return s.busy;
  if (s.res) return s.res.gate === 'drive' ? 'drive' : 'carrier';
  return (L && L.next) || 'load';  // all done: "Test Again" starts over at the load
}
function ulPttUnproven() {  // D17: the keying path is broken; VOX can never be proven, so it runs capped instead
  const c = check('ptt'), p = profile();
  return !!c && c.state === 'fail' && !!p && p.ptt_method !== 'vox';
}
function ulChips(step) {
  const s = ulS(), st = k => {
    if (step === k) return s.res && s.res.gate === k && !s.res.ok && !ulWifi(s.res) ? 'fail'
      : k !== 'load' && ulGate(k).state === 'passed' ? 'done' : 'cur';
    if (k === 'load') return S.ladder && S.ladder.load ? 'done' : 'todo';
    return ulGate(k).state === 'passed' ? 'done' : 'todo';  // a skipped check stays dim
  };
  return stepsHTML('Unlock steps', [['1. Load', st('load')], ['2. Drive', st('drive')], ['3. Antenna', st('carrier')]]);
}
function ulWifi(r) { return !!r && /Wi-Fi is still open/.test(r.detail || ''); }
function ulCard(tone, ic, title, body) {  // tone ok | bad | warn | dim
  const b = {ok: ' b-ok', bad: ' b-bad', warn: ' b-warn', dim: ''}[tone];
  return `<section class="card${b}" role="status"><div class="head c-${tone}">${icon(ic, 22)}<span>${esc(title)}</span></div>${body}</section>`;
}
function ulTicks(items) { return '<ul class="setfor">' + items.map(i => `<li>${icon('tick')}<span>${esc(i)}</span></li>`).join('') + '</ul>'; }
function ulFixes(items) { return '<ul class="fixes">' + items.map(i => `<li>${esc(i)}</li>`).join('') + '</ul>'; }
function ulBtn(act, label, kind, extra) {
  const busy = !!ulS().busy;
  return `<button type="button" class="btn ${kind || 'primary'}" data-ul="${act}"${busy ? ' disabled' : ''}${extra || ''}>${esc(label)}</button>`;
}
function ulPowerNote() {
  return `<p class="why">The radio stays at ${esc(ulW())} after this result. Set your operating power on the radio before calling.</p>`;
}
function ulTone() { return ulS().busy ? icon('busy', 22, 'spin') : ''; }

// -- step 1: frequency and mode, then load (V2-20) --
const UL_OUT = 'outside every amateur band';
const UL_LICENCE = 'The band check is a gross-error check, not a licence check: you remain responsible for the band segment, mode and class privileges your licence allows.';
function ulMhz(hz) { const t = (hz / 1e6).toFixed(6).replace(/0+$/, ''); const [w, f] = t.split('.'); return w + '.' + (f || '').padEnd(3, '0'); }
async function ulReadRadio() {  // the radio's own frequency and mode prefill the box
  const s = ulS(), r = await post('/api/ladder', {action: 'radio'});
  if (r && r.radio) {
    s.radio = r.radio;
    if (s.freq === undefined) s.freq = ulMhz(r.radio.freq_hz);
    if (s.mode === undefined) s.mode = ((S.ladder && S.ladder.data_modes) || []).includes(r.radio.mode) ? r.radio.mode : 'PKTUSB';
  } else s.radio = null;
  ulPaint();
}
function ulRadioHTML() {
  const s = ulS(), L = S.ladder || {};
  if (!L.can_tune) return '';
  if (s.radio === undefined) { s.radio = 'reading'; ulReadRadio(); }
  const rd = s.radio && s.radio !== 'reading' ? s.radio : null;
  const modes = L.data_modes || ['PKTUSB'], mode = s.mode || 'PKTUSB';
  const now = rd ? `The radio is on ${ulMhz(rd.freq_hz)} MHz ${rd.mode}` + (rd.band ? ` (${rd.band}).` : ', outside every amateur band.') : '';
  const outside = !L.any_band && ((rd && !rd.band) || (s.err || '').includes(UL_OUT));
  const t = s.tuned;
  return '<div class="sec" style="border-top:0;padding-top:0"><h3>Frequency and mode</h3>' +
    '<label class="fl" for="ulfreq">Frequency (MHz)</label>' +
    `<input type="text" id="ulfreq" inputmode="decimal" autocomplete="off" spellcheck="false" placeholder="14.074" value="${esc(s.freq || '')}">` +
    '<label class="fl" for="ulmode">Data mode</label><select id="ulmode">' +
    modes.map(m => `<option value="${esc(m)}"${m === mode ? ' selected' : ''}>${esc(m)}</option>`).join('') + '</select>' +
    `<p class="note">${esc(now || (s.radio === 'reading' ? 'Reading the radio.' : 'The radio did not answer with its frequency.'))} ` +
    'PKTUSB is the data mode for HF digital. PKTFM is for VHF and UHF packet.</p>' +
    (t ? `<p class="${t.ok ? 'note' : 'err'}">${esc(t.detail)}</p>` : '') +
    ulBtn('tune', 'Tune Radio', 'secondary') +
    (outside ? '<p class="note">The bridge will not unlock transmit outside the amateur bands. If this frequency is allowed where you are, you can override that.</p>' +
      ulBtn('anyband', 'Transmit Outside the Ham Bands', 'secondary') : '') +
    (L.any_band ? '<p class="why">Transmit outside the amateur bands is allowed for this rig. The rig is marked unverified.</p>' : '') +
    '</div>';
}
function ulLoadHTML() {
  const s = ulS(), L = S.ladder || {}, w = S.wizard || {}, p = profile() || {};
  const set = [];
  if (w.kit) set.push(`Audio drive restored from your ${w.kit} kit`);
  if (p.ptt_method) set.push(`Keying by ${ulK().PTT[p.ptt_method] || p.ptt_method}, checked at setup. One app transmits at a time.`);
  if (typeof L.watchdog_s === 'number') set.push(`The radio stops transmitting if the app goes quiet for ${L.watchdog_s} s`);
  const radio = ulRadioHTML();
  return radio + `<div class="sec"${radio ? '' : ' style="border-top:0;padding-top:0"'}><h3>What is on the antenna jack?</h3>` +
    choicesHTML('ulload', [{value: 'antenna', label: 'Antenna', hint: 'On air'},
      {value: 'dummy', label: 'Dummy Load', hint: 'Bench test, nothing radiated'}], s.load) +
    '<p class="note">Asked once after each power-up. It can never be skipped. Confirming a new load unkeys any app and resets the later steps.</p></div>' +
    '<p class="note">' + esc(UL_LICENCE) + '</p>' +
    '<div class="sec"><h3>Next: drive, then antenna</h3>' +
    `<p class="note">Drive: the bridge keys with its own test tone and steps the audio down until the radio's ALC clears. ` +
    `Antenna: a 2-second carrier at this radio's minimum power, ${esc(ulW())}, ` +
    (L.can_report_swr === false ? 'while you watch the radio&#39;s own SWR meter.' : 'while reading SWR.') + '</p>' +
    ulBtn('confirm', !s.load ? 'Choose What Is Connected' : s.load === 'dummy' ? 'Confirm Dummy Load and Continue' : 'Confirm Antenna and Continue',
      'primary', s.load ? '' : ' disabled') +
    ulBtn('skip', 'Transmit Without Testing', 'secondary', s.load ? '' : ' disabled') +
    `<p class="note">Without the tests, each transmission stops after ${ulK().CAP_S} s and waits ${ulK().CAP_S} s before the next.</p></div>` +
    (set.length ? setForHTML(set) : '');
}

// -- TX level slider (REQ-WEB-013): the Drive step and the Levels sheet share it; one sheet is open at a time --
const txSlider = {timer: null, send: null, touched: 0, dragging: false};
const TX_ERR = {service_unreachable: 'The audio service is not answering, so the level was not set.'};
function txGain() { const t = S.status; return t && typeof t.tx_gain === 'number' ? t.tx_gain : null; }
function txSavedText() {
  const g = txGain(), kit = S.wizard && S.wizard.kit;
  if (g === null) return 'The audio service has not reported the transmit level yet.';
  return `Transmit level ${g}% is saved on the card and used after a restart.` +
    (kit ? ` Save to Kit keeps it in kit ${kit} once the Drive check has passed.` : '');
}
function txMeterHTML() {
  return '<div class="meter" role="meter" aria-label="Transmit audio level" data-tx><div class="track"><div class="fill"></div></div><span class="mlabel mono"></span></div>';
}
function txSliderHTML() {
  const g = txGain(), v = g === null ? 0 : g;
  return '<div class="sec" style="border-top:0;padding-top:0"><div style="display:flex;justify-content:space-between;align-items:center"><label class="fl" for="ultx">Transmit level</label>' +
    `<span class="mono" id="ultxv" aria-hidden="true">${g === null ? '--' : g + '%'}</span></div>` +
    `<input type="range" id="ultx" min="0" max="100" step="1" value="${v}" aria-valuetext="${g === null ? 'unknown' : g + ' percent'}">` +
    txMeterHTML() + `<p class="note" id="ultxsaved">${esc(txSavedText())}</p><p class="err" id="ultxerr" role="alert"></p></div>`;
}
function txPaint() {  // every status poll: the meter, and the slider when the operator is not holding it
  const t = S.status, g = $('ultx');
  if (g) {
    const cur = txGain();
    if (cur !== null && !txSlider.dragging && performance.now() - txSlider.touched > 1500) g.value = cur;
    if ($('ultxv')) $('ultxv').textContent = g.value + '%';
    g.setAttribute('aria-valuetext', g.value + ' percent');
    if ($('ultxsaved')) $('ultxsaved').textContent = txSavedText();
  }
  for (const m of document.querySelectorAll('[data-tx]')) {
    const db = t && typeof t.tx_dbfs === 'number' ? t.tx_dbfs : null;
    const f = m.querySelector('.fill');
    f.style.width = (db === null ? 0 : pos(db)) + '%';
    f.style.background = db !== null && db > -3 ? 'var(--bad)' : db !== null && db > FLOOR ? 'var(--ok)' : 'var(--dim)';
    const text = db === null ? 'No reading' : db <= FLOOR ? 'Silent' : `${db.toFixed(0)} dBFS`;
    m.setAttribute('aria-valuetext', text); m.querySelector('.mlabel').textContent = text;
  }
}
async function txPost(v) {
  txSlider.send = null;
  const r = await post('/api/gain', {tx: v});
  if ($('ultxerr')) $('ultxerr').textContent = r.ok ? '' : (TX_ERR[r.error] || r.error || 'Not set.');
  return r;
}
function txSliderBind(el) {  // el: the sheet; the range input's events bubble to it, so a repaint keeps the handlers
  if (el._txBound) return;  // the same element hosts every sheet: bind once
  el._txBound = true;
  const is = e => e.target && e.target.id === 'ultx';
  el.addEventListener('pointerdown', e => { if (is(e)) txSlider.dragging = true; });
  const release = e => { if (is(e)) { txSlider.dragging = false; txSlider.touched = performance.now(); } };
  el.addEventListener('pointerup', release); el.addEventListener('pointercancel', release); el.addEventListener('change', release);
  el.addEventListener('input', e => {
    if (!is(e)) return;
    const v = +e.target.value;
    txSlider.touched = performance.now();
    if ($('ultxv')) $('ultxv').textContent = v + '%';
    clearTimeout(txSlider.timer);
    txSlider.send = () => txPost(v);
    txSlider.timer = setTimeout(() => txSlider.send && txSlider.send(), 150);  // debounced 150 ms
  });
}
function txFlush() { clearTimeout(txSlider.timer); if (txSlider.send) txSlider.send(); }  // a move just before closing still lands

// -- step 2: drive (V2-21, V2-22) --
const UL_RAISE = 10;  // Raise Level: one tap moves the transmit level this much
const UL_BAND = 'Check the radio is tuned inside a ham band. Most radios will not transmit outside one, for example on 10 MHz (WWV).';
function ulDriveHTML() {
  const s = ulS(), L = S.ladder || {};
  let r = s.res;
  const how = "The bridge plays its own test tone, keys the radio and moves the transmit level up, then down, until the radio's ALC sits just below its onset.";
  if (s.busy === 'drive') return `<section class="card b-ok" role="status"><div class="head">${ulTone()}<span>Checking drive</span></div><div>${esc(how)}</div></section>`;
  const g = txGain(), lvl = g === null ? '' : `${g}%`;
  // Level first, then Retry; Skip Drive Check last, behind a warning (REQ-WEB-032)
  const retry = '<div class="btnrow">' + ulBtn('raise', 'Raise Level', 'secondary', g !== null && g >= 100 ? ' disabled' : '') + ulBtn('drive', 'Retry') + '</div>' +
    ulBtn('skipdrivewarn', 'Skip Drive Check', 'secondary');
  if (r && r.ok) {
    const m = /transmit level (\d+)/.exec(r.detail || ''), gl = m ? m[1] : g;
    const pct = gl === undefined || gl === null ? '' : `${gl}%`;
    return ulCard('ok', 'ok', 'Drive set', `<div>${pct ? `ALC cleared at ${esc(pct)} drive.` : esc(r.detail)}</div>` +
      (pct ? ulTicks([`Drive ${pct}, ALC clear`]) : '') +
      ulBtn('antenna', 'Continue to Antenna Test'));
  }
  if (r && /No drive is reaching/.test(r.detail)) {
    return ulCard('bad', 'fail', 'No drive', `<div>No drive is reaching the radio. The bridge keyed and played its test tone${lvl ? ` up to transmit level ${esc(lvl)}` : ''}, but the radio reported no ALC.</div>` +
      ulFixes([`The transmit level${lvl ? `, now ${esc(lvl)}` : ''}: raise it with the slider or Raise Level, then Retry.`, UL_BAND,
        `Check the radio is in its data mode${ulIsKx2() ? ' (DATA A on the KX2)' : ''}.`,
        "Check the audio cable is in the radio's data or line input jack."]) + txSliderHTML() + retry);
  }
  if (r && /stays pegged|still above its target/.test(r.detail)) {
    return ulCard('bad', 'fail', 'ALC stuck', `<div>ALC stayed above its target${lvl ? ` down to transmit level ${esc(lvl)}` : ''}. The radio's input is being overdriven from somewhere else.</div>` +
      ulFixes([`The transmit level${lvl ? `, now ${esc(lvl)}` : ''}: lower it with the slider, then Retry.`, "Turn down the radio's mic or data gain.",
        "Turn down the interface's TX level knob if it has one."]) + txSliderHTML() + retry);
  }
  if (r && L.can_report_alc === false) r = null;  // the manual card below is the answer for a radio with no ALC readout
  if (r) return ulCard('bad', 'fail', 'Drive check failed', `<div>${esc(r.detail)}</div>` + txSliderHTML() + retry);
  if (L.can_report_alc === false) {  // V2-22 "Radio cannot report ALC": never keyed; the operator sets the level by hand
    const p = profile() || {}, kit = S.wizard && S.wizard.kit;
    return ulCard('dim', 'info', 'Set the transmit level by hand', `<div>${esc(p.display_name || 'This radio')} cannot report ALC over radio control, so the bridge cannot search for the level. ` +
      `Key from an app and set the slider by the radio's own ALC meter${ulIsKx2() ? ': about 5 bars on the KX2' : ''}.</div>` +
      txSliderHTML() + (kit ? `<div class="note">Kit ${esc(kit)} restores this level when it is applied.</div>` : '') +
      ulBtn('skipdrive', 'Continue to Antenna Test'));
  }
  const first = 'Before you start, tune the radio inside a ham band and set it to its data mode' +
    (ulIsKx2() ? ' (DATA A on the KX2)' : '') + '. Most radios will not transmit outside a ham band.';
  const start = typeof L.tx_start_gain === 'number' ? ` The search starts at this radio's level, ${L.tx_start_gain}%.` : '';
  return ulCard('ok', 'dot', 'Drive check', `<div>${esc(how + start)}</div><p class="note">${esc(first)}</p>` + ulBtn('drive', 'Check Drive') + txSliderHTML()) +
    '<div class="sec"><h3>If this radio cannot report ALC</h3><p class="note">The KX2, QMX, uSDX and a Digirig with no radio control cannot report ALC. ' +
    "This step is skipped with a note and the antenna test runs next. Set the transmit level with the slider by the radio's own ALC meter instead.</p>" +
    ulBtn('skipdrive', 'Skip Drive Check', 'secondary') + '</div>';
}

// -- step 3: antenna (V2-23, V2-24) --
function ulCarrierText() {
  return `A 2-second carrier at ${ulW()}, this radio's minimum. The bridge stops at once if SWR reaches 3:1 or the supply sags.`;
}
function ulCarrierHTML() {
  const s = ulS(), r = s.res, L = S.ladder || {};
  const keep = '<p class="note">The bridge plays its own test tone until the carrier ends.</p>';
  if (s.busy === 'carrier') {
    return `<section class="card b-red" role="status"><div class="head c-bad">${ulTone()}<span>Sending test carrier</span></div><div>${esc(ulCarrierText())}</div>` +
      `<div class="meter"><span class="mlabel">Carrier</span><div class="track" id="ulcar" role="progressbar" aria-label="Test carrier" aria-valuemin="0" aria-valuemax="${ulK().CARRIER_S}">` +
      '<div class="fill" style="background:var(--bad)"></div></div><span class="mlabel mono" id="ulcarv"></span></div></section>' + keep;
  }
  const again = ulBtn('carrier', 'Try Again'), both = '<div class="btnrow">' + ulBtn('change', 'Change Load', 'secondary') + again + '</div>';
  if (r && r.ok && r.av) {
    const m = /SWR (\d+(?:\.\d+)?)/.exec(r.detail || ''), x = m ? m[1] : '?';
    return ulCard('ok', 'unlock', 'Unlocked', `<div>SWR ${esc(x)}:1 on the ${L.load === 'dummy' ? 'dummy load' : 'antenna'}. Apps may key the radio with no time cap.</div>` +
      // set by hand: the bridge never read the power, so it has nothing to restore
      ulTicks([`SWR ${x}:1, read from the radio`].concat(r.byhand ? [] : ['Power restored to its earlier setting'])) +
      (r.byhand ? ulPowerNote() : '') +
      '<button type="button" class="btn primary" data-close>Done</button>');
  }
  if (r && r.ok) {  // D8: SWR not read over radio control, so the session stays capped
    const m = /The (.+?) held steady\./.exec(r.detail || ''), cap = L.max_tx_s || ulK().CAP_S;
    const seen = m ? m[1].charAt(0).toUpperCase() + m[1].slice(1) + ' held steady, so nothing looks wrong.' : 'Nothing could be measured.';
    return ulCard('warn', 'warn', 'Passed, capped', `<div>This radio does not report SWR over radio control. ${esc(seen)} Each transmission is capped at ${cap} s.</div>` +
      "<h3 style=\"font-size:15px;line-height:20px;margin-top:6px\">What did the radio's SWR meter show?</h3>" +
      choicesHTML('ulmeter', [{value: 'good', label: 'Under 3:1', hint: 'Unlocks with the time cap kept'},
        {value: 'bad', label: '3:1 or higher, or no reading', hint: 'Fails the test. Fix the antenna first.'},
        {value: 'unsure', label: 'I did not look', hint: 'Stays capped'}], s.meter) +
      ulPowerNote() + ulBtn('meter', 'Confirm', 'primary', s.meter ? '' : ' disabled'));
  }
  if (r && /Power cannot be set over CAT/.test(r.detail)) {
    const p = profile() || {};
    return ulCard('warn', 'warn', 'Set power by hand', "<div>This radio does not take a power setting over radio control. Turn the radio's power to its minimum, " +
      `${esc(ulW())}${p.display_name ? ' on the ' + esc(p.display_name) : ''}, then confirm.</div>` +
      '<div class="btnrow">' + ulBtn('change', 'Cancel', 'secondary') + ulBtn('byhand', `Power Is at ${ulW()}, Send Test`) + '</div>');
  }
  const m = r && /Aborted: SWR (\d+(?:\.\d+)?)/.exec(r.detail);
  if (m) {
    return ulCard('bad', 'fail', `Stopped: SWR ${m[1]}:1`, '<div>The bridge aborted the carrier. The antenna is not matched.</div>' +
      ulFixes(['Check the antenna and feedline connections.', 'Tune the antenna or the tuner for this band.',
        'Try again. Transmit stays locked until a test passes or is skipped.']) + ulPowerNote() + both);
  }
  if (r && /No power left the radio/.test(r.detail)) {
    return ulCard('bad', 'fail', 'No tone', "<div>Forward power stayed near zero. The bridge's test tone is not reaching the radio.</div>" +
      ulFixes([UL_BAND, `Check the radio is in its data mode${ulIsKx2() ? ' (DATA A on the KX2)' : ''}.`,
        "Check the audio cable is in the radio's data or line input jack.",
        'Raise the transmit level: Back to Drive opens the level slider and runs the Drive check again.']) + ulPowerNote() +
      '<div class="btnrow">' + ulBtn('backdrive', 'Back to Drive', 'secondary') + again + '</div>');
  }
  if (r && /supply voltage sagged/.test(r.detail)) {
    return ulCard('bad', 'fail', 'Stopped: supply sagged', '<div>Supply voltage dropped during the carrier. The bridge aborted. The battery or lead cannot carry transmit current.</div>' +
      ulFixes(["Charge or replace the radio's battery.", 'Use a shorter or thicker power lead.', 'Try again.']) + ulPowerNote() + again);
  }
  if (r) return ulCard('bad', 'fail', 'Antenna test failed', `<div>${esc(r.detail)}</div>` + ulPowerNote() + both);
  if (ulPttUnproven()) {  // D17: shown instead of running the carrier
    return ulCard('bad', 'fail', 'Keying not proven', '<div>The bridge cannot read PTT state, or keying has not been proven this session. The test cannot run.</div>' +
      ulFixes(['Open Radio and run Apply and Re-check.', 'If keying is by RTS or DTR, check the serial path exists.',
        'If keying is by VOX, the bridge cannot prove it; the test proceeds capped.']) +
      '<button type="button" class="btn primary" data-sheet="radio">Re-check Keying</button>');
  }
  return ulCard('ok', 'dot', 'Antenna test', `<div>${esc(ulCarrierText())}</div>` + ulBtn('carrier', 'Send Test Carrier')) + keep;
}
function ulWifiHTML(r) {  // D14: the bridge refuses to key while its Wi-Fi is open
  return ulCard('bad', 'fail', 'Wi-Fi is open', '<div>The bridge refuses to key while its Wi-Fi has no passphrase. Secure the Wi-Fi, then unlock.</div>' +
    (r.gate === 'carrier' ? ulPowerNote() : '') +
    `<button type="button" class="btn primary" data-sheet="${SHEETS.secure ? 'secure' : 'wifi'}">Secure This Wi-Fi</button>`);
}

// -- the sheet --
function ulBlockHTML() {  // why nothing can run: shown in place of the step content
  const L = S.ladder;
  if (ulS().busy) return '';  // a blocking check is running: keep its card whatever the polls say
  if (!L) return '<p class="note">Loading.</p>';
  if (L._code !== 200) return ulCard('dim', 'lock', 'Transmit locked', '<div>Transmit stays locked: the bridge has no radio control service to key through.</div>');
  if (rxRed()) {
    return ulCard('bad', 'warn', 'Fix receive first', '<div>Receive audio is not arriving. The bridge will not let an app key the radio until it is, because one cable fault usually breaks both.</div>' +
      '<button type="button" class="btn primary" data-sheet="receive">Open Receive</button>');
  }
  if (battLocked()) return ulCard('bad', 'warn', 'Battery below 6%', '<div>Plug in the bridge to transmit.</div>');
  return '';
}
function ulHTML() {
  const s = ulS(), L = S.ladder || {}, step = ulStep();
  const sub = {load: 'Step 1 of 3', drive: 'Step 2 of 3, drive', carrier: 'Step 3 of 3, antenna'}[step];
  const block = ulBlockHTML();
  const body = block || (s.res && ulWifi(s.res) ? ulWifiHTML(s.res)
    : step === 'load' ? ulLoadHTML() : step === 'drive' ? ulDriveHTML() : ulCarrierHTML());
  return sheetHead({title: 'Unlock Transmit', sub}) + ulChips(step) +
    (L.load === 'dummy' ? '<div class="badges"><span class="badge">Dummy load</span></div>' : '') +
    (s.err ? `<p class="err" role="alert">${esc(s.err)}</p>` : '') +
    (L.record_error ? `<p class="why">${esc(L.record_error)}</p>` : '') + body;
}
function ulPaint() {
  const el = $('sheet');
  if (!el || !cur || cur.name !== 'transmit') return;
  const a = document.activeElement, had = a && el.contains(a) ? a : null;
  if (had && (had.id === 'ulfreq' || had.id === 'ultx')) return;  // never rebuild the box being typed in or the slider being held
  const key = had ? (had.name ? `input[name="${had.name}"][value="${had.value}"]` : had.dataset.ul ? `[data-ul="${had.dataset.ul}"]` : '') : '';
  const before = el._h;
  setHTML(el, ulHTML());
  if (had && el._h !== before) {
    const n = (key && el.querySelector(key)) || el.querySelector('h2');
    if (n) n.focus({preventScroll: true});
  }
  SHEETS.transmit.level();
}
async function ulPost(body) {
  const r = await post('/api/ladder', body);
  if (r && 'tx_allowed' in r) S.ladder = r;
  ulS().err = r && r._code !== 200 ? (r.error || 'The bridge refused that.') : '';
  refresh();
  return r;
}
async function ulRun(action, extra) {  // drive or carrier: one blocking request, the reply carries the result
  const s = ulS();
  s.busy = action; s.res = null; s.err = ''; s.meter = null; s.t0 = performance.now(); ulPaint();
  const tick = action === 'carrier' ? setInterval(() => SHEETS.transmit.level(), 100) : null;
  const r = await ulPost(Object.assign({action}, extra || {}));
  clearInterval(tick); s.busy = null;
  if (r && r.result) s.res = Object.assign({}, r.result, {av: !!r.antenna_verified, byhand: !!(extra && extra.power_by_hand)});
  ulPaint();
}
function ulAntenna() {  // Continue to Antenna Test: straight into the carrier unless keying is known broken
  const s = ulS(); s.res = null; s.view = null;
  if (ulPttUnproven()) ulPaint(); else ulRun('carrier');
}
async function ulAct(act) {
  const s = ulS();
  if (s.busy) return;
  if (act === 'tune') {
    const f = $('ulfreq'), m = $('ulmode');
    if (f) s.freq = f.value.trim();
    if (m) s.mode = m.value;
    const hz = Math.round(parseFloat((s.freq || '').replace(',', '.')) * 1e6);  // a comma-decimal keypad
    if (!(hz > 0)) { s.err = 'Type the frequency in MHz, for example 14.074.'; ulPaint(); return; }
    s.busy = 'tune'; s.err = ''; s.tuned = null; ulPaint();
    const r = await ulPost({action: 'tune', freq_hz: hz, mode: s.mode || 'PKTUSB'});
    s.busy = null;
    if (r && r.radio) s.radio = r.radio;
    if (r && r.result) s.tuned = r.result;
    ulPaint();
  } else if (act === 'anyband') {
    confirmBox({title: 'Transmit outside the ham bands?', lead: 'Only if this frequency is allowed where you are, for example a new allocation the bridge does not know.',
      listTitle: 'What this means', cancel: 'Go Back', ok: 'Allow Outside the Bands',
      items: [esc('The bridge stops checking the frequency for this radio until it restarts or the radio changes.'),
        esc('The rig is marked unverified for the rest of this session.'),
        esc('The choice is written to the card. If the write fails, the home page says so.')],
      onOk: async () => { s.busy = 'anyband'; s.err = ''; ulPaint(); await ulPost({action: 'any_band'}); s.busy = null; ulPaint(); }});
  } else if (act === 'confirm') {
    s.busy = 'load'; s.err = ''; ulPaint();
    const r = await ulPost({action: 'confirm_load', load: s.load});
    s.busy = null;
    if (r && r._code === 200) {
      s.view = null; s.res = null;
      if (r.can_report_alc) { ulRun('drive'); return; }
    }
    if (r && (r.error || '').includes(UL_OUT)) { ulReadRadio(); return; }
    ulPaint();
  } else if (act === 'skip') {
    const cap = ulK().CAP_S;
    confirmBox({title: 'Skip the antenna test?', lead: 'You can transmit now, with limits, instead of testing the antenna.',
      listTitle: 'What this means', cancel: 'Go Back and Test', ok: 'Transmit Without Testing',
      items: [esc('The rig is marked unverified for the rest of this session.'),
        esc(`Each transmission stops after ${cap} s, then the bridge waits ${cap} s before the next.`),
        esc('The skip is written to the card. If the write fails, the home page says so.')],
      onOk: async () => {
        s.busy = 'skip'; s.err = ''; ulPaint();
        const a = await ulPost({action: 'confirm_load', load: s.load});  // the load is never skipped
        const b = a && a._code === 200 ? await ulPost({action: 'override', gates: ['drive', 'carrier']}) : null;
        s.busy = null;
        if (b && b._code === 200) { s.view = null; closeSheet(); } else ulPaint();
      }});
  } else if (act === 'drive') {
    txFlush();
    ulRun('drive');
  } else if (act === 'raise') {  // one step up, posted at once; Retry searches from here
    const g = txGain(); if (g === null) return;
    txSlider.touched = performance.now(); s.busy = 'raise'; ulPaint();
    await txPost(Math.min(100, g + UL_RAISE)); await pollStatus();
    s.busy = null; ulPaint();
  } else if (act === 'skipdrivewarn') {
    confirmBox({title: 'Skip the drive check?', lead: 'No drive reached the radio, so the antenna test that follows will most likely fail too.',
      listTitle: 'What this means', cancel: 'Go Back', ok: 'Skip Drive Check',
      items: [esc('The radio may transmit nothing, or with the level set wrong.'),
        esc('The rig is marked unverified for the rest of this session.'),
        esc('The skip is written to the card. If the write fails, the home page says so.')],
      onOk: () => ulAct('skipdrive')});
  } else if (act === 'skipdrive') {
    txFlush();
    s.busy = 'skip'; ulPaint();
    const r = await ulPost({action: 'override', gates: ['drive']});
    s.busy = null;
    if (r && r._code === 200) ulAntenna(); else ulPaint();
  } else if (act === 'antenna') {
    ulAntenna();
  } else if (act === 'carrier' || act === 'byhand') {
    if (ulPttUnproven()) { s.res = null; ulPaint(); return; }  // shows "Keying not proven" instead
    ulRun('carrier', act === 'byhand' ? {power_by_hand: true} : null);
  } else if (act === 'backdrive') {  // an antenna 'No tone' usually means too little drive: reopen the slider
    s.view = 'drive'; s.res = null; s.err = ''; ulPaint();
  } else if (act === 'change') {
    s.view = 'load'; s.res = null; s.err = ''; ulPaint();
  } else if (act === 'meter') {
    if (s.meter === 'unsure') { closeSheet(); return; }
    const ok = s.meter === 'good';
    s.busy = 'meter'; ulPaint();
    const r = await ulPost({action: 'meter', ok});
    s.busy = null;
    if (r && r.result) {
      if (ok && r.result.ok) { closeSheet(); return; }
      s.res = Object.assign({}, r.result, {av: !!r.antenna_verified}); s.meter = null;
    }
    ulPaint();
  }
}
SHEETS.transmit = {
  k: {CARRIER_S: 2.0,  // ladder.CARRIER_S
    CAP_S: 15,  // ladder.CAPPED_MAX_TX_S and CAPPED_REST_S: the limits a skip or an unread SWR brings
    PTT: {cat: 'radio control', rts: 'RTS', dtr: 'DTR', vox: 'VOX'}},
  s: {view: null, busy: null, res: null, err: '', meter: null, load: null, t0: 0, radio: undefined, freq: undefined, mode: undefined, tuned: null},
  render(el) {
    const s = ulS();
    if (!s.load) s.load = (S.ladder && S.ladder.load) || null;  // no default: what is on the jack is a conscious answer
    el.onclick = e => { const b = e.target.closest('[data-ul]'); if (b && !b.disabled) ulAct(b.dataset.ul); };
    txSliderBind(el);
    el.onchange = e => {
      const t = e.target;
      if (t.id === 'ulfreq') { s.freq = t.value.trim(); return; }
      if (t.id === 'ulmode') { s.mode = t.value; return; }
      if (t.name === 'ulload') s.load = t.value; else if (t.name === 'ulmeter') s.meter = t.value; else return;
      ulPaint();
    };
    ulPaint();
  },
  update() { ulPaint(); },
  level() {  // the carrier's elapsed bar: a local clock, since the request only answers when the carrier is over
    // (ulRun ticks it too: the blocked request and queued polls can starve the status poll)
    txPaint();
    const s = ulS(), bar = $('ulcar');
    if (!bar || s.busy !== 'carrier') return;
    const t = Math.min(ulK().CARRIER_S, (performance.now() - s.t0) / 1000), txt = `${t.toFixed(1)} of ${ulK().CARRIER_S.toFixed(1)} s`;
    bar.firstChild.style.width = (t / ulK().CARRIER_S * 100) + '%';
    bar.setAttribute('aria-valuenow', t.toFixed(1)); bar.setAttribute('aria-valuetext', txt);
    $('ulcarv').textContent = txt;
  },
  close() {
    txFlush();
    const s = ulS(); s.view = null; s.err = ''; s.meter = null; if (!s.busy) s.res = null; s.load = null;
    s.radio = s.freq = s.mode = undefined; s.tuned = null;  // read the radio afresh next time
  },
};
"""
