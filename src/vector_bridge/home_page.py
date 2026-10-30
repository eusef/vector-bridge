"""The home page: "Can I receive?" and "Can I transmit?", with every setting in a sheet over it.

One page serves `/` and `/wifi`. It polls the JSON routes the bridge already has and draws
the design boards (DOCS/design; requirements in SPEC.md). Sheets are same-page overlays, so the polling keeps
running underneath; `/setup` and `/transmit` redirect to `/#setup` and `/#transmit`.

The sheets live beside the modules they drive: wizard_page (radio setup), ladder_page
(unlock transmit), wifi_page (Wi-Fi) and station_page (receive, power, display). Each adds
renderers to SHEETS using the helpers defined here. Everything is inline: no fonts,
scripts or images come from outside the bridge.
"""

import html
import json

from . import version_text
from .alerts_page import HEAD_TAGS, HOME_SCRIPT
from .ladder_page import TRANSMIT_JS
from .readiness import FAULT_PRIORITY
from .station_page import STATION_JS
from .wifi_page import WIFI_JS
from .wizard_page import SETUP_JS

STYLE = r"""<style>
:root, :root[data-theme=dark] { color-scheme: dark;
  --bg: #0e1210; --surface: #1a1f1c; --line: #2a312c; --text: #ffffff; --dim: #9e9e9e;
  --ok: #3fbf4f; --ok-hover: #6fd57b; --warn: #f5c518; --bad: #e0332e; --bad-text: #f25a54; --stop: #d02e29;
  --field: rgba(14,18,16,0.6); --track: rgba(255,255,255,0.06); --on-accent: #0e1210; --scrim: rgba(0,0,0,0.55); }
:root[data-theme=light] { color-scheme: light;
  --bg: #f2f4f2; --surface: #ffffff; --line: #cfd6d1; --text: #0e1210; --dim: #5a635d;
  --ok: #1b7f2b; --ok-hover: #146321; --warn: #8a5a00; --bad: #b3261e; --bad-text: #b3261e; --stop: #b3261e;
  --field: rgba(14,18,16,0.04); --track: rgba(0,0,0,0.08); --on-accent: #ffffff; --scrim: rgba(0,0,0,0.35); }
:root[data-theme=night] { color-scheme: dark;
  --bg: #000000; --surface: #120000; --line: #3a0a0a; --text: #ff3b30; --dim: #b3261e;
  --ok: #ff3b30; --ok-hover: #ff6b62; --warn: #ff3b30; --bad: #ff3b30; --bad-text: #ff3b30; --stop: #b3261e;
  --field: rgba(255,59,48,0.06); --track: rgba(255,59,48,0.12); --on-accent: #000000; --scrim: rgba(0,0,0,0.7); }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); overflow-wrap: anywhere;
  font: 15px/20px -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, "Segoe UI", "Helvetica Neue", Arial, sans-serif; }
body.locked { overflow: hidden; }
.sr { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
.ic { width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 1.75; stroke-linecap: round; stroke-linejoin: round; flex: none; }
.mono { font-family: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; font-variant-numeric: tabular-nums; }
button { font-family: inherit; color: inherit; cursor: pointer; }
button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible { outline: 2px solid var(--ok); outline-offset: 2px; }
a { color: var(--ok); } a:hover { color: var(--ok-hover); }
dl, dd { margin: 0; } ol, ul { margin: 0; } h2, h3, p { margin: 0; }
input[type=range] { width: 100%; height: 44px; margin: 0; accent-color: var(--ok); }
input[type=checkbox], input[type=radio] { width: 22px; height: 22px; margin: 0; accent-color: var(--ok); flex: none; }
input[type=text], input[type=password], input[type=number], select { font-family: inherit; font-size: 17px; line-height: 22px; color: var(--text);
  background: var(--field); border: 1px solid var(--line); border-radius: 8px; min-height: 44px; padding: 0 12px; width: 100%; }
label.fl { display: block; font-size: 15px; line-height: 20px; font-weight: 600; }
.top { height: 64px; display: flex; align-items: center; justify-content: space-between; padding: 0 16px;
  background: var(--surface); border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 5; }
.brand { display: flex; align-items: center; gap: 8px; font-size: 20px; line-height: 25px; font-weight: 700; }
.brand .ic { width: 22px; height: 22px; color: var(--ok); }
.hbtns { display: flex; align-items: center; gap: 8px; }
.hbtn { min-width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; gap: 6px; padding: 0 10px;
  background: var(--field); border: 1px solid var(--line); border-radius: 8px; color: var(--text); text-decoration: none;
  font-size: 15px; line-height: 20px; font-weight: 600; font-variant-numeric: tabular-nums; }
.band { background: var(--warn); color: var(--on-accent); padding: 8px 16px; font-weight: 600; text-align: center; }
main { padding: 12px 16px; display: flex; flex-direction: column; gap: 12px; max-width: 560px; margin: 0 auto; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 14px;
  display: flex; flex-direction: column; gap: 6px; color: var(--text); text-decoration: none; text-align: left; }
.card.flush { padding: 0; gap: 0; }
.b-warn { border: 2px solid var(--warn) !important; } .b-bad { border: 2px solid var(--bad-text) !important; }
.b-ok { border: 2px solid var(--ok) !important; } .b-red { border: 2px solid var(--bad) !important; }
.eyebrow { display: flex; align-items: center; justify-content: space-between; gap: 6px; font-size: 12px; line-height: 16px;
  font-weight: 600; color: var(--dim); }
.eyebrow > span { display: flex; align-items: center; gap: 6px; }
.eyebrow .ic { width: 16px; height: 16px; }
.state { display: flex; align-items: center; gap: 10px; }
.state .ic { width: 30px; height: 30px; stroke-width: 2; }
.state span { font-size: 34px; line-height: 41px; font-weight: 700; }
.head { display: flex; align-items: center; gap: 8px; font-size: 17px; line-height: 22px; font-weight: 600; }
.c-ok { color: var(--ok); } .c-warn { color: var(--warn); } .c-bad { color: var(--bad-text); } .c-dim { color: var(--dim); }
.note { font-size: 13px; line-height: 18px; color: var(--dim); }
.why { font-size: 13px; line-height: 18px; color: var(--warn); }
.btn { min-height: 48px; display: flex; align-items: center; justify-content: center; gap: 6px; width: 100%; padding: 0 12px;
  border: 0; border-radius: 8px; font-size: 17px; line-height: 22px; font-weight: 600; text-decoration: none; text-align: center; }
.btn.primary { background: var(--ok); color: var(--on-accent); }
.btn.primary:hover { color: var(--on-accent); }
.btn.secondary { background: var(--field); border: 1px solid var(--line); color: var(--text); }
.btn.stop { background: var(--stop); color: #ffffff; }
.btn:disabled, .btn[aria-disabled=true] { background: var(--field); border: 1px solid var(--line); color: var(--dim); cursor: default; }
.btnrow { display: flex; gap: 8px; } .btnrow > * { flex: 1; }
.meter { display: flex; align-items: center; gap: 8px; }
.track { flex-grow: 1; height: 8px; border-radius: 3px; background: var(--track); position: relative; overflow: hidden; }
.zone { position: absolute; top: 0; bottom: 0; border-left: 1px solid var(--dim); border-right: 1px solid var(--dim); }
.fill { height: 100%; width: 0; background: var(--ok); transition: width .1s linear; }
.mlabel { font-size: 12px; line-height: 16px; color: var(--dim); white-space: nowrap; }
.fixes { margin: 4px 0 0; padding-left: 20px; } .fixes li { margin: 2px 0; }
.sechead { min-height: 44px; display: flex; align-items: center; justify-content: space-between; padding: 0 14px;
  border-bottom: 1px solid var(--line); }
.sechead h2 { font-size: 17px; line-height: 22px; font-weight: 600; }
.secbody { padding: 8px 14px 12px; display: flex; flex-direction: column; gap: 4px; }
.kv { min-height: 44px; display: flex; align-items: center; gap: 8px; }
.kv > div { flex-grow: 1; } .kv .k { font-size: 12px; line-height: 16px; color: var(--dim); } .kv .v { font-size: 17px; line-height: 22px; }
.round { width: 44px; height: 44px; flex: none; display: flex; align-items: center; justify-content: center; background: var(--field);
  border: 1px solid var(--line); border-radius: 8px; color: var(--text); }
.rows { list-style: none; margin: 0; padding: 0 14px; }
.rows > li { min-height: 56px; display: flex; align-items: center; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--line); }
.rows > li:last-child { border-bottom: 0; }
.rows .t { font-size: 15px; line-height: 20px; font-weight: 600; } .rows .s { font-size: 13px; line-height: 18px; color: var(--dim); }
.rows > li > div:first-child { flex-grow: 1; min-width: 0; }
.rows > li > .grow { flex-grow: 1; min-width: 0; } .rows a.t { color: var(--text); }
.appic { width: 32px; height: 32px; flex: none; border-radius: 8px; color: var(--dim); }
.pill { display: flex; align-items: center; gap: 4px; font-size: 12px; line-height: 16px; font-weight: 600; white-space: nowrap; }
.pill .ic { width: 14px; height: 14px; }
.badges { display: flex; flex-wrap: wrap; gap: 6px; }
.badge { font-size: 12px; line-height: 16px; font-weight: 700; padding: 2px 8px; border-radius: 999px; background: var(--warn); color: var(--on-accent); }
.badge.plain { background: transparent; border: 1px solid var(--line); color: var(--text); }
h2.station { margin: 4px 0 0; font-size: 17px; line-height: 22px; font-weight: 600; }
.tiles { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.tile { min-height: 96px; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px;
  display: flex; flex-direction: column; gap: 2px; color: var(--text); text-decoration: none; text-align: left; font: inherit; }
.tile.t-warn { border-color: var(--warn); } .tile.t-bad { border-color: var(--bad-text); }
.tile .tl { width: 100%; display: flex; justify-content: space-between; align-items: center; font-size: 12px; line-height: 16px; color: var(--dim); }
.tile .tv { font-size: 15px; line-height: 20px; font-weight: 600; margin-top: 6px; }
.tile .ts { font-size: 12px; line-height: 16px; color: var(--dim); font-variant-numeric: tabular-nums; }
.ver { margin: 0 0 16px; text-align: center; font-size: 13px; color: var(--dim); }
.foot { min-height: 44px; display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 16px; font-size: 15px; }
#sheetroot, #confirmroot { position: fixed; inset: 0; z-index: 20; }
#confirmroot { z-index: 30; }
.scrim { position: absolute; inset: 0; background: var(--scrim); }
.sheet { position: absolute; left: 0; right: 0; bottom: 0; top: 72px; max-width: 560px; margin: 0 auto; background: var(--surface);
  border-top: 1px solid var(--line); border-radius: 12px 12px 0 0; padding: 8px 16px 24px; display: flex; flex-direction: column;
  gap: 12px; overflow-y: auto; overscroll-behavior: contain; }
.grab { align-self: center; width: 36px; height: 5px; border-radius: 999px; background: var(--line); flex: none; }
.shead { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.shead > div { flex-grow: 1; } .shead h2 { font-size: 20px; line-height: 25px; font-weight: 700; }
.shead .sub { font-size: 13px; line-height: 18px; color: var(--dim); }
.shead .round { border-radius: 999px; }
.steps { list-style: none; margin: 0; display: flex; gap: 4px; border: 1px solid var(--line); border-radius: 8px; padding: 8px 4px; background: var(--field); }
.steps li { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 4px; font-size: 12px; line-height: 16px; color: var(--dim); text-align: center; }
.steps li .ic { width: 18px; height: 18px; }
.steps .done { color: var(--ok); } .steps .cur { color: var(--text); font-weight: 600; } .steps .fail { color: var(--bad-text); font-weight: 600; }
.sec { border-top: 1px solid var(--line); padding-top: 12px; display: flex; flex-direction: column; gap: 8px; }
.sec h3 { font-size: 17px; line-height: 22px; font-weight: 600; }
.choice { display: flex; align-items: center; gap: 12px; min-height: 56px; padding: 10px 12px; background: var(--field);
  border: 1px solid var(--dim); border-radius: 8px; cursor: pointer; }
.choice:has(input:checked) { border-color: var(--ok); }
.choice .cl { display: block; font-size: 17px; line-height: 22px; font-weight: 600; } .choice .ch { display: block; font-size: 13px; line-height: 18px; color: var(--dim); }
.opt { width: 100%; min-height: 56px; display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 12px;
  background: var(--field); border: 1px solid var(--line); border-radius: 8px; color: var(--text); text-decoration: none; text-align: left; font: inherit; }
.opt .ot { display: block; font-size: 17px; line-height: 22px; font-weight: 600; } .opt .os { display: block; font-size: 13px; line-height: 18px; color: var(--dim); }
.optlist { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.checks { list-style: none; margin: 0; padding: 0; }
.checks > li { display: flex; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--line); }
.checks > li > div { flex-grow: 1; min-width: 0; }
.checks .ct { font-size: 15px; line-height: 20px; font-weight: 600; } .checks .cd { font-size: 13px; line-height: 18px; color: var(--dim); }
.setfor { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.setfor li { display: flex; gap: 8px; align-items: flex-start; } .setfor li .ic { width: 16px; height: 16px; margin-top: 2px; color: var(--ok); }
.setfor li.dimmed, .setfor li.dimmed .ic { color: var(--dim); }
.box { background: var(--track); border-radius: 8px; padding: 12px; display: flex; flex-direction: column; gap: 6px; }
.box h3 { font-size: 15px; line-height: 20px; font-weight: 600; } .box ul { padding-left: 20px; }
.dlg { position: absolute; left: 16px; right: 16px; top: 110px; max-width: 520px; margin: 0 auto; background: var(--surface);
  border: 2px solid var(--warn); border-radius: 12px; padding: 16px; display: flex; flex-direction: column; gap: 12px;
  max-height: calc(100% - 130px); overflow-y: auto; }
.dlg.bad { border-color: var(--bad-text); }
.dlg h2 { font-size: 20px; line-height: 25px; font-weight: 700; }
.err { color: var(--bad-text); font-size: 13px; line-height: 18px; }
details.hlog { margin-top: 8px; font-size: 13px; color: var(--dim); }
details.hlog pre { margin: 6px 0 0; padding: 8px; max-height: 200px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere;
  font-size: 11px; line-height: 15px; background: var(--field); border: 1px solid var(--line); border-radius: 8px; }
.spin { animation: spin 1s linear infinite; } @keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } .fill { transition: none; } }
</style>"""

