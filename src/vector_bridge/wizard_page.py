"""The radio setup sheets on the home page: choose a radio, run the setup checks, apply a kit,
and the radio sheet with its Advanced settings.

All state lives on the Pi (see wizard.py); these sheets only render its report and post actions to
/api/wizard. SETUP_JS is appended to the home page script after SHELL_JS (home_page.py) and uses its
helpers. Setup never keys the radio: keying is proven by the transmit checks, after the load question.
"""

SETUP_JS = r"""
// ---- radio setup: choose, "I'm not sure", checks, kits (boards V2-10 to V2-16) ----
const RADIO_TITLE = {kx2: 'Elecraft KX2', qmx: 'QRP Labs QMX', usdx: 'uSDX', 'ft-891': 'Yaesu FT-891', 'ic-705': 'Icom IC-705',
  'digirig-generic': 'Any radio with a Digirig', 'signalink-generic': 'Any radio with a SignaLink'};
const PTT_WORD = {cat: 'radio control', rts: 'RTS', dtr: 'DTR', vox: 'VOX'};
const PTT_LABEL = {cat: 'Radio control (CAT)', rts: 'RTS line', dtr: 'DTR line', vox: 'VOX'};
const IFACE_BODY = {builtin: {interface: 'internal-usb'}, digirig: {interface: 'digirig'}, vox: {interface: 'other', ptt_method: 'vox'}};
const CHECK_ROWS = ['cabling', 'audio_in', 'cat', 'ptt'];  // wizard order; the board draws receive level last
let SU = {view: 'choose', gen: 0, busy: null, fail: null, details: {}, iface: 'builtin', err: '', helpOpen: false,
  bare: false, confirmed: false, ns: {}, nsSug: null, kit: null, kitRes: null};

const radioTitle = r => (r && (RADIO_TITLE[r.name] || r.display_name)) || 'Radio';
const radioShort = r => radioTitle(r).split(' ').pop();  // "Elecraft KX2" -> "KX2"
const suIsSignaLink = p => !!p && p.name === 'signalink-generic';
const suIsVoxCard = p => !!p && p.interface === 'other' && p.ptt_method === 'vox';
function radioSub(r) {
  if (r.name === 'usdx') return `Radio control, ${r.cat_baud} baud, may fall back to VOX. Cannot report SWR`;
  if (!r.rig_model) return 'No radio control' + (r.ptt_method === 'vox' ? '' : ' until a radio is chosen') + `. Keys by ${PTT_WORD[r.ptt_method] || r.ptt_method}`;
  let s = `Radio control, ${r.cat_baud || 'default'} baud`;
  if (r.ptt_method !== 'cat') s += `, keys by ${PTT_WORD[r.ptt_method] || r.ptt_method}`;
  if (!r.can_report_swr) s += '. Cannot report SWR: transmissions capped';
  else if (!r.can_report_alc) s += '. Reports SWR, not ALC';
  return s;
}
function ifaceWord(iface, ptt) {
  return iface === 'internal-usb' ? 'built-in USB' : iface === 'digirig' ? 'Digirig' : 'separate sound card';
}
function kitRadio(k) { return ((S.wizard && S.wizard.radios) || []).find(r => r.display_name === k.radio_name) || {display_name: k.radio_name}; }
function kitSub(k) {
  return [radioTitle(kitRadio(k)), ifaceWord(k.interface, k.ptt_method), 'keys by ' + (PTT_WORD[k.ptt_method] || k.ptt_method)]
    .concat(k.cat_baud ? [String(k.cat_baud)] : []).join(', ');
}
function suNeedsWarning() { return !SU.confirmed && (radioSetUp() || !!(S.wizard && S.wizard.kit)); }
function changeWarning(onOk, onCancel) {  // V2-16
  const kit = S.wizard && S.wizard.kit;
  confirmBox({title: 'Change the radio?', lead: 'Changing the radio, interface or kit re-locks transmit and drops any app that is keying the radio.',
    listTitle: 'What happens next', items: ['Transmit locks again. You will answer the antenna question and test again.',
      'Any app that is keying the radio is unkeyed and loses transmit until it asks again.']
      .concat(kit ? [`The current kit ${esc(kit)} stays saved. Nothing is deleted.`] : []),
    cancel: 'Keep This Radio', ok: 'Change Radio', onOk, onCancel});
}
function suGuard(fn) {  // a confirmation passed in from the radio sheet covers one change only
  if (suNeedsWarning()) changeWarning(fn); else { SU.confirmed = false; fn(); }
}
function suActive() { return cur && cur.name === 'setup'; }
function suShow(view, err) { SU.view = view; SU.err = err || ''; const el = $('sheet'); if (el) suRender(el); }

// -- any radio Hamlib knows: a search over the card's own `rigctl -l`, fetched once --
let HL = null;
async function hlLoad() {
  if (HL === null) { const r = await getJSON('/api/hamlib/models'); if (r && r.ok) HL = r.models || []; }
  return HL || [];
}
function hlSearch(q) {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  return (HL || []).filter(m => { const t = `${m.mfg} ${m.name} ${m.model}`.toLowerCase(); return words.every(x => t.includes(x)); }).slice(0, 25);
}
function hlHTML(id, label) {
  return `<label class="fl" for="${id}">${label}</label>` +
    `<input type="text" id="${id}" autocomplete="off" spellcheck="false" enterkeyhint="search" placeholder="Maker or model, for example 7300">` +
    `<ul class="optlist" id="${id}res" aria-live="polite" style="margin-top:8px"></ul>`;
}
function hlWire(id, onPick) {  // onPick gets the Hamlib model number
  const inp = $(id), ul = $(id + 'res');
  if (!inp || !ul) return;
  inp.oninput = async () => {
    const all = await hlLoad();
    if (!all.length) {  // no list to search: a typed Hamlib model number still works
      const n = inp.value.trim();
      setHTML(ul, /^\d{1,5}$/.test(n) ? `<li><button type="button" class="opt" data-hl="${+n}"><span><span class="ot">Hamlib model ${+n}</span>` +
        `<span class="os">Not checked: the bridge could not read Hamlib\'s list of radios.</span></span>${icon('chev')}</button></li>`
        : '<li class="note">The bridge could not read Hamlib\'s list of radios. Type the radio\'s Hamlib model number instead.</li>');
      return;
    }
    const hits = hlSearch(inp.value);
    setHTML(ul, hits.map(m => `<li><button type="button" class="opt" data-hl="${m.model}"><span><span class="ot">${esc(m.mfg)} ${esc(m.name)}</span>` +
      `<span class="os">Hamlib model ${m.model}${m.status && m.status !== 'Stable' ? ', ' + esc(m.status) : ''}</span></span>${icon('chev')}</button></li>`).join('') ||
      (inp.value.trim() ? '<li class="note">No match. Try only the maker, or the model number.</li>' : ''));
  };
  ul.onclick = e => { const b = e.target.closest('[data-hl]'); if (b) { e.stopPropagation(); onPick(+b.dataset.hl); } };
}

// -- choose (V2-10) --
function chooseHTML() {
  const w = S.wizard || {}, kits = w.kits || [], radios = w.radios || [];
  const opt = (attrs, t, s) => `<li><button type="button" class="opt" ${attrs}><span><span class="ot">${esc(t)}</span>` +
    `<span class="os">${esc(s)}</span></span>${icon('chev')}</button></li>`;
  return (kits.length ? '<div class="sec" style="border-top:0;padding-top:0"><h3>Your kits</h3><ul class="optlist">' +
      kits.map((k, i) => opt(`data-su="kit" data-i="${i}"`, k.name, kitSub(k))).join('') + '</ul>' +
      '<p class="note">A kit restores the radio, interface, keying and audio levels, then re-checks each step.</p>' +
      '<button type="button" class="btn secondary" data-sheet="manage">Rename or Delete Kits</button></div>' : '') +
    '<div class="sec"><h3>Choose a radio</h3><ul class="optlist">' +
    radios.map((r, i) => opt(`data-su="pick" data-i="${i}"`, radioTitle(r), radioSub(r))).join('') + '</ul>' +
    '<p class="note">Each choice sets the keying method and baud rate. You can change both later under Advanced.</p></div>';
}
function suChooseView(el) {
  el.innerHTML = sheetHead({id: 'sheeth', title: 'Set Up a Radio', sub: 'Step 1 of 4. Nothing is sent to the radio yet.'}) +
    '<p class="err" id="suerr" role="alert"></p><div id="sulist" style="display:flex;flex-direction:column;gap:12px"></div>' +
    '<div class="sec"><h3 id="suifh">What carries the audio?</h3><div role="radiogroup" aria-labelledby="suifh" style="display:flex;flex-direction:column;gap:8px">' +
    choicesHTML('iface', [{value: 'builtin', label: 'Built in', hint: 'USB on the radio'}, {value: 'digirig', label: 'Digirig', hint: 'Separate box'},
      {value: 'vox', label: 'Sound card on VOX', hint: 'SignaLink or USB dongle'}], SU.iface) + '</div>' +
    '<p class="note">Chosen separately from the radio. The KX2, QMX, uSDX, FT-891 and IC-705 usually need a Digirig; the IC-705 can use its own USB. ' +
    'On VOX the radio keys itself from the transmit audio and radio control is not used.</p></div>' +
    '<div class="sec"><h3>Not in the list?</h3>' + hlHTML('suhl', 'Search every radio Hamlib knows') +
    '<p class="note">The radio you pick is saved on the bridge, so it is in the list next time.</p></div>' +
    `<div class="sec"><button type="button" class="btn secondary" data-su="notsure">I'm not sure what I have</button></div>`;
  el.querySelectorAll('input[name=iface]').forEach(i => { i.onchange = () => { SU.iface = i.value; }; });
  hlWire('suhl', model => suGuard(() => suChoose({action: 'choose_model', rig_model: model, ...(IFACE_BODY[SU.iface] || {})})));
  suChooseUpdate();
}
function suChooseUpdate() { setHTML($('sulist'), chooseHTML()); setHTML($('suerr'), esc(SU.err)); }
function suPick(r) {
  const body = {action: 'choose', profile: r.name};
  if (r.rig_model) Object.assign(body, IFACE_BODY[SU.iface] || {});
  suGuard(() => suChoose(body));
}
async function suChoose(body) {
  const g = ++SU.gen;
  SU.fail = null; SU.details = {}; SU.busy = 'identify'; SU.helpOpen = false; suShow('checks');
  let r = await post('/api/wizard', body);
  if (g !== SU.gen) return;
  if (!r.ok) { SU.busy = null; suShow('choose', r.error || 'The bridge refused that radio.'); return; }
  S.wizard = r;
  r = await post('/api/wizard', {action: 'update', settings: {cabling_confirmed: true}});
  if (g !== SU.gen) return;
  if (r.ok) S.wizard = r;
  suRun(g);
}

async function suUseVox() {  // the control cable is not there: key by VOX, which turns radio control off
  const g = ++SU.gen;
  SU.fail = null; SU.busy = 'cat'; suPaint();
  const r = await post('/api/wizard', {action: 'update', settings: {ptt_method: 'vox'}});
  if (g !== SU.gen) return;
  if (!r.ok) { SU.busy = null; SU.fail = {step: 'cat', detail: r.error || 'The bridge did not answer.'}; suPaint(); return; }
  S.wizard = r;
  suRun(g);
}

// -- "I'm not sure" (V2-11) --
function nsQuestion(n, title, name, opts) {
  return `<div class="sec" role="radiogroup" aria-labelledby="nsq${n}"><h3 id="nsq${n}">${esc(title)}</h3>` + choicesHTML(name, opts, SU.ns[name]) + '</div>';
}
function suNotSureView(el) {
  const a = ((S.wizard && S.wizard.interview) || {}).answers || {};
  if (!SU.ns.q1 && a.usb_count) SU.ns = {q1: {one: '1', box: '1', two: '2', none: '0'}[a.usb_count], q2: a.usb_count === 'box' ? 'yes' : undefined,
    q3: a.box_label};
  el.innerHTML = sheetHead({id: 'sheeth', title: 'What do you have?', sub: 'Three questions. The answer suggests an interface and keying method.', back: true}) +
    nsQuestion(1, '1. How many USB cables run from the bridge to the radio side?', 'q1',
      [{value: '1', label: 'One'}, {value: '2', label: 'Two'}, {value: '0', label: 'None', hint: 'Only audio leads'}]) +
    nsQuestion(2, '2. Is there a small box between the bridge and the radio?', 'q2',
      [{value: 'yes', label: 'Yes', hint: 'A box with its own jacks'}, {value: 'no', label: 'No', hint: 'Cables go straight in'}]) +
    nsQuestion(3, '3. What does the box say on it?', 'q3',
      [{value: 'digirig', label: 'Digirig', hint: 'Small aluminium box'}, {value: 'signalink', label: 'SignaLink', hint: 'Blue or black box with knobs'},
       {value: 'other', label: 'Something else', hint: 'Or no name'}, {value: 'none', label: 'No box'}]) +
    '<p class="err" id="suerr" role="alert"></p><div id="nssug" aria-live="polite"></div>';
  el.querySelectorAll('input[type=radio]').forEach(i => { i.onchange = () => nsAnswer(i.name, i.value); });
  SU.nsSug = ((S.wizard && S.wizard.interview) || {}).suggestion || null;
  nsPaint();
}
async function nsAnswer(q, v) {
  SU.ns[q] = v;
  const n = SU.ns, sends = [];
  const usb = n.q1 === '1' ? (n.q2 === 'yes' ? 'box' : 'one') : {2: 'two', 0: 'none'}[n.q1];
  if ((q === 'q1' || q === 'q2') && usb) sends.push(['usb_count', usb]);
  if (q === 'q2' && v === 'no') sends.push(['box_label', 'none']);
  if (q === 'q3') sends.push(['box_label', v]);
  for (const [key, value] of sends) {
    const r = await post('/api/wizard', {action: 'answer', key, value});
    if (!r.ok) { setHTML($('suerr'), esc(r.error)); return; }
    SU.nsSug = r.suggestion || null;
  }
  setHTML($('suerr'), ''); nsPaint();
}
function nsPaint() {
  const s = SU.nsSug, el = $('nssug');
  if (!el) return;
  if (!s) { setHTML(el, ''); return; }
  if (s.ptt_method === 'vox') {
    setHTML(el, '<section class="card b-ok"><div class="head c-ok">Looks like a SignaLink</div>' +
      '<p>A SignaLink carries audio and keys the radio when it hears a tone (VOX). The bridge cannot talk to the radio through it, so frequency and mode will not be shown and keying is set to VOX.</p>' +
      '<p class="note">If the radio also has its own USB port, plug that in too and choose the radio itself instead, so the bridge can add radio control.</p>' +
      '<button type="button" class="btn primary" data-su="signalink">Continue with SignaLink</button></section>');
    return;
  }
  setHTML(el, `<section class="card b-ok"><div class="head c-ok">${esc(s.why)}</div>` +
    (s.advice || []).map(a => `<p class="note">${esc(a)}</p>`).join('') +
    `<button type="button" class="btn primary" data-su="nsback" data-v="${s.interface === 'internal-usb' ? 'builtin' : s.interface === 'digirig' ? 'digirig' : SU.iface}">Choose the Radio</button></section>`);
}

// -- checks (V2-12, V2-13, V2-15) --
async function suRun(g) {
  g = g || ++SU.gen;
  SU.fail = null; SU.helpOpen = false; SU.kitErr = ''; SU.finding = false;
  if (!S.wizard) { const w = await getJSON('/api/wizard'); if (w && w.ok) S.wizard = w; }
  for (let n = 0; n < 6 && S.wizard && S.wizard.step !== 'ladder'; n++) {
    const step = S.wizard.step;
    SU.busy = step; suPaint();
    const r = await post('/api/wizard', {action: 'advance'});
    if (g !== SU.gen) return;
    if (!r.ok) { SU.fail = {step, detail: r.error || 'The bridge did not answer.'}; break; }
    S.wizard = r; SU.details[step] = r.result.detail;
    if (!r.result.ok) { SU.fail = {step, detail: r.result.detail, log: r.result.log}; break; }
  }
  if (g !== SU.gen) return;
  SU.busy = null; suPaint();
}
// The baud search: the CAT step stays busy for up to ~30 s, then the remaining checks run on.
async function suFindBaud() {
  const g = ++SU.gen;
  SU.fail = null; SU.helpOpen = false; SU.busy = 'cat'; SU.finding = true; suPaint();
  const r = await post('/api/wizard', {action: 'find_baud'});
  if (g !== SU.gen) return;
  SU.busy = null; SU.finding = false;
  if (!r.ok) SU.fail = {step: 'cat', detail: r.error || 'The bridge did not answer.'};
  else {
    S.wizard = r; SU.details.cat = r.result.detail;
    if (!r.result.ok) SU.fail = {step: 'cat', detail: r.result.detail, log: r.result.log};
  }
  if (SU.fail) { suPaint(); return; }
  suRun(g);
}
function rowState(step) {
  const w = S.wizard || {};
  if (SU.busy === step) return 'busy';
  if (SU.fail && SU.fail.step === step) return 'fail';
  return (w.passed || []).includes(step) ? 'ok' : 'todo';
}
function checkRow(o) {  // {state, title, detail, extra}
  const ic = {ok: icon('ok', 22, 'c-ok'), fail: icon('fail', 22, 'c-bad'), busy: icon('busy', 22, 'spin'), todo: icon('dot', 22, 'c-dim')}[o.state];
  const dim = o.state === 'todo' ? ' c-dim' : '';
  return `<li>${ic}<div><div class="ct${dim}">${esc(o.title)}</div><div class="cd">${esc(o.detail)}</div>${o.extra || ''}</div></li>`;
}
function helpList(step) {
  const s = ((S.wizard && S.wizard.steps) || []).find(x => x.key === step);
  return s ? s.help : [];
}
// rigctld's own last words under a CAT failure: a wrong port, a busy port or a bad model each say so here.
function hlLog(lines) {
  return lines && lines.length ? '<details class="hlog"><summary>Details from Hamlib</summary><pre class="mono">' +
    esc(lines.join('\n')) + '</pre></details>' : '';
}
function fixList(items) { return '<ol class="fixes">' + items.map(i => `<li>${i}</li>`).join('') + '</ol>'; }
function suRow(step, p) {
  const st = rowState(step), det = SU.details[step] || '', fail = SU.fail && SU.fail.step === step ? SU.fail.detail : '';
  const alsa = S.status && S.status.alsa_device ? String(S.status.alsa_device) : '';
  const dev = p.cat_device, sig = suIsSignaLink(p);
  const generic = st === 'fail' ? {detail: fail, extra: fixList(helpList(step).map(esc))} : {};
  if (step === 'cabling') {
    if (st === 'ok') return checkRow({state: st, title: 'Cables in', detail: alsa ? `USB audio adapter found: ${alsa}.` : det});
    return checkRow({state: st, title: 'Cables in', detail: st === 'busy' ? 'Looking for the USB audio adapter.' : 'Checks the USB audio adapter is there.', ...generic});
  }
  if (step === 'audio_in') {
    if (st === 'ok') {
      const g = S.status && typeof S.status.rx_gain === 'number' ? S.status.rx_gain : null;
      const where = (rxLevel() || {}).state === 'ok' ? 'inside the brackets' : 'audio moving';
      if (rxFixed()) return checkRow({state: st, title: 'Receive level', detail: `${where === 'audio moving' ? 'Audio moving' : 'Audio inside the brackets'}. ${RX_FIXED_TEXT}`});
      return checkRow({state: st, title: 'Receive level saved', detail: g !== null ? `Level ${g}%, ${where}. Change it later in Receive Audio.` : det});
    }
    return checkRow({state: st, title: 'Receive level', detail: st === 'busy' ? 'Listening to the receive audio.' : 'Checked after the cables.', ...generic});
  }
  if (step === 'cat') {
    if (!p.rig_model && st !== 'fail') return checkRow({state: st, title: 'Radio control not used',
      detail: `${sig ? 'A SignaLink has no control line.' : p.ptt_method === 'vox' ? 'Keying by VOX, so radio control is not used.' : 'This setup has no control line.'}` +
        ' Frequency, mode, SWR and power cannot be read. This is normal, not a fault.'});
    if (st === 'ok') return checkRow({state: st, title: 'Radio control answers',
      detail: `${radioTitle(p)} replied${dev ? ' on ' + dev : ''} at ${p.cat_baud || 'its default'} baud.`});
    if (st === 'busy') return checkRow({state: st, title: 'Asking the radio', detail: SU.finding
      ? `Trying each baud rate in turn${dev ? ' on ' + dev : ''}, up to 30 s. Never keys the radio.` : `Waiting for a reply${dev ? ' on ' + dev : ''}.`});
    if (st === 'todo') return checkRow({state: st, title: 'Radio control', detail: 'Checked after the receive level.'});
    const baud = `Check the radio's baud rate is <b class="mono">${esc(p.cat_baud || 'its default')}</b>${p.name === 'kx2' ? ' (KX2 menu RS232)' : ''}.`;
    return checkRow({state: st, title: 'Radio control does not answer', detail: fail, extra: fixList(['Check the radio is switched on.',
      "Check the control cable is in the radio's ACC or USB jack, not the microphone jack.", baud]) +
      '<div class="btnrow" style="margin-top:8px"><button type="button" class="btn primary" data-su="retry">Try Again</button>' +
      (p.rig_model ? '<button type="button" class="btn secondary" data-su="findbaud">Find It for Me</button>' : '') +
      `<button type="button" class="btn secondary" data-sheet="radio" data-arg='{"advanced":true}'>Pick Port or Baud</button></div>` +
      '<p class="note" style="margin-top:6px">Find It for Me tries each common baud rate until the radio answers, up to 30 s. With the port on Automatic the bridge uses the one USB serial device plugged in. If there are two, pick the radio port under Advanced.</p>' +
      hlLog(SU.fail && SU.fail.step === step ? SU.fail.log : null)});
  }
  if (p.ptt_method === 'vox' && st !== 'fail') return checkRow({state: st, title: 'Keying by VOX',
    detail: sig ? 'Nothing to check. The SignaLink keys the radio when it hears audio. Set its DLY knob low so the radio unkeys quickly.'
      : 'Nothing to check. The radio keys itself when it hears audio. Turn VOX on at the radio.'});
  if (st === 'ok') return checkRow({state: st, title: 'Keying set', detail: (p.ptt_method === 'cat'
    ? 'Radio control. The radio reports receive, not already keyed.' : `${PTT_LABEL[p.ptt_method] || p.ptt_method}${dev ? ' on ' + dev : ''}.`) +
    ' Keying itself is proven in Unlock Transmit, after you confirm the load. Setup never transmits.'});
  return checkRow({state: st, title: 'Keying', detail: st === 'busy' ? 'Checking the keying settings. Never keys the radio.'
    : 'Settings checked after radio control answers. Never keys the radio.', ...generic});
}
function suChecksSub(p) {
  const w = S.wizard || {};
  if (SU.fail) return `Stopped at ${STEP_WORD[SU.fail.step] || SU.fail.step}.`;
  if (SU.busy) return 'Checking each step. Setup never keys the radio.';
  if (w.kit_ready) return p && !p.rig_model ? `Radio control not used. Keys by ${PTT_WORD[p.ptt_method] || p.ptt_method}.` : 'All four checks passed.';
  return `Setup stopped at ${STEP_WORD[w.step] || 'radio control'}.`;
}
function suChecksView(el) {
  el.innerHTML = '<div id="suhead"></div><div id="subody" style="display:flex;flex-direction:column;gap:12px"></div><div id="sukit" hidden></div>';
  suPaint();
}
function suPaint() {
  if (!suActive()) return;
  if (SU.view === 'checks') suChecksPaint(); else if (SU.view === 'apply') suApplyPaint();
}
function suChecksPaint() {
  const w = S.wizard || {}, p = profile() || {};
  if (!$('subody')) return;
  setHTML($('suhead'), sheetHead({id: 'sheeth', title: p.name ? radioTitle(p) : 'Set Up a Radio', sub: suChecksSub(p), back: true}));
  const idFail = SU.fail && !CHECK_ROWS.includes(SU.fail.step);
  const catFail = SU.fail && SU.fail.step === 'cat' && p.rig_model;
  const helpStep = SU.fail ? SU.fail.step : w.step;
  const help = helpList(helpStep);
  setHTML($('subody'), stepsHTML('Setup progress', setupSteps(!!SU.fail).items) +
    (idFail ? `<p class="err" role="alert">${esc(SU.fail.detail)}</p>` : '') +
    (SU.kitErr && !w.kit_ready ? `<p class="err" role="alert">${esc(SU.kitErr)}</p>` : '') +
    '<ol class="checks" aria-label="Setup checks" aria-live="polite">' + CHECK_ROWS.map(s => suRow(s, p)).join('') + '</ol>' +
    (!SU.busy && !SU.fail && !w.kit_ready && w.step !== 'ladder' ? '<button type="button" class="btn primary" data-su="retry">Continue Checks</button>' : '') +
    (help.length && !w.kit_ready ? `<div class="sec"><button type="button" class="btn secondary" data-su="help" aria-expanded="${SU.helpOpen}">This isn't working</button>` +
      (SU.helpOpen ? '<div class="box"><h3>Likely causes</h3><ul>' + help.map(h => `<li>${esc(h)}</li>`).join('') + '</ul></div>' : '') + '</div>' : '') +
    (catFail ? `<div class="sec"><p class="note">Keying by ${esc(PTT_WORD[p.ptt_method] || p.ptt_method)} needs the ${esc(radioShort(p))}'s control cable. ` +
      'To run without radio control, key by VOX instead, or choose "Any radio with a Digirig" or "Any radio with a SignaLink".</p>' +
      '<button type="button" class="btn primary" data-su="usevox">Key by VOX Instead</button>' +
      '<button type="button" class="btn secondary" data-su="nocontrol" aria-expanded="' + !!SU.bare + '">Choose a Radio Without Control</button>' +
      (SU.bare ? '<ul class="optlist">' + ((w.radios || []).map((r, i) => [r, i]).filter(([r]) => !r.rig_model).map(([r, i]) =>
        `<li><button type="button" class="opt" data-su="pick" data-i="${i}"><span><span class="ot">${esc(radioTitle(r))}</span>` +
        `<span class="os">${esc(radioSub(r))}</span></span>${icon('chev')}</button></li>`).join('')) + '</ul>' : '') +
      '<p class="note">Without radio control the bridge cannot read frequency, mode, SWR or power, and every transmission is capped at 15 s.</p></div>' : ''));
  const kit = $('sukit'), show = !!w.kit_ready && !SU.busy;
  if (show && !kit.innerHTML) {
    const name = w.kit || (p.rig_model ? `${radioShort(p)} Field` : `${radioShort(p)} Kit`).replace(/[^A-Za-z0-9 _-]/g, '').trim().slice(0, 40);
    kit.innerHTML = '<div class="sec"><h3>Save as a kit</h3><label class="fl" for="kitname">Kit name</label>' +
      `<input type="text" id="kitname" maxlength="40" autocomplete="off" value="${esc(name)}">` +
      '<div class="note">Saves the radio, interface, keying, baud and levels. Pick this kit next time and the bridge sets everything up again.</div>' +
      '<p class="err" id="kiterr" role="alert"></p>' +
      '<button type="button" class="btn primary" data-su="savekit">Save Kit and Finish</button>' +
      '<button type="button" class="btn secondary" data-close>Finish Without Saving</button>' +
      (p.rig_model ? '' : '<p class="note">On the home page the Radio tile will read "Not used (VOX)" in grey. Every transmission is capped at 15 s because the bridge cannot read SWR.</p>') +
      '</div>';
  }
  kit.hidden = !show;
}
async function suSaveKit(btn) {
  const name = $('kitname').value.trim();
  btn.disabled = true;
  const r = await post('/api/wizard', {action: 'save_kit', name});
  btn.disabled = false;
  if (!r.ok) { SU.kitErr = r.error; setHTML($('kiterr'), esc(r.error)); if (r._code === 400) refresh(); return; }
  S.wizard = r; closeSheet();
}

// -- apply a kit (V2-14) --
const APPLY_ROWS = [['identify', '1. Settings'], ['cabling', '2. Cables'], ['cat', '3. Control'], ['ptt', '4. Keying'], ['audio_in', '5. Levels']];
async function suApply(k) {
  const g = ++SU.gen;
  SU.kit = k; SU.kitRes = null; SU.view = 'apply';
  const el = $('sheet'); if (el) suRender(el);
  const r = await post('/api/wizard', {action: 'apply_kit', name: k.name});
  if (g !== SU.gen) return;
  if (r.ok) S.wizard = r;
  SU.kitRes = r.ok ? (r.step === 'ladder' || (r.result && r.result.ok) ? {ok: true} : {ok: false, step: r.step, detail: (r.result || {}).detail, log: (r.result || {}).log})
    : {ok: false, step: 'identify', detail: r.error};
  suPaint();
}
function suApplyPaint() {
  if (!$('subody')) return;
  const k = SU.kit, res = SU.kitRes, w = S.wizard || {}, passed = w.passed || [], p = profile() || {};
  const alsa = S.status && S.status.alsa_device ? String(S.status.alsa_device) : '';
  const lv = k.levels && typeof k.levels.rx_gain === 'number' ? `, receive level ${k.levels.rx_gain}%` : '';
  const stateOf = s => !res ? 'busy' : passed.includes(s) ? 'ok' : !res.ok && res.step === s ? 'fail' : 'todo';
  const rows = {
    identify: st => ({title: st === 'ok' ? 'Settings loaded' : 'Settings', detail: `${radioTitle(kitRadio(k))} with ${ifaceWord(k.interface, k.ptt_method)}, ` +
      `keys by ${PTT_WORD[k.ptt_method] || k.ptt_method}${k.cat_baud ? ', ' + k.cat_baud + ' baud' : ''}${lv}.`}),
    cabling: st => ({title: 'Cables in', detail: st === 'ok' && alsa ? `USB audio adapter found: ${alsa}.` : 'Looks for the USB audio adapter.'}),
    cat: st => (!p.rig_model && res ? (st === 'fail' ? {title: 'Keying line not open', detail: 'Checks the serial line that keys the radio.'}
        : {title: 'Radio control not used', detail: 'This kit has no radio control. Nothing to check.'})
      : {title: st === 'ok' ? 'Radio control answers' : st === 'fail' ? 'Radio control does not answer' : 'Asking the radio',
         detail: st === 'ok' ? `${radioTitle(p)} replied${p.cat_device ? ' on ' + p.cat_device : ''} at ${p.cat_baud || 'its default'} baud.`
           : `Waiting for a reply${p.cat_device ? ' on ' + p.cat_device : ''}.`}),
    ptt: st => ({title: st === 'ok' ? (p.ptt_method === 'vox' ? 'Keying by VOX' : 'Keying set') : 'Keying',
      detail: st === 'ok' ? 'Keying settings checked. Never keys.' : 'Checks the keying settings and that the radio reports receive. Never keys.'}),
    audio_in: st => ({title: st === 'ok' ? 'Levels restored' : 'Levels',
      detail: st === 'ok' ? 'Receive level and audio drive restored from the kit.' : 'Restores receive level and audio drive from the kit.'}),
  };
  const sub = !res ? 'Re-checking each step. About 10 s.' : res.ok ? 'Every step passed.' : `Stopped at ${STEP_WORD[res.step] || res.step}.`;
  setHTML($('suhead'), sheetHead({id: 'sheeth', title: `Applying ${k.name}`, sub}));
  setHTML($('subody'), stepsHTML('Applying kit', APPLY_ROWS.map(([s, t]) => [t, stateOf(s) === 'ok' ? 'done' : stateOf(s) === 'fail' ? 'fail' : stateOf(s) === 'busy' ? 'cur' : 'todo'])) +
    '<ol class="checks" aria-label="Kit checks" aria-live="polite">' + APPLY_ROWS.map(([s]) => {
      const st = stateOf(s), o = rows[s](st);
      if (st === 'fail') { o.detail = res.detail || ''; o.extra = fixList(helpList(s).map(esc)) + (s === 'cat' ? hlLog(res.log) : ''); }
      return checkRow({state: st, ...o});
    }).join('') + '</ol>' +
    (res && !res.ok && !APPLY_ROWS.some(([s]) => s === res.step) ? `<p class="err" role="alert">${esc(res.detail)}</p>` : '') +
    '<p class="note">Stops at the first check that fails and shows what to fix. Transmit stays locked until you answer the antenna question.</p>' +
    (!res ? '<button type="button" class="btn secondary" data-close>Cancel</button>'
      : res.ok ? '<button type="button" class="btn primary" data-close>Done</button>'
      : '<div class="btnrow"><button type="button" class="btn primary" data-su="reapply">Try Again</button><button type="button" class="btn secondary" data-close>Close</button></div>'));
}

// -- the setup sheet --
function suRender(el) {
  if (SU.view === 'notsure') suNotSureView(el);
  else if (SU.view === 'checks' || SU.view === 'apply') suChecksView(el);
  else suChooseView(el);
  const h = el.querySelector('h2'); if (h) h.focus({preventScroll: true});
}
function suClick(e) {
  const b = e.target.closest('[data-su]');
  if (!b) return;
  const w = S.wizard || {}, a = b.dataset.su, i = +b.dataset.i;
  if (a === 'pick') suPick((w.radios || [])[i]);
  else if (a === 'kit') { const k = (w.kits || [])[i]; if (k) suGuard(() => suApply(k)); }
  else if (a === 'notsure') suShow('notsure');
  else if (a === 'nsback') { SU.iface = b.dataset.v; suShow('choose'); }
  else if (a === 'signalink') { const r = (w.radios || []).find(x => x.name === 'signalink-generic'); if (r) suGuard(() => suChoose({action: 'choose', profile: r.name})); }
  else if (a === 'retry') suRun();
  else if (a === 'findbaud') suFindBaud();
  else if (a === 'reapply') suApply(SU.kit);
  else if (a === 'help') { SU.helpOpen = !SU.helpOpen; suPaint(); }
  else if (a === 'nocontrol') { SU.bare = !SU.bare; suPaint(); }
  else if (a === 'usevox') suUseVox();
  else if (a === 'savekit') suSaveKit(b);
}
SHEETS.setup = {
  render(el, arg) {
    const w = S.wizard || {}, p = profile();
    SU = {...SU, gen: SU.gen + 1, busy: null, finding: false, fail: null, details: {}, err: '', kitErr: '', helpOpen: false, bare: false, ns: {}, nsSug: null,
      confirmed: !!arg.confirmed, kit: null, kitRes: null};
    SU.iface = arg.iface || (p ? (suIsVoxCard(p) ? 'vox' : p.interface === 'digirig' ? 'digirig' : 'builtin') : 'builtin');
    SU.view = arg.choose ? 'choose' : arg.resume ? 'checks' : !p ? 'choose' : !w.kit_ready ? 'checks' : 'choose';
    el.onclick = suClick;
    suRender(el);
    if (arg.resume) suRun(SU.gen);
  },
  update() {
    if (SU.view === 'choose') suChooseUpdate();
    else suPaint();
  },
  back() { ++SU.gen; SU.busy = null; suShow('choose'); },
  close() { ++SU.gen; },
};

// ---- the radio sheet (V2-17) ----
let RS = {adv: false, err: '', kitMsg: ''};
function rsStatus(p) {
  if (!p.rig_model) return '<div class="head c-dim">Radio control not used (VOX)</div>';
  const c = check('cat');
  if (c && c.state === 'ok') return `<div class="head c-ok">${icon('ok', 22)}<span>Connected by radio control</span></div>`;
  if (c && c.state === 'fail') return `<div class="head c-bad">${icon('fail', 22)}<span>Radio control not answering</span></div>`;
  return c ? `<div class="head c-warn">${icon('warn', 22)}<span>${esc(c.detail)}</span></div>` : '';
}
function rsBody(p) {
  const w = S.wizard || {}, c = check('cat'), pt = check('ptt');
  const keyed = !!(S.ladder && (S.ladder.gates || []).some(g => g.key !== 'load' && g.state === 'passed'));
  const proven = !!(pt && pt.state === 'ok' && keyed);
  const kv = (k, v) => `<div class="kv"><div><dt class="k">${esc(k)}</dt><dd class="v">${esc(v)}</dd></div></div>`;
  const set = [`You chose ${radioTitle(p)}.` + (p.rig_model && c && c.state === 'ok' ? ' The radio confirmed it by answering on the control cable.' : '')];
  if (p.rig_model) set.push(`Radio control at ${p.cat_baud || 'the default'} baud` + (p.interface === 'digirig' ? ' over the Digirig' : ''));
  else set.push(['No radio control, so frequency, mode, SWR and power cannot be read']);
  if (!p.can_report_alc) set.push(['This radio cannot report audio drive (ALC), so the drive check is skipped']);
  if (!p.can_report_swr) set.push(['This radio cannot report SWR, so transmissions are capped']);
  return rsStatus(p) +
    '<dl style="border-top:1px solid var(--line)">' +
    kv('Keying', (PTT_LABEL[p.ptt_method] || p.ptt_method).replace(' (CAT)', '') + (proven ? ', proven' : ', checked at setup')) +
    kv('Test power', typeof minPower() === 'number' ? `${fmtW(minPower())} (this radio's minimum)` : "This radio's minimum") + '</dl>' +
    setForHTML(set) +
    '<div class="sec">' + (w.kit ? `<div class="kv"><div><div class="k">Kit</div><div class="v">${esc(w.kit)}</div></div></div>` +
      '<button type="button" class="btn secondary" data-rs="savekit">Save Changes to Kit</button>' +
      `<p class="err" role="alert">${esc(RS.kitMsg)}</p>` : '') +
    '<button type="button" class="btn secondary" data-rs="another">Use Another Radio</button></div>';
}
// The sound card: Automatic picks the one on the radio's USB connection, else the first.
function rsCardHTML() {
  const w = S.wizard || {}, cards = w.sound_cards || [], cur = w.audio_card || {};
  if (cards.length < 2 && !cur.usb_id && !cur.usb_path) return '';  // one card: nothing to choose
  const key = c => `${c.usb_path}@${c.usb_id}`, curKey = cur.usb_id || cur.usb_path ? `${cur.usb_path}@${cur.usb_id}` : '';
  const known = !curKey || cards.some(c => key(c) === curKey);
  return '<label class="fl" for="rscard">Sound card</label><select id="rscard">' +
    `<option value=""${curKey ? '' : ' selected'}>Automatic</option>` +
    cards.map(c => `<option value="${esc(key(c))}"${key(c) === curKey ? ' selected' : ''}>${esc(c.name)} (USB ${esc(c.usb_path || c.usb_id)})</option>`).join('') +
    (known ? '' : `<option value="${esc(curKey)}" selected>Chosen card, not plugged in</option>`) + '</select>' +
    '<div class="note">Automatic uses the card on the same USB connection as the radio\'s control cable, else the first one.</div>';
}
// The serial ports the Pi sees, by their USB names, plus a typed path for anything else.
function rsPortHTML(p) {
  const ports = (S.wizard && S.wizard.serial_ports) || [], cur = p.cat_device || '';
  const typed = !!cur && !ports.some(o => o.path === cur);
  const sel = (v) => ((typed ? v === 'type' : v === cur) ? ' selected' : '');
  return '<label class="fl" for="rsportsel">Serial port</label><select id="rsportsel">' +
    `<option value=""${sel('')}>Automatic${ports.length === 1 ? ' (' + esc(ports[0].label) + ')' : ''}</option>` +
    ports.map(o => `<option value="${esc(o.path)}"${sel(o.path)}>${esc(o.label)}${o.sound_card ? ', with sound card ' + esc(o.sound_card) : ''}</option>`).join('') +
    `<option value="type"${sel('type')}>Type a path</option></select>` +
    `<input type="text" id="rsport" autocomplete="off" spellcheck="false" placeholder="/dev/ttyUSB0" style="margin-top:6px" value="${esc(typed ? cur : '')}"${typed ? '' : ' hidden'}>` +
    '<div class="note">' + (ports.length ? 'Automatic uses the one USB serial device plugged in. With two, pick the radio\'s.'
      : 'No USB serial device is plugged in. Plug in the radio\'s USB or control cable, then reopen this.') +
    (typed ? ' The typed port is not plugged in, or is not a USB serial device.' : '') + '</div>';
}
function rsPortValue() {
  const s = $('rsportsel');
  return !s ? '' : s.value === 'type' ? $('rsport').value.trim() : s.value;
}
// Hamlib numbers its Icom models 3001 to 3999; only those talk CI-V with an address.
function isIcom(p) { return Math.floor((p.rig_model || 0) / 1000) === 3; }
function rsAdvanced(p) {
  const radios = ((S.wizard && S.wizard.radios) || []).filter(r => r.rig_model > 0);
  const bauds = [4800, 9600, 19200, 38400, 57600, 115200];
  if (p.cat_baud && !bauds.includes(p.cat_baud)) bauds.push(p.cat_baud);
  const sel = (v, cur) => (v === cur ? ' selected' : '');
  return '<div class="sec" id="rsadv"><h3>Advanced</h3>' +
    '<label class="fl" for="rsmodel">Hamlib model</label><select id="rsmodel">' +
    (p.rig_model ? '' : '<option value="" selected>None (no radio control)</option>') +
    radios.map(r => `<option value="${esc(r.name)}"${sel(r.name, p.rig_model ? p.name : '')}>${esc(radioTitle(r))} (${r.rig_model})</option>`).join('') + '</select>' +
    hlHTML('rshl', 'Or search every radio Hamlib knows') +
    rsPortHTML(p) +
    '<label class="fl" for="rsbaud">Baud rate</label><select id="rsbaud">' +
    (p.cat_baud ? '' : '<option value="0" selected>Radio default</option>') +
    bauds.map(b => `<option value="${b}"${sel(b, p.cat_baud)}>${b}</option>`).join('') + '</select>' +
    (isIcom(p) ? '<label class="fl" for="rsciv">CI-V address</label>' +
      `<input type="text" id="rsciv" autocomplete="off" spellcheck="false" maxlength="4" placeholder="Radio default" value="${esc(p.civaddr || '')}">` +
      '<div class="note">Only if you changed it on the radio. Copy it from the radio\'s menu (CI-V Address), for example A4.</div>' : '') +
    rsCardHTML() +
    '<label class="fl" for="rsptt">Keying method</label><select id="rsptt">' +
    ['cat', 'rts', 'dtr', 'vox'].map(m => `<option value="${m}"${sel(m, p.ptt_method)}>${PTT_LABEL[m]}</option>`).join('') + '</select>' +
    '<div class="note">Radio control: refuses if the radio is already keyed. RTS or DTR: needs the serial path. VOX: nothing to check, and radio control is not used.</div>' +
    rsPttPortHTML(p) + rsConfHTML(p) +
    '<p class="why">Changing any of these re-checks radio control and keying, and re-locks transmit.</p>' +
    '<p class="err" id="rserr" role="alert"></p>' +
    '<div class="btnrow"><button type="button" class="btn secondary" data-rs="cancel">Cancel</button>' +
    '<button type="button" class="btn primary" data-rs="apply">Apply and Re-check</button></div></div>' +
    '<div class="sec" id="rsapps"></div>';
}
// RTS or DTR keying through a second cable, such as a keying interface beside the radio's own USB.
function rsPttPortHTML(p) {
  if (!p.rig_model) return '';  // with no radio control the serial port above is the keying port
  const ports = ((S.wizard && S.wizard.serial_ports) || []).slice();
  if (p.ptt_device && !ports.some(x => x.path === p.ptt_device)) ports.push({path: p.ptt_device, label: p.ptt_device});
  return '<label class="fl" for="rspttdev">Keying port</label><select id="rspttdev">' +
    `<option value=""${p.ptt_device ? '' : ' selected'}>Same as the radio control port</option>` +
    ports.map(x => `<option value="${esc(x.path)}"${x.path === p.ptt_device ? ' selected' : ''}>${esc(x.label)}</option>`).join('') + '</select>' +
    '<div class="note">Used only when keying by RTS or DTR through another cable.</div>';
}
// rigctld -C: settings some cables and radios need, such as rts_state=OFF to stop an interface keying at open.
function rsConfHTML(p) {
  if (!p.rig_model) return '';  // VOX or no radio control: rigctld has no radio to pass them to
  return '<details class="hlog" id="rsconfd"' + (p.set_conf ? ' open' : '') + '><summary>Expert Hamlib settings</summary>' +
    '<label class="fl" for="rsconf">Settings, name=value separated by commas</label>' +
    `<input type="text" id="rsconf" autocomplete="off" spellcheck="false" maxlength="400" placeholder="rts_state=OFF, post_write_delay=50" value="${esc(p.set_conf || '')}">` +
    '<div class="note">Only if the radio or cable maker says so. Each name is checked against this radio\'s Hamlib settings, listed below.</div>' +
    '<div id="rsconflist" class="note"></div></details>';
}
async function rsConfLoad(model) {
  const el = $('rsconflist');
  if (!el || el.dataset.model === String(model)) return;
  el.dataset.model = String(model);
  const r = await getJSON(`/api/hamlib/conf?model=${model}`);
  if (!$('rsconflist')) return;
  const owned = new Set((r && r.owned) || []);
  const ps = r && r.ok ? r.params.filter(x => !owned.has(x.name) && !x.name.endsWith('_pathname')) : [];
  const what = x => x.options ? x.options.join(', ') : x.kind === 'range' ? `${x.min} to ${x.max}` : 'text';
  setHTML(el, ps.length ? '<ul class="mono" style="padding-left:18px;margin:6px 0 0">' + ps.map(x =>
    `<li><b>${esc(x.name)}</b>: ${esc(x.desc)} (${esc(what(x))}; default ${esc(x.default || 'none')})</li>`).join('') + '</ul>'
    : 'The list could not be read from Hamlib on this bridge.');
}
// Port 4532: an app logged in with the PIN (VectorCable, relaying WSJT-X and JS8Call) tunes and keys through the bridge's safety proxy.
// Applies at once, apart from Apply and Re-check: it changes who may connect, not the radio.
const RC_LABEL = {off: 'Off (only this bridge)', ap: "This bridge's Wi-Fi", wifi: 'Any Wi-Fi the bridge is on', all: 'Every network'};
function rcHTML(r) {
  if (!r || !r.ok) return '<h3>Rig Control for Apps</h3><p class="note">Not available: the safety proxy on port 4532 is not running.</p>';
  const sel = (v) => (v === r.access ? ' selected' : '');
  const where = r.access === 'off' ? '' : r.access === 'ap' ? `10.42.0.1:${r.port}` : `${address()}:${r.port}`;
  const failed = r.failed && r.failed.length ? r.failed : [];
  return '<h3>Rig Control for Apps</h3>' +
    '<label class="fl" for="rsrc">Who can connect</label><select id="rsrc">' +
    ['off', 'ap', 'wifi', 'all'].map(m => `<option value="${m}"${sel(m)}>${RC_LABEL[m]}</option>`).join('') + '</select>' +
    '<div class="note">Lets an app logged in with the admin PIN, such as VectorCable, change band, frequency and mode, and key the radio. ' +
    'Keying still passes the transmit checks, one transmitter at a time and the Transmit Time Limit.' +
    (where ? ` VectorCable connects to <b class="mono">${esc(where)}</b>; in WSJT-X or JS8Call choose Hamlib NET rigctl and enter VectorCable\'s own address, <b class="mono">127.0.0.1:4532</b>.` : '') + '</div>' +
    (r.access === 'wifi' ? '<p class="note">Answers on the bridge\'s own Wi-Fi and on a home or event Wi-Fi it joins, never over Tailscale.</p>' : '') +
    (r.access === 'all' ? '<p class="why">Answers on every network the bridge is on, Tailscale included. Use Any Wi-Fi the bridge is on unless you need Tailscale.</p>' : '') +
    (failed.length ? `<p class="err" role="alert">Not listening on ${esc(failed.join('; '))}</p>` : '') +
    `<p class="err" id="rsrcerr" role="alert">${esc(RS.rcMsg || '')}</p>`;
}
async function rcLoad(r) {
  if (!$('rsapps')) return;
  if (r === undefined) { try { r = await (await fetch('/api/rigctl', {cache: 'no-store'})).json(); } catch (e) { r = null; } }
  const el = $('rsapps');
  if (!el) return;
  el.innerHTML = rcHTML(r);  // always: a repeat of the same answer must re-enable the select
  const s = $('rsrc');
  if (s) s.onchange = async () => {
    s.disabled = true;
    const out = await post('/api/rigctl', {access: s.value});
    RS.rcMsg = out.ok ? '' : out.error;
    rcLoad(out.ok ? out : undefined);
  };
}
function rsPaint() {
  const p = profile(), el = $('rsbody');
  if (!el) return;
  if (!p) { setHTML(el, '<p class="note">No radio is set up yet.</p><button type="button" class="btn primary" data-sheet="setup" data-arg=\'{"choose":true}\'>Set Up a Radio</button>'); return; }
  setHTML($('rshead'), sheetHead({id: 'sheeth', title: radioTitle(p)}));
  setHTML(el, rsBody(p));
}
function rsAdvPaint() {
  const p = profile(), a = $('rsadvwrap'), t = $('rsadvbtn');
  if (!a || !t) return;
  t.hidden = !p; t.setAttribute('aria-expanded', RS.adv ? 'true' : 'false');
  a.innerHTML = RS.adv && p ? rsAdvanced(p) : '';
  rcLoad();
  const cd = $('rsconfd');  // the list is read from Hamlib only when the operator opens it
  if (cd) { const load = () => { if (cd.open) rsConfLoad(p.rig_model); }; cd.ontoggle = load; load(); }
  const ps = $('rsportsel');
  if (ps) ps.onchange = () => { const t = $('rsport'); t.hidden = ps.value !== 'type'; if (!t.hidden) t.focus(); };
  const m = $('rsmodel');
  if (m) m.onchange = () => {
    const name = m.value, cp = profile();
    const go = () => { openSheet('setup', {choose: true, confirmed: true}); suChoose({action: 'choose', profile: name, interface: cp.interface === 'other' && cp.ptt_method === 'vox' ? '' : cp.interface}); };
    if (radioSetUp() || (S.wizard && S.wizard.kit)) changeWarning(go, () => { m.value = cp.rig_model ? cp.name : ''; });
    else go();
  };
  hlWire('rshl', model => {
    const cp = profile();
    const go = () => { openSheet('setup', {choose: true, confirmed: true}); suChoose({action: 'choose_model', rig_model: model, interface: cp.interface === 'other' && cp.ptt_method === 'vox' ? '' : cp.interface}); };
    if (radioSetUp() || (S.wizard && S.wizard.kit)) changeWarning(go); else go();
  });
}
async function rsClick(e) {
  const b = e.target.closest('[data-rs]');
  if (!b) return;
  const a = b.dataset.rs, w = S.wizard || {};
  if (a === 'adv' || a === 'cancel') { RS.adv = !RS.adv && a === 'adv'; rsAdvPaint(); if (RS.adv) $('rsadv').scrollIntoView({block: 'nearest'}); }
  else if (a === 'another') {
    const go = () => openSheet('setup', {choose: true, confirmed: true});
    if (radioSetUp() || w.kit) changeWarning(go); else go();
  } else if (a === 'savekit') {
    b.disabled = true;
    const r = await post('/api/wizard', {action: 'save_kit', name: w.kit});
    b.disabled = false;
    RS.kitMsg = r.ok ? '' : r.error; if (r.ok) S.wizard = r;
    rsPaint();
  } else if (a === 'apply') {
    const settings = {cat_device: rsPortValue(), cat_baud: parseInt($('rsbaud').value, 10) || 0, ptt_method: $('rsptt').value};
    if ($('rsciv')) settings.civaddr = $('rsciv').value.trim();
    if ($('rsconf')) settings.set_conf = $('rsconf').value.trim();
    if ($('rspttdev')) settings.ptt_device = $('rspttdev').value;
    if ($('rscard')) { const [path, id] = $('rscard').value.split('@'); Object.assign(settings, {audio_usb_path: path || '', audio_usb_id: id || ''}); }
    b.disabled = true;
    const r = await post('/api/wizard', {action: 'update', settings});
    b.disabled = false;
    if (!r.ok) { setHTML($('rserr'), esc(r.error)); return; }
    S.wizard = r; RS.adv = false;
    openSheet('setup', {resume: true});
  }
}
SHEETS.radio = {
  render(el, arg) {
    RS = {adv: !!arg.advanced, err: '', kitMsg: '', rcMsg: ''};
    el.innerHTML = '<div id="rshead">' + sheetHead({id: 'sheeth', title: radioTitle(profile())}) + '</div><div id="rsbody" style="display:flex;flex-direction:column;gap:12px"></div>' +
      '<button type="button" class="btn secondary" id="rsadvbtn" data-rs="adv" aria-controls="rsadvwrap" style="border-color:var(--ok)">Advanced</button><div id="rsadvwrap"></div>';
    el.onclick = rsClick;
    rsPaint(); rsAdvPaint();
    if (RS.adv && $('rsadv')) $('rsadv').scrollIntoView({block: 'nearest'});
  },
  update() { rsPaint(); if (!$('rsadv') && RS.adv) rsAdvPaint(); },
};
// -- kits and factory reset (P1 #10). Wi-Fi is never touched from here --
let MK = {edit: -1, msg: '', err: '', done: ''};
const MK_SCOPES = [
  {value: 'radio', label: 'Reset Radio Setup', hint: "Forgets the radio, every saved kit and the radios picked from Hamlib's list. Radio control stops. Levels, alerts and Wi-Fi stay."},
  {value: 'all', label: 'Reset Everything Except Wi-Fi', hint: 'The radio setup, plus audio levels, sound card, transmit time limit, rig control for apps and alert choices back to their defaults. Saved fault reports are removed. Wi-Fi stays.'}];
function mkKitsHTML() {
  const kits = (S.wizard && S.wizard.kits) || [];
  const row = (k, i) => MK.edit === i
    ? `<li><label class="fl" for="mkname">New name for ${esc(k.name)}</label><input type="text" id="mkname" maxlength="40" autocomplete="off" spellcheck="false" value="${esc(k.name)}">` +
      (k.name === (S.wizard && S.wizard.kit) ? '<p class="note">This is the kit in use: renaming it locks transmit again until the transmit checks pass.</p>' : '') +
      `<div class="btnrow"><button type="button" class="btn secondary" data-mk="cancel">Cancel</button><button type="button" class="btn primary" data-mk="save" data-i="${i}">Save Name</button></div></li>`
    : `<li><div class="ct">${esc(k.name)}</div><div class="cd">${esc(kitSub(k))}</div>` +
      `<div class="btnrow"><button type="button" class="btn secondary" data-mk="rename" data-i="${i}">Rename</button><button type="button" class="btn secondary" data-mk="delete" data-i="${i}">Delete</button></div></li>`;
  return '<h3>Saved Kits</h3>' + (kits.length ? '<ul style="list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:12px">' +
    kits.map(row).join('') + '</ul>' : '<p class="note">No kits saved yet.</p>') + `<p class="err" role="alert">${esc(MK.msg)}</p>`;
}
function mkPaint() {
  setHTML($('mkkits'), mkKitsHTML());
  setHTML($('mkerr'), esc(MK.err));
  setHTML($('mkdone'), MK.done ? `<p class="note" role="status">${esc(MK.done)}</p><button type="button" class="btn primary" data-sheet="setup" data-arg='{"choose":true}'>Set Up a Radio</button>` : '');
}
async function mkClick(e) {
  const b = e.target.closest('[data-mk]');
  if (!b) return;
  const a = b.dataset.mk, k = ((S.wizard && S.wizard.kits) || [])[+b.dataset.i];
  if (a === 'rename' && k) { MK.edit = +b.dataset.i; MK.msg = ''; mkPaint(); const n = $('mkname'); if (n) n.focus(); }
  else if (a === 'cancel') { MK.edit = -1; MK.msg = ''; mkPaint(); }
  else if (a === 'save' && k) {
    b.disabled = true;
    const r = await post('/api/wizard', {action: 'rename_kit', name: k.name, new_name: $('mkname').value});
    b.disabled = false;
    MK.msg = r.ok ? '' : r.error; if (r.ok) { S.wizard = r; MK.edit = -1; }
    mkPaint();
  } else if (a === 'delete' && k) confirmBox({title: `Delete ${k.name}?`, bad: true, cancel: 'Keep It', ok: 'Delete Kit',
    lead: 'The kit is removed from the card. The radio setup in use now stays as it is. If it is the kit in use, transmit locks again.',
    onOk: async () => {
      const r = await post('/api/wizard', {action: 'delete_kit', name: k.name});
      MK.msg = r.ok ? '' : r.error; if (r.ok) { S.wizard = r; MK.edit = -1; }
      mkPaint();
    }});
  else if (a === 'reset') {
    const scope = (document.querySelector('input[name="mkscope"]:checked') || {}).value || 'radio';
    MK.done = ''; MK.err = '';
    if ($('mkconfirm').value.trim() !== 'RESET') { MK.err = 'Type RESET in capital letters to confirm.'; mkPaint(); return; }
    b.disabled = true;
    const r = await post('/api/reset', {scope, confirm: 'RESET'});
    b.disabled = false;
    if (r.wizard) S.wizard = r.wizard;
    if (r.wizard || r.ok) { $('mkconfirm').value = ''; MK.edit = -1; }
    if (r.ok) MK.done = scope === 'all' ? 'Done. The radio setup is cleared and settings are back to their defaults. Wi-Fi is unchanged.'
      : 'Done. The radio setup and saved kits are cleared. Transmit is locked until a radio is set up again.';
    else MK.err = r.problems && r.problems.length ? `Reset, but ${r.problems.join('; ')}.` : r.error === 'wifi_open' ? r.fix : r.error || 'The bridge did not answer.';
    mkPaint();
  }
}
SHEETS.manage = {
  render(el) {
    MK = {edit: -1, msg: '', err: '', done: ''};
    el.innerHTML = sheetHead({id: 'sheeth', title: 'Kits and Reset'}) +
      '<div class="sec" id="mkkits" style="border-top:0;padding-top:0"></div>' +
      '<div class="sec"><h3>Factory Reset</h3>' + choicesHTML('mkscope', MK_SCOPES, 'radio') +
      '<label class="fl" for="mkconfirm">Type RESET to confirm</label>' +
      '<input type="text" id="mkconfirm" maxlength="5" autocomplete="off" autocapitalize="characters" spellcheck="false">' +
      '<p class="note">Refused while the radio is transmitting. Transmit stays locked until setup and the transmit checks are done again.</p>' +
      '<p class="err" id="mkerr" role="alert"></p>' +
      '<div class="btnrow"><button type="button" class="btn stop" data-mk="reset">Reset</button></div><div id="mkdone"></div></div>';
    el.onclick = mkClick;
    mkPaint();
  },
  update() { mkPaint(); },
};
"""