BODY = r"""<header class="top">
<div class="brand"><svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9"/><path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5"/><circle cx="12" cy="12" r="2"/><path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5"/><path d="M19.1 4.9C23 8.8 23 15.1 19.1 19"/></svg>VectorBridge</div>
<div class="hbtns"><button type="button" class="hbtn" id="themebtn" aria-label="Appearance: dark. Tap to switch."><svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.9 4.9 1.4 1.4"/><path d="m17.7 17.7 1.4 1.4"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.3 17.7-1.4 1.4"/><path d="m19.1 4.9-1.4 1.4"/></svg></button>
<a class="hbtn" id="battbtn" href="#power" data-sheet="power" aria-label="Battery not yet reported. Open power settings"><svg class="ic" viewBox="0 0 24 24" aria-hidden="true" id="batticon"><rect width="16" height="10" x="2" y="7" rx="2"/><path d="M22 11v2"/><path d="M6 11v2"/><path d="M10 11v2"/></svg><span id="batt" hidden></span><span id="battnone">--</span></a></div>
</header>
<div class="band" id="maint" hidden><span id="mode" hidden>Maintenance mode. Do not pull power.</span></div>
<div class="band" id="cfgbad" hidden>The settings file could not be read, so the bridge is on its default settings. Changing any setting writes a new one.</div>
<main>
<h1 class="sr">Station status</h1>
<div role="status" class="sr" id="live">Could I pass traffic right now? Checking.</div>
<div id="top"></div>
<div id="rx"></div>
<div id="tx"></div>
<section class="card flush" aria-labelledby="hcon" id="connect"></section>
<div id="apps"></div>
<div id="station"></div>
<div class="foot"><a href="/docs/">Docs and Help</a><a href="#display" data-sheet="display">Display</a><a href="#txlimit" data-sheet="txlimit">Transmit Time Limit</a><a href="/api/support" download>Support Bundle</a><a href="#manage" data-sheet="manage">Kits and Reset</a></div>
<p class="ver">VectorBridge __VERSION__</p>
</main>
<div id="alert" hidden></div>
<div id="sheetroot" hidden></div>
<div id="confirmroot" hidden></div>
<span id="arm" class="sr"></span>
"""

# The shell: state, polling, the home cards and the sheet framework the *_page modules build on.
SHELL_JS = r"""
const PRIORITY = __PRIORITY__;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => '&#' + c.charCodeAt(0) + ';');
const S = {readiness: null, battery: null, mode: undefined, wifi: null, join: null, wizard: null, ladder: null, status: null, apps: null};
const T0 = performance.now();  // no RTC on the Pi: times are relative to this page, never a time of day
const ICONS = {
  ok: '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>', dot: '<circle cx="12" cy="12" r="10"/>',
  fail: '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
  warn: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  chev: '<path d="m9 18 6-6-6-6"/>', close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>', back: '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
  tick: '<path d="M20 6 9 17l-5-5"/>', lock: '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  unlock: '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/>',
  rx: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>', tx: '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
  copy: '<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  busy: '<path d="M21 12a9 9 0 1 1-6.2-8.6"/>', pause: '<circle cx="12" cy="12" r="10"/><path d="M10 15V9"/><path d="M14 15V9"/>',
  app: '<rect width="18" height="18" x="3" y="3" rx="4"/><path d="M3 9h18"/>',
  wifi: '<path d="M12 20h.01"/><path d="M2 8.82a15 15 0 0 1 20 0"/><path d="M5 12.86a10 10 0 0 1 14 0"/><path d="M8.5 16.43a5 5 0 0 1 7 0"/>',
};
function icon(name, size, cls) {
  return `<svg class="ic${cls ? ' ' + cls : ''}" viewBox="0 0 24 24" aria-hidden="true"${size ? ` style="width:${size}px;height:${size}px"` : ''}>${ICONS[name] || ''}</svg>`;
}
const fmtW = w => (typeof w === 'number' ? (+w.toPrecision(3)).toString() + ' W' : 'its minimum');
function ago(ms) {  // "3 min ago" from a performance.now() reading
  const s = Math.max(0, Math.round((performance.now() - ms) / 1000));
  return s < 60 ? s + ' s ago' : s < 3600 ? Math.round(s / 60) + ' min ago' : Math.round(s / 3600) + ' h ago';
}
async function getJSON(path) {
  try { const r = await fetch(path, {cache: 'no-store'}); const b = await r.json(); b._code = r.status; return b; }
  catch (e) { return null; }
}
async function post(path, body) {
  try {
    const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const b = await r.json(); b._code = r.status; return b;
  } catch (e) { return {ok: false, error: 'The bridge did not answer. Check you are still on its Wi-Fi.', _code: 0}; }
}
function setHTML(el, html) { if (el && el._h !== html) { el.innerHTML = html; el._h = html; } }
function check(key) { return ((S.readiness && S.readiness.checks) || []).find(c => c.key === key) || null; }
const profile = () => (S.wizard && S.wizard.profile) || null;
const minPower = () => (S.ladder && typeof S.ladder.min_power_w === 'number') ? S.ladder.min_power_w : (profile() || {}).min_power_w;
const rxRed = () => { const c = check('rx_audio'); return !!c && c.state === 'fail'; };
const battLocked = () => !!(S.battery && S.battery.tx_locked);
const radioSetUp = () => !!(profile() && S.wizard.kit_ready);
const keyedNow = () => { const c = check('ptt'); return !!c && /\btransmitting\b/i.test(c.detail); };

// ---- receive level: the same judgement for the home card and the receive sheet ----
const FLOOR = -60, JUDGE_MS = 3000, SILENT_DB = -45, CLIP_MS = 3000;
const pos = db => Math.max(0, Math.min(100, (Math.max(db, FLOOR) - FLOOR) / -FLOOR * 100));
let peaks = [], lastClip = null, clipAt = -1e12;
function noteStatus(t) {
  const now = performance.now();
  peaks.push([now, t.rx_dbfs]); while (peaks.length && now - peaks[0][0] > JUDGE_MS) peaks.shift();
  if (lastClip !== null && t.clip_count > lastClip) clipAt = now;
  lastClip = t.clip_count;
}
function rxLevel() {  // null until the audio service answers
  const t = S.status; if (!t || typeof t.rx_dbfs !== 'number') return null;
  const loud = Math.max(...peaks.map(p => p[1])), lo = t.target_low_dbfs, hi = t.target_high_dbfs;
  const clip = performance.now() - clipAt < CLIP_MS;
  const [state, label] = clip ? ['clip', 'Clipping'] : loud <= SILENT_DB ? ['silent', 'Silent'] : loud > hi ? ['high', 'Above range']
    : loud < lo ? ['low', 'Below range'] : ['ok', 'In range'];
  return {fill: pos(t.rx_dbfs), lo: pos(lo), hi: pos(hi), state, label, gain: t.rx_gain};
}
// A sound card with no capture level (the IC-705's built-in codec): the receive slider can change
// nothing, so every receive-level hint points at the radio. Radios name the setting differently.
const rxFixed = () => !!(S.status && S.status.rx_gain_fixed);
const RX_FIXED_TEXT = "This radio's sound card has no receive level control, so the bridge cannot change it. Set the level on the radio.";
const RX_RADIO_HINT = "Look in the radio's menu for a USB audio, AF output or data output level. " +
  "On a headphone or line-out cable, use the radio's volume or line-out level.";
function meterHTML(label) {  // live parts are filled in by paintMeters() on every status poll
  return `<div class="meter" role="meter" aria-label="Receive level" data-rx aria-valuetext="${esc(label || '')}"><div class="track">` +
    `<div class="zone"></div><div class="fill"></div></div><span class="mlabel">${esc(label || '')}</span></div>`;
}
function paintMeters() {
  const L = rxLevel();
  for (const m of document.querySelectorAll('[data-rx]')) {
    if (!L) continue;
    const fixed = m.dataset.label;  // a card that names its own state keeps its label
    m.querySelector('.zone').style.cssText = `left:${L.lo}%;width:${L.hi - L.lo}%`;
    const f = m.querySelector('.fill');
    f.style.width = L.fill + '%';
    f.style.background = L.state === 'clip' ? 'var(--bad)' : L.state === 'silent' ? 'var(--dim)' : L.state === 'ok' ? 'var(--ok)' : 'var(--warn)';
    const text = fixed || L.label;
    m.setAttribute('aria-valuetext', text); m.querySelector('.mlabel').textContent = text;
  }
}

// ---- sheets ----
const SHEETS = {};
let cur = null;
function sheetHead(o) {  // {id, title, sub, back}: back shows the arrow and calls SHEETS[name].back()
  return '<div class="grab" aria-hidden="true"></div><div class="shead">' +
    (o.back ? `<button type="button" class="round" data-back aria-label="Back">${icon('back')}</button>` : '') +
    `<div><h2 id="${esc(o.id || 'sheeth')}" tabindex="-1">${esc(o.title)}</h2>` + (o.sub ? `<div class="sub">${esc(o.sub)}</div>` : '') + '</div>' +
    `<button type="button" class="round" data-close aria-label="Close">${icon('close')}</button></div>`;
}
function stepsHTML(label, items) {  // items: [[text, state]] with state done | cur | fail | todo
  const ic = {done: 'ok', fail: 'fail', cur: 'dot', todo: 'dot'};
  return `<ol class="steps" aria-label="${esc(label)}">` + items.map(([t, s]) =>
    `<li class="${s}"${s === 'cur' ? ' aria-current="step"' : ''}>${icon(ic[s] || 'dot', 18)}<span>${esc(t)}</span></li>`).join('') + '</ol>';
}
function choicesHTML(name, opts, checked) {  // opts: [{value, label, hint}]
  return opts.map(o => `<label class="choice"><input type="radio" name="${esc(name)}" value="${esc(o.value)}"` +
    (o.value === checked ? ' checked' : '') + `><span><span class="cl">${esc(o.label)}</span>` +
    (o.hint ? `<span class="ch">${esc(o.hint)}</span>` : '') + '</span></label>').join('');
}
function setForHTML(items) {  // "Set for you": [text] or [[text, true]] for a dimmed line
  return '<div class="sec"><div class="note">Set for you</div><ul class="setfor">' + items.map(i => Array.isArray(i)
    ? `<li class="dimmed">${icon('info')}<span>${esc(i[0])}</span></li>` : `<li>${icon('tick')}<span>${esc(i)}</span></li>`).join('') + '</ul></div>';
}
function openSheet(name, arg) {
  if (!SHEETS[name]) return;
  cur = {name, arg: arg || {}};
  const root = $('sheetroot');
  root.innerHTML = '<div class="scrim" data-close aria-hidden="true"></div><section class="sheet" role="dialog" aria-modal="true" aria-labelledby="sheeth" id="sheet"></section>';
  root.hidden = false; document.body.classList.add('locked');
  if (location.hash !== '#' + name) history.replaceState(null, '', '/#' + name);
  SHEETS[name].render($('sheet'), cur.arg);
  const h = $('sheet').querySelector('h2'); if (h) h.focus({preventScroll: true});
}
function closeSheet() {
  if (cur && SHEETS[cur.name].close) SHEETS[cur.name].close();
  cur = null; $('sheetroot').hidden = true; $('sheetroot').innerHTML = ''; document.body.classList.remove('locked');
  history.replaceState(null, '', '/'); refresh();
}
function confirmBox(o) {  // {title, lead, listTitle, items, cancel, ok, onOk, bad}: the alertdialog boards
  const root = $('confirmroot');
  root.innerHTML = `<div class="scrim" aria-hidden="true" style="background:rgba(0,0,0,0.7)"></div><section class="dlg${o.bad ? ' bad' : ''}" role="alertdialog" aria-modal="true" aria-labelledby="dlgh" aria-describedby="dlgd">` +
    `<div class="head ${o.bad ? 'c-bad' : 'c-warn'}">${icon('warn', 22)}<h2 id="dlgh" tabindex="-1">${esc(o.title)}</h2></div>` +
    `<p id="dlgd">${esc(o.lead)}</p>` + (o.items ? `<div class="box"><h3>${esc(o.listTitle || 'What happens next')}</h3><ul>` +
    o.items.map(i => `<li>${i}</li>`).join('') + '</ul></div>' : '') +
    `<div class="btnrow"><button type="button" class="btn secondary" id="dlgno">${esc(o.cancel)}</button>` +
    `<button type="button" class="btn primary" id="dlgyes">${esc(o.ok)}</button></div></section>`;
  root.hidden = false; $('dlgh').focus();
  const done = () => { root.hidden = true; root.innerHTML = ''; };
  $('dlgno').onclick = () => { done(); if (o.onCancel) o.onCancel(); };
  $('dlgyes').onclick = () => { done(); o.onOk(); };
}
document.addEventListener('click', e => {
  const t = e.target.closest('[data-sheet]');
  if (t) { e.preventDefault(); if (cur) closeSheetQuiet(); openSheet(t.dataset.sheet, t.dataset.arg ? JSON.parse(t.dataset.arg) : {}); return; }
  if (e.target.closest('#sheetroot [data-close]')) { closeSheet(); return; }
  if (e.target.closest('#sheetroot [data-back]') && cur && SHEETS[cur.name].back) SHEETS[cur.name].back();
});
function closeSheetQuiet() { if (cur && SHEETS[cur.name].close) SHEETS[cur.name].close(); cur = null; }
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (!$('confirmroot').hidden) { $('confirmroot').hidden = true; $('confirmroot').innerHTML = ''; } else if (cur) closeSheet();
});

// ---- theme: saved on this phone, not on the bridge ----
const THEME_NAMES = {light: 'Daylight', dark: 'Dark', night: 'Night Ops', auto: 'Match the phone'};
function themePref() { try { return localStorage.getItem('vb-theme') || 'auto'; } catch (e) { return 'auto'; } }
function applyTheme() {
  const p = themePref(), dark = window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches;
  const t = p === 'auto' ? (dark ? 'dark' : 'light') : p;
  document.documentElement.dataset.theme = t;
  $('themebtn').setAttribute('aria-label', `Appearance: ${THEME_NAMES[t].toLowerCase()}. Tap to switch.`);
}
function setTheme(p) { try { localStorage.setItem('vb-theme', p); } catch (e) {} applyTheme(); }
$('themebtn').onclick = () => {  // one tap cycles Daylight, Dark and Night Ops: no menu with gloves on
  const order = ['light', 'dark', 'night'], now = document.documentElement.dataset.theme;
  setTheme(order[(order.indexOf(now) + 1) % order.length]);
};
if (window.matchMedia) matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);
applyTheme();

// ---- faults: one owns the top of the page and the alert, the rest stack under it ----
const FAULT = {
  ptt: ['Unexpected transmit', null, null], cat: ['Radio control lost', 'radio', 'Fix'],
  gates: ['Transmit locked again', 'transmit', 'Unlock'], rx_audio: ['Receive audio lost', 'receive', 'Fix'],
  battery: ['Battery low', 'power', 'Power'], client: ['App disconnected', null, null],
};
const since = {};  // fault key -> performance.now() when this page first saw it
function batteryFault() {
  const b = S.battery; if (!b || !b.present || !b.level || b.level === 'ok') return null;
  const p = b.percent + '%';
  const [title, detail] = b.level === 'tx_locked' ? ['Battery too low to transmit', `${p}. Transmit is locked below 6%. Plug in the bridge.`]
    : b.level === 'critical' ? ['Battery critical', `${p}. Plug in the bridge soon. Transmit locks at 6%.`]
    : ['Battery low', `${p}. The bridge warns at 20% and 10% and locks transmit at 6%.`];
  return {key: 'battery', title, detail, fix: ''};
}
function faults() {
  const out = ((S.readiness && S.readiness.fault) || []).map(k => faultOf(check(k) || {key: k}));
  const b = batteryFault(); if (b) out.push(b);
  for (const f of out) if (!(f.key in since)) since[f.key] = performance.now();
  for (const k of Object.keys(since)) if (!out.some(f => f.key === k)) delete since[k];
  return out.sort(byRank);
}
function faultOf(c) {  // an unreadable PTT is not an unexpected transmit: it follows lost radio control
  const unread = c.key === 'ptt' && !/\btransmitting\b/i.test(c.detail || '');
  return {key: c.key, title: unread ? 'Transmit state unreadable' : (FAULT[c.key] || [c.title])[0], detail: c.detail || '', fix: c.fix || '',
    rank: unread ? PRIORITY.indexOf('cat') + 0.5 : PRIORITY.indexOf(c.key)};
}
const byRank = (a, b) => (a.rank ?? PRIORITY.indexOf(a.key)) - (b.rank ?? PRIORITY.indexOf(b.key));
function faultAction(f) {
  const [, sheet, label] = FAULT[f.key] || [];
  return sheet ? `<a href="#${sheet}" data-sheet="${sheet}">${esc(label)}</a>` : '';
}

// ---- the home cards ----
const STEP_WORD = {identify: 'choosing the radio', cabling: 'cables', audio_in: 'receive audio', cat: 'radio control', ptt: 'keying', ladder: 'keying'};
function setupSteps(failAt) {  // the five chips the boards draw over the wizard's six steps; failAt marks the stop red
  const w = S.wizard || {passed: []}, p = w.passed || [], has = s => p.includes(s);
  const at = {identify: 0, cabling: 1, audio_in: 1, cat: 2, ptt: 3, ladder: 4}[w.step] ?? 0;
  const done = [has('identify'), has('cabling') && has('audio_in'), has('cat'), has('ptt'), !!(w.kit && w.kit_ready)];
  return {done, at, items: ['1. Radio', '2. Cables', '3. Control', '4. Keying', '5. Kit'].map((t, i) =>
    [t, done[i] ? 'done' : i === at ? (failAt ? 'fail' : 'cur') : 'todo'])};
}
function topHTML() {
  const f = faults();
  if (f.length) {
    const o = f[0];
    const rest = f.slice(1).map(x => `<li><div><div class="t">${esc(x.title)}</div><div class="s">${esc(x.detail)} Seen ${ago(since[x.key])}.</div></div>${faultAction(x)}</li>`).join('');
    return `<section class="card b-red" role="region" aria-labelledby="hf1"><div class="head c-bad">${icon('warn', 22)}<h2 id="hf1">${esc(o.title)}</h2></div>` +
      `<div>${esc(o.detail)}</div>` + (o.fix ? `<div class="note">${esc(o.fix)}</div>` : '') + (faultAction(o) ? `<div>${faultAction(o)}</div>` : '') + '</section>' +
      (rest ? `<section class="card flush" aria-labelledby="hf2"><div class="sechead"><h2 id="hf2">Also needs attention</h2><span class="note">Most urgent first</span></div><ul class="rows">${rest}</ul></section>` : '');
  }
  const w = S.wizard; if (!w) return '';
  const p = profile();
  if (!p) return '<section class="card b-ok"><div class="head">No radio chosen</div><div class="note">Plug in the radio and its cables, then choose it here. Takes about two minutes.</div>' +
    '<a href="/setup" data-sheet="setup" data-arg=\'{"choose":true}\' class="btn primary" style="margin-top:6px">Set Up a Radio</a></section>';
  if (!w.kit_ready) {
    const s = setupSteps(true), word = STEP_WORD[w.step] || 'radio control';
    return `<section class="card b-warn" role="region" aria-label="Radio setup did not finish"><div class="head c-warn">${icon('warn', 22)}<span>Radio setup did not finish</span></div>` +
      `<div>Setting up ${esc(p.display_name)} stopped at ${esc(word)} (power was lost or the page was closed). What passed is kept.</div>` +
      stepsHTML('Radio setup progress', s.items) +
      '<a href="/setup" data-sheet="setup" data-arg=\'{"resume":true}\' class="btn primary">Continue Setup</a>' +
      '<a href="/setup" data-sheet="setup" data-arg=\'{"choose":true}\' class="btn secondary">Start Over</a></section>';
  }
  if (differentRadio()) return `<section class="card b-warn" role="region" aria-label="Kit radio not found"><div class="head c-warn">${icon('warn', 22)}<span>${esc(p.display_name)} not found</span></div>` +
    `<div>Your kit ${esc(w.kit)} expects an ${esc(p.display_name)}. It has not answered on radio control since the bridge started, so a different radio or cable may be plugged in.</div>` +
    '<div class="btnrow"><button type="button" class="btn secondary" id="recheck">Recheck Cables</button>' +
    '<a href="/setup" data-sheet="setup" data-arg=\'{"choose":true}\' class="btn primary">Use Another Radio</a></div></section>';
  return '';
}
function differentRadio() {  // the kit's radio never answered this session: not a loss, a mismatch
  const w = S.wizard, p = profile(), c = check('cat');
  return !!(w && p && w.kit && w.kit_ready && p.rig_model && c && c.state === 'fail' && !((S.readiness.fault || []).includes('cat')));
}
function rxModel() {
  const c = check('rx_audio'), L = rxLevel(), kit = S.wizard && S.wizard.kit;
  if (!profile()) return {tone: 'dim', ic: 'dot', title: 'No radio', detail: 'Set up a radio to check receive.', meter: false, aria: 'no radio chosen'};
  if (!c) return {tone: 'dim', ic: 'busy', title: 'Checking', detail: 'Waiting for the first receive check.', meter: false};
  if (keyedNow()) return {tone: 'dim', ic: 'pause', title: 'Paused', detail: 'Receive resumes when the radio stops transmitting.', meter: false};
  if (c.state === 'fail') {
    const down = /not running/.test(c.detail);
    return {tone: 'bad', ic: 'fail', title: 'No audio', border: 'b-bad', label: 'Silent', meter: true,
      detail: down ? c.detail : 'The adapter is present but nothing is arriving.',
      fixes: down ? [c.fix] : ["Check the cable is in the radio's headphone or data jack, not the microphone jack.",
        "Turn up the radio's volume or data output level.", 'Unplug the USB audio adapter and plug it back in.'],
      after: 'The card turns green by itself when audio arrives. Transmit stays locked until receive is ready.'};
  }
  if (c.state === 'warn' && /not moving/.test(c.detail)) return {tone: 'warn', ic: 'warn', title: 'Frozen', border: 'b-warn', label: 'Not moving', meter: true,
    detail: 'Audio is present but the level has stopped moving. The interface is probably stuck.',
    fixes: ['Unplug the USB audio adapter and plug it back in.', 'If it happens again, try another USB cable.']};
  if (c.state === 'warn') return {tone: 'dim', ic: 'busy', title: 'Checking', detail: c.detail, meter: true};
  if (L && (L.state === 'clip' || L.state === 'high')) return {tone: 'warn', ic: 'warn', title: 'Too loud', border: 'b-warn', meter: true,
    detail: 'Audio is above the brackets and clipping. Decodes will fail.',
    fixes: rxFixed() ? ["Turn down the radio's receive audio level. " + RX_RADIO_HINT, RX_FIXED_TEXT]
      : ["Turn down the radio's data output or headphone level.", 'Then lower the receive level slider in Receive Audio.']};
  if (L && L.state === 'low') return {tone: 'warn', ic: 'warn', title: 'Too quiet', border: 'b-warn', meter: true,
    detail: 'Audio is arriving but below the brackets. Weak signals will be missed.',
    fixes: rxFixed() ? ["Turn up the radio's receive audio level. " + RX_RADIO_HINT, RX_FIXED_TEXT]
      : ["Turn up the radio's data output or headphone level.", 'Then raise the receive level slider in Receive Audio.']};
  return {tone: 'ok', ic: 'ok', title: 'Ready', detail: 'Audio is arriving from the radio on an unknown frequency.', meter: true,
    after: rxFixed() ? 'Receive level is set on the radio.'
      : kit && L && typeof L.gain === 'number' ? `Level ${L.gain}%, from kit ${kit}. Adjust in Receive Audio.` : ''};
}
function rxHTML() {
  const m = rxModel();
  return `<a href="#receive" data-sheet="receive" class="card ${m.border || ''}" aria-label="Receive: ${esc(m.aria || m.title + '. ' + m.detail)} Open receive settings">` +
    `<div class="eyebrow"><span>${icon('rx')}Receive</span>${icon('chev', 18)}</div>` +
    `<h2 class="state c-${m.tone}">${icon(m.ic)}<span>${esc(m.title)}</span></h2><div>${esc(m.detail)}</div>` +
    (m.meter ? meterHTML(m.label).replace('data-rx', m.label ? `data-rx data-label="${esc(m.label)}"` : 'data-rx') : '') +
    (m.fixes ? '<div class="note">Try these, in order</div><ol class="fixes">' + m.fixes.map(x => `<li>${esc(x)}</li>`).join('') + '</ol>' : '') +
    (m.after ? `<p class="note">${esc(m.after)}</p>` : '') + '</a>';
}
function txModel() {
  const L = S.ladder;
  if (!radioSetUp()) return {title: 'Locked', detail: 'No radio chosen. Transmit stays locked until one is set up and its keying is proven.'};
  if (!L || L._code !== 200) return {title: 'Locked', detail: 'Transmit stays locked: the bridge has no radio control service to key through.'};
  if (!L.tx_allowed) {
    const why = rxRed() ? 'Fix receive first.' : battLocked() ? 'Battery below 6%. Plug in the bridge to transmit.' : '';
    return {title: 'Locked', detail: 'Tell the bridge what is on the antenna jack to unlock.', button: 'Unlock Transmit', why};
  }
  const lines = [];
  if (L.max_tx_s) lines.push(`Each transmission stops after ${L.max_tx_s} s, then the bridge waits ${L.max_tx_s} s before the next.`);
  else lines.push(L.load === 'dummy' ? 'On a dummy load. Nothing is radiated. Apps may key the radio with no time cap.' : 'Apps may key the radio with no time cap.');
  if (L.unverified && L.unverified.length) lines.push('Rig unverified: skipped ' + L.unverified.map(g => g === 'carrier' ? 'the antenna test' : g === 'band' ? 'the ham band check' : 'the drive check').join(' and ') + '.');
  if (L.record_error) lines.push(L.record_error);
  const why = rxRed() ? 'Fix receive first. Apps cannot key the radio until receive is ready.'
    : battLocked() ? 'Battery below 6%. Apps cannot key the radio until the bridge is plugged in.' : '';
  return {title: 'Unlocked', ok: true, detail: lines.join(' '), why, button: 'Test Again', secondary: true,
    badges: (L.load === 'dummy' ? '<span class="badge">Dummy load</span>' : '') + (L.antenna_verified ? '' : `<span class="badge plain">Power set to ${esc(fmtW(minPower()))}</span>`),
    power: !L.antenna_verified && (L.gates || []).some(g => g.key === 'carrier' && g.state === 'passed')};
}
let powerHidden = false;  // the operator hid the test-power warning; it comes back once transmit locks again
function powerHTML() {
  const w = esc(fmtW(minPower())), byHand = !(profile() || {}).rig_model;  // no radio control: the operator set it
  return `<section class="card b-warn" role="region" aria-label="Power is still at the test setting"><div class="head c-warn">${icon('warn', 20)}` +
    '<span>Power is still at the test setting</span><button type="button" class="round" data-hidepower aria-label="Hide this warning" style="margin-left:auto">' +
    `${icon('close')}</button></div><div class="note">` + (byHand
      ? `You set the radio to ${w} for the antenna test. Set the power you want on the radio before calling.`
      : `The radio was left at ${w} after the antenna test. Set the power you want on the radio before calling. The bridge could not restore it because SWR was not read over radio control.`) +
    '</div></section>';
}
document.addEventListener('click', e => {
  if (!e.target.closest('[data-hidepower]')) return;
  powerHidden = true;
  setHTML($('tx'), txHTML());
});
function txHTML() {
  const m = txModel(), dis = m.button && !m.ok && m.why;
  if (S.ladder && S.ladder._code === 200 && !S.ladder.tx_allowed) powerHidden = false;  // a failed poll is not a re-lock
  return `<section class="card" aria-labelledby="htx"><div class="eyebrow"><span>${icon('tx')}Transmit</span></div>` +
    `<h2 id="htx" class="state ${m.ok ? 'c-ok' : 'c-dim'}">${icon(m.ok ? 'unlock' : 'lock')}<span>${esc(m.title)}</span></h2>` +
    (m.badges ? `<div class="badges">${m.badges}</div>` : '') + `<div>${esc(m.detail)}</div>` +
    (m.power && !powerHidden ? powerHTML() : '') +
    (m.button ? (dis ? `<button type="button" class="btn secondary" disabled aria-describedby="htxwhy" style="margin-top:6px">${esc(m.button)}</button>`
      : `<a href="/transmit" data-sheet="transmit" class="btn ${m.secondary ? 'secondary' : 'primary'}" style="margin-top:6px">${esc(m.button)}</a>`) : '') +
    (m.why ? `<p class="why" id="htxwhy">${esc(m.why)}</p>` : '') + '</section>';
}
function address() {
  const ip = /^\d+\.\d+\.\d+\.\d+$/.test(location.hostname) ? location.hostname : (S.join && !S.join.on_ap && S.join.ip) || '10.42.0.1';
  return ip.split('/')[0];
}
function connectHTML(label) {
  const ssid = (S.wifi && S.wifi.ssid) || 'this Wi-Fi', addr = address();
  const row = (k, v) => `<div class="kv"><div><div class="k">${esc(k)}</div><div class="v mono">${esc(v)}</div></div>` +
    `<button type="button" class="round" data-copy="${esc(v)}" aria-label="Copy ${esc(v)}">${icon('copy', 18)}</button></div>`;
  return '<div class="sechead"><h2 id="hcon">Connect</h2></div><div class="secbody">' + row('Name', 'vectorbridge.local') +
    row((S.join && !S.join.on_ap ? 'Address on ' + (S.join.connection || 'this network') : 'Address on ' + ssid), addr) +
    '<p class="note">If the name does not open (Android and some browsers), type the address. A "continue to site" page on Android is expected; tap through it.</p>' +
    (label ? '<p class="note">Both are printed on the label under the bridge. The bridge light winks once every 2 s while it waits for you, and stays on once your phone has joined.</p>' : '') + '</div>';
}
document.addEventListener('click', e => {
  const b = e.target.closest('[data-copy]'); if (!b) return;
  const v = b.dataset.copy;
  const fallback = () => { const i = document.createElement('input'); i.value = v; document.body.append(i); i.select();
    try { document.execCommand('copy'); } catch (x) {} i.remove(); };
  (navigator.clipboard ? navigator.clipboard.writeText(v).catch(fallback) : Promise.resolve(fallback())).then(() => { $('live').textContent = 'Copied ' + v; });
});
// Each app names itself, its icon and its link (DOCS/app-listing.md); the bridge checked them, and this checks the link again.
const safeLink = u => typeof u === 'string' && /^[a-z][a-z0-9+.-]*:/i.test(u) && !/^(javascript|data|file|blob|vbscript|about):/i.test(u);
function appRow(a) {
  const c = check('client'), peer = a.role === 'peer';
  const [tone, word, ic] = !peer ? ['c-dim', 'Listening', 'rx'] : !c ? ['c-dim', 'Connected', 'dot']
    : c.state === 'ok' ? ['c-ok', 'Good', 'ok'] : c.state === 'warn' ? ['c-warn', 'Check', 'dot'] : ['c-dim', 'Off', 'dot'];
  const pic = a.icon_url ? `<img class="appic" src="${esc(a.icon_url)}" alt="">` : icon('app', 32, 'appic');
  const name = safeLink(a.launch_url)
    ? `<a class="t" href="${esc(a.launch_url)}" target="_blank" rel="noopener noreferrer">${esc(a.name)}</a>` : `<div class="t">${esc(a.name)}</div>`;
  const sub = [peer ? 'Sending and receiving' : 'Listening only', a.note].filter(Boolean).map(esc).join('. ');
  return `<li>${pic}<div class="grow">${name}<div class="s">${sub}</div><div class="s mono">${esc(a.addr)}</div></div>` +
    `<span class="pill ${tone}">${icon(ic)}${word}</span></li>`;
}
function appsHTML() {
  const list = S.apps, c = check('client');
  if (!list && !c) return '';
  const rows = list && list.length ? list.map(appRow).join('')
    : `<li><div><div class="t">No apps connected</div><div class="s">${esc(c ? c.detail : 'Open an app on this bridge\'s Wi-Fi.')}</div></div>` +
      `<span class="pill c-dim">${icon('dot')}Off</span></li>`;
  return `<section class="card flush" aria-labelledby="hsvc"><div class="sechead"><h2 id="hsvc">Apps</h2></div><ul class="rows">${rows}</ul></section>`;
}
function tile(o) {  // {sheet|href, label, tone, word, value, sub}
  const tone = {ok: 'c-ok', warn: 'c-warn', bad: 'c-bad', dim: 'c-dim'}[o.tone], ic = o.tone === 'ok' ? 'ok' : o.tone === 'dim' ? 'dot' : 'warn';
  const attrs = o.sheet ? `href="#${o.sheet}" data-sheet="${o.sheet}"` + (o.arg ? ` data-arg='${JSON.stringify(o.arg)}'` : '') : `href="${o.href}"`;
  return `<a ${attrs} ${o.id ? `id="${o.id}"` : ''} class="tile ${o.tone === 'warn' ? 't-warn' : o.tone === 'bad' ? 't-bad' : ''}">` +
    `<span class="tl">${esc(o.label)}<span class="pill ${tone}">${icon(ic)}${esc(o.word)}${icon('chev', 16)}</span></span>` +
    `<span class="tv">${esc(o.value)}</span><span class="ts">${esc(o.sub || '')}</span></a>`;
}
function radioTile() {
  const w = S.wizard, p = profile(), c = check('cat');
  if (!p) return tile({sheet: 'setup', arg: {choose: true}, label: 'Radio', tone: 'warn', word: 'Set up', value: 'No radio chosen', sub: 'Tap to set one up'});
  if (!w.kit_ready) return tile({sheet: 'setup', arg: {resume: true}, label: 'Radio', tone: 'warn', word: 'Setup', value: p.display_name,
    sub: `Setup ${setupSteps().done.filter(Boolean).length} of 5 done`});
  const sub = w.kit ? 'Kit ' + w.kit : 'Not saved as a kit';
  if (!p.rig_model && c && c.state === 'fail') return tile({sheet: 'radio', label: 'Radio', tone: 'bad', word: 'Check', value: p.display_name, sub: c.detail});
  if (!p.rig_model) return tile({sheet: 'radio', label: 'Radio', tone: 'dim', word: p.ptt_method === 'vox' ? 'Not used (VOX)' : 'Not used', value: p.display_name, sub});
  if (differentRadio()) return tile({sheet: 'radio', label: 'Radio', tone: 'warn', word: 'Check', value: p.display_name + ' not found', sub});
  if (c && c.state === 'fail') return tile({sheet: 'radio', label: 'Radio', tone: 'bad', word: 'Check', value: p.display_name, sub: c.detail});
  return tile({sheet: 'radio', label: 'Radio', tone: 'ok', word: 'Good', value: p.display_name, sub});
}
function powerTile() {
  const b = S.battery || {}, m = S.mode;
  const src = b.present ? (b.plugged === true ? 'plugged in' : b.plugged === false ? 'on battery' : '') : '';
  const sub = [typeof b.volts === 'number' ? b.volts.toFixed(2) + ' V' : '', src].filter(Boolean).join(', ');
  const lv = b.level || 'ok', tone = m === 'maintenance' ? 'warn' : lv === 'low' ? 'warn' : lv === 'ok' ? (m ? 'ok' : 'dim') : 'bad';
  const word = m === 'maintenance' ? 'Check' : lv === 'ok' ? (m ? 'Good' : 'Unknown') : lv === 'low' ? 'Low' : 'Critical';
  return tile({sheet: 'power', label: 'Power', tone, word, value: m === 'maintenance' ? 'Do not pull power' : m === 'field' ? 'Safe to unplug' : 'Mode not reported', sub});
}
function wifiTile() {
  const w = S.wifi || {}, j = S.join;
  if (j && !j.on_ap) return tile({sheet: 'wifi', label: 'Wi-Fi', tone: 'warn', word: 'Check', value: j.connection || 'Another network', sub: 'Bridge Wi-Fi is off'});
  return tile({sheet: 'wifi', label: 'Wi-Fi', tone: w.ssid ? 'ok' : 'dim', word: w.ssid ? 'Good' : 'Unknown', value: w.ssid || 'Bridge Wi-Fi', sub: 'Secured'});
}
function alertsTile() {
  const armed = $('arm').hidden;
  return armed ? tile({href: '/alerts', label: 'Alerts', tone: 'ok', word: 'Good', value: 'Sound on', sub: 'Choose what alerts you', id: 'alerttile'})
    : tile({href: '/alerts', label: 'Alerts', tone: 'warn', word: 'Check', value: 'Sound off', sub: 'Tap to turn on here', id: 'alerttile'});
}
document.addEventListener('click', e => {  // phones only play sound after a tap: the tile is that tap
  const t = e.target.closest('#alerttile'); if (t && !$('arm').hidden) { e.preventDefault(); armSound(); render(); }
});
function liveText() {
  const f = faults(); if (f.length) return f[0].title + (f.length > 1 ? `. ${f.length - 1} more listed below it.` : '.');
  if (!profile()) return 'No radio chosen. Set up a radio to check receive and transmit.';
  if (S.wizard && !S.wizard.kit_ready) return `Radio setup did not finish. Continue setup at ${STEP_WORD[S.wizard.step] || 'radio control'}.`;
  if (differentRadio()) return `Different radio plugged in. Kit ${S.wizard.kit} expects an ${profile().display_name}.`;
  return `Receive ${rxModel().title.toLowerCase()}. Transmit ${txModel().title.toLowerCase()}.`;
}
function renderHeader() {
  const b = S.battery;
  const el = $('batt'), none = $('battnone'), btn = $('battbtn');
  if (b && b.present) {
    el.hidden = false; none.hidden = true; el.textContent = b.percent + '%';
    $('batticon').style.color = b.level === 'ok' || !b.level ? 'var(--ok)' : b.level === 'low' ? 'var(--warn)' : 'var(--bad-text)';
    btn.setAttribute('aria-label', `Battery ${b.percent} percent, ${b.plugged ? 'plugged in' : b.plugged === false ? 'not charging' : 'charging not reported'}${b.level && b.level !== 'ok' ? ', low' : ''}. Open power settings`);
  } else { el.hidden = true; none.hidden = false; btn.setAttribute('aria-label', 'Battery not yet reported. Open power settings'); }
  $('maint').hidden = $('mode').hidden = S.mode !== 'maintenance';
  $('cfgbad').hidden = !(S.readiness && S.readiness.config_problem);
}
function render() {
  renderHeader();
  if (FIRST_BOOT) { renderFirstBoot(); return; }
  setHTML($('top'), topHTML());
  setHTML($('rx'), rxHTML());
  setHTML($('tx'), txHTML());
  setHTML($('connect'), connectHTML(false));
  setHTML($('apps'), appsHTML());
  setHTML($('station'), '<h2 class="station">Station</h2><div class="tiles">' + radioTile() + powerTile() + wifiTile() + alertsTile() + '</div>');
  const rc = $('recheck'); if (rc) rc.onclick = async () => { rc.disabled = true; await post('/api/wizard', {action: 'goto', step: 'cat'}); await post('/api/wizard', {action: 'advance'}); refresh(); };
  const lt = liveText(); if ($('live').textContent !== lt) $('live').textContent = lt;
  paintMeters();
}

// ---- first boot: the access point is still open, so only /wifi and /api/wifi answer ----
let FIRST_BOOT = false;
function renderFirstBoot() {
  const ssid = (S.wifi && S.wifi.ssid) || 'VectorBridge';
  setHTML($('top'), `<section class="card b-warn" role="region" aria-label="First step: secure this Wi-Fi"><div class="head c-warn">${icon('warn', 22)}<span>First step: secure this Wi-Fi</span></div>` +
    `<div>This bridge is on an open Wi-Fi named ${esc(ssid)}. Anyone nearby can join it. Set a passphrase to enable setup and transmit.</div>` +
    (S.wifi && S.wifi.pending ? '<p class="note">A change is waiting; the Wi-Fi is about to come back with its passphrase.</p>' : '') +
    '<a href="#secure" data-sheet="secure" class="btn primary" style="margin-top:6px">Secure This Wi-Fi</a></section>');
  setHTML($('rx'), `<div class="card" aria-label="Receive: Waiting until this Wi-Fi is secured."><div class="eyebrow"><span>${icon('rx')}Receive</span></div>` +
    `<h2 class="state c-dim">${icon('dot')}<span>Waiting</span></h2><div>Waits until this Wi-Fi is secured.</div></div>`);
  setHTML($('tx'), `<section class="card" aria-labelledby="htx"><div class="eyebrow"><span>${icon('tx')}Transmit</span></div>` +
    `<h2 id="htx" class="state c-dim">${icon('lock')}<span>Locked</span></h2><div>The bridge refuses to key while this Wi-Fi is open. Secure the Wi-Fi first.</div>` +
    '<button type="button" class="btn secondary" disabled aria-describedby="htxwhy" style="margin-top:6px">Unlock Transmit</button><p class="why" id="htxwhy">Waits on Wi-Fi.</p></section>');
  setHTML($('connect'), connectHTML(true));
  setHTML($('apps'), '');
  setHTML($('station'), '<p class="note">Until the Wi-Fi is secured every page opens here. Settings cannot be saved and the bridge will not key the radio.</p>');
  $('live').textContent = 'First step: secure this Wi-Fi. Receive and transmit wait on it.';
}

// ---- alerts: interrupt only when something that was green turned red, or the battery crosses a threshold ----
let seenSeq = null, battSeen = 'ok';
const BATT_RANK = {ok: 0, low: 1, critical: 2, tx_locked: 3};
function showAlert(list) {
  const o = list[0], rest = list.slice(1);
  $('alert').innerHTML = `<div class="scrim" aria-hidden="true" style="position:fixed;z-index:25"></div><section class="dlg bad" role="alertdialog" aria-modal="true" aria-labelledby="alh" style="position:fixed;z-index:26">` +
    `<div class="head c-bad">${icon('warn', 22)}<h2 id="alh" tabindex="-1">${esc(o.title)}</h2></div><p>${esc(o.detail)}</p>` + (o.fix ? `<p class="note">${esc(o.fix)}</p>` : '') +
    (rest.length ? '<div class="box"><h3>Also</h3><ul>' + rest.map(x => `<li>${esc(x.title)}: ${esc(x.detail)}</li>`).join('') + '</ul></div>' : '') +
    '<button type="button" class="btn primary" id="aldone">OK</button></section>';
  $('alert').hidden = false; $('alh').focus();
  $('aldone').onclick = () => { $('alert').hidden = true; $('alert').innerHTML = ''; };
  notify(list.map(x => x.key));
}
function checkAlerts() {
  const r = S.readiness, list = [];
  if (r && seenSeq !== null && r.alert_seq > seenSeq)
    for (const c of r.alert) list.push(faultOf(c));
  if (r) seenSeq = r.alert_seq;
  const b = batteryFault(), lv = (S.battery && S.battery.level) || 'ok';
  if (b && BATT_RANK[lv] > BATT_RANK[battSeen]) list.push(b);
  battSeen = lv;
  if (list.length) showAlert(list.sort(byRank));
}

// ---- the built-in Wi-Fi notice: once per page load while the Pi's own radio carries the network, until "Got it" ----
const ADAPTER_TIP = 'vb-adapter-tip-seen';
function showAdapterTip() {
  const j = S.join;
  if (FIRST_BOOT || !j || !j.radio || j.radio_usb !== false || !$('alert').hidden) return;
  try { if (localStorage.getItem(ADAPTER_TIP)) return; } catch (e) {}
  $('alert').innerHTML = `<div class="scrim" aria-hidden="true" style="position:fixed;z-index:25"></div><section class="dlg" role="dialog" aria-modal="true" aria-labelledby="tiph" style="position:fixed;z-index:26">` +
    `<div class="head">${icon('wifi', 22)}<h2 id="tiph" tabindex="-1">Better Wi-Fi with a USB adapter</h2></div>` +
    '<p>The Raspberry Pi\'s built-in Wi-Fi is 2.4 GHz only, and it drops audio under load. On the bench up to 1.6% of audio packets failed to send, in stalls of up to 3 seconds. Through a USB adapter on 5 GHz, 0 to 0.03% failed.</p>' +
    '<div class="box"><h3>We recommend a USB Wi-Fi adapter</h3><ul>' +
    '<li>Plug it into any USB port. Within a few seconds the bridge moves its Wi-Fi to the adapter, on 5 GHz when it can. Phones rejoin by themselves.</li>' +
    '<li>It must support access point mode and have a Linux driver. Tested: Panda AC600.</li>' +
    '<li>Unplug it and the bridge goes back to the built-in Wi-Fi.</li></ul></div>' +
    '<p class="note">More in <a href="/docs/field-network">the field network doc</a>.</p>' +
    '<button type="button" class="btn primary" id="tipdone">Got It</button></section>';
  $('alert').hidden = false; $('tiph').focus();
  $('tipdone').onclick = () => {
    try { localStorage.setItem(ADAPTER_TIP, '1'); } catch (e) {}
    $('alert').hidden = true; $('alert').innerHTML = '';
  };
}

// ---- polling ----
function afterPoll() { render(); if (cur && SHEETS[cur.name].update) SHEETS[cur.name].update(); }
async function pollReadiness() { const r = await getJSON('/api/readiness'); if (r && r.ok) S.readiness = r; checkAlerts(); afterPoll(); }
async function pollApps() { const a = await getJSON('/api/apps'); S.apps = a && a.ok ? a.apps : null; afterPoll(); }
async function pollWizard() { const w = await getJSON('/api/wizard'); if (w && w.ok) S.wizard = w; afterPoll(); }
async function pollLadder() { S.ladder = await getJSON('/api/ladder'); afterPoll(); }
async function pollStatus() {
  const t = await getJSON('/api/status');
  S.status = t && t._code === 200 ? t : null; if (S.status) noteStatus(S.status);
  paintMeters(); if (cur && SHEETS[cur.name].level) SHEETS[cur.name].level();
}
async function pollSlow() {
  const [b, m, w, j] = await Promise.all([getJSON('/api/battery'), getJSON('/api/mode'), getJSON('/api/wifi'), getJSON('/api/join')]);
  if (b && b.ok) S.battery = b; if (m && m.ok) S.mode = m.mode; if (w && w.ok) S.wifi = w; if (j && j.ok) S.join = j;
  checkAlerts(); afterPoll();
}
async function refresh() { await Promise.all([pollReadiness(), pollWizard(), pollLadder(), pollApps(), pollSlow()]); }
async function boot() {
  S.wifi = await getJSON('/api/wifi');
  if (S.wifi && S.wifi.unsecured) {
    FIRST_BOOT = true; render();
    if (location.hash === '#secure') openSheet('secure');
    setInterval(async () => {  // once secured, every page answers again: reload into the normal home page
      const w = await getJSON('/api/wifi'); if (w && w.ok) { S.wifi = w; if (!w.unsecured) location.replace('/'); else afterPoll(); }
    }, 5000);
    return;
  }
  await refresh();
  const want = location.hash.slice(1) || (location.pathname.startsWith('/wifi') ? 'wifi' : '');
  if (want && SHEETS[want]) openSheet(want, {});
  else showAdapterTip();
  setInterval(pollReadiness, 2000); setInterval(pollLadder, 2000); setInterval(pollApps, 2000); setInterval(pollWizard, 5000);
  setInterval(pollStatus, 250); setInterval(pollSlow, 30000);
}
"""

HOME = ("""<!doctype html>
<html lang="en" data-theme="dark">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>VectorBridge</title>
""" + HEAD_TAGS + """<script>try { const p = localStorage.getItem('vb-theme') || 'auto';
document.documentElement.dataset.theme = p === 'auto' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : p; } catch (e) {}</script>
""" + STYLE + "\n<body>\n" + BODY.replace("__VERSION__", html.escape(version_text())) + "<script>" + HOME_SCRIPT
        + SHELL_JS.replace("__PRIORITY__", json.dumps(list(FAULT_PRIORITY)))
        + SETUP_JS + TRANSMIT_JS + WIFI_JS + STATION_JS + "\nboot();\n</script>\n</body>\n</html>\n")
