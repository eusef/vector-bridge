"""The alert settings page, and the script the home screen uses to sound and vibrate.

Everything is generated in the browser: the tone is Web Audio, so no sound file ships
and nothing is fetched from outside the bridge.
"""

# Tags that let iOS "Add to Home Screen" open the bridge full screen as a web app.
HEAD_TAGS = """<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="VectorBridge">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
"""

# Browsers only allow sound after a tap, so the home screen shows one "Turn on alert sound" button.
# notify(keys) is called by the home screen with the keys of the checks that just turned red.
HOME_SCRIPT = """
let actx = null, alertCfg = null;
async function loadAlertCfg() { try { alertCfg = (await (await fetch('/api/alerts', {cache: 'no-store'})).json()).events; } catch (e) {} }
function armSound() {
  try { actx = actx || new (window.AudioContext || window.webkitAudioContext)(); actx.resume(); tone(0.15); } catch (e) {}
  document.getElementById('arm').hidden = true;
}
function tone(seconds) {
  if (!actx) return;
  if (actx.state !== 'running') { actx.resume(); document.getElementById('arm').hidden = false; }  // iOS suspends it
  const o = actx.createOscillator(), g = actx.createGain();
  o.type = 'square'; o.frequency.value = 880; g.gain.value = 0.4; o.connect(g); g.connect(actx.destination);
  o.start(); o.stop(actx.currentTime + seconds);
}
function notify(keys) {
  const want = ch => keys.some(k => !alertCfg || !alertCfg[k] || alertCfg[k][ch]);
  if (want('sound')) { tone(0.2); setTimeout(() => tone(0.2), 350); setTimeout(() => tone(0.5), 700); }
  if (want('vibrate') && navigator.vibrate) navigator.vibrate([200, 100, 200, 100, 500]);
}
loadAlertCfg(); setInterval(loadAlertCfg, 30000);
"""

ALERTS = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Alerts - VectorBridge</title>
<style>
  body { margin: 0; padding: 14px; background: #12151a; color: #e7ecf3;
         font: 16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
  a { color: #5ac8fa; } nav { color: #8b97a8; font-size: 13px; margin-bottom: 12px; }
  .card { background: #1b2029; border-radius: 12px; padding: 14px; margin-bottom: 12px; }
  table { width: 100%; border-collapse: collapse; } th, td { padding: 9px 4px; text-align: center; }
  th:first-child, td:first-child { text-align: left; } th { color: #8b97a8; font-size: 13px; font-weight: normal; }
  input { width: 22px; height: 22px; } .muted { color: #8b97a8; font-size: 14px; }
  .bad { background: #4a1f1f; padding: 10px; border-radius: 9px; } h2 { margin: 0 0 8px; font-size: 19px; }
</style>
<nav><a href="/">Home</a> · <a href="/setup">Setup</a> · <a href="/docs/">Docs</a></nav>
<div class="card"><h2>Alerts</h2>
<p class="muted">An alert fires when something that was working stops. Choose how each one reaches you.</p>
<div id="grid">Connecting to the bridge...</div><div id="err" class="bad" hidden></div></div>
<div class="card muted"><p>Phone sound and vibration need the home screen open on this phone, with
"Turn on alert sound" tapped once. iPhones do not vibrate for web pages, so rely on sound there.</p>
<p>To keep it one tap away on an iPhone: Share, then Add to Home Screen.</p>
<p>The bridge beeper and the status light are described in <a href="/docs/alerts">the alerts doc</a>.</p></div>
<script>
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => "&#" + c.charCodeAt(0) + ";");
const LABEL = {sound: "Phone sound", vibrate: "Phone vibrate", beep: "Bridge beep"};
function render(r) {
  let h = "<table><tr><th>When</th>" + r.channels.map(c => "<th>" + esc(LABEL[c] || c) + "</th>").join("") + "</tr>";
  for (const [e, title] of Object.entries(r.titles))
    h += "<tr><td>" + esc(title) + "</td>" + r.channels.map(c => "<td><input type='checkbox' data-e='" + esc(e) +
         "' data-c='" + esc(c) + "'" + (r.events[e][c] ? " checked" : "") + "></td>").join("") + "</tr>";
  document.getElementById("grid").innerHTML = h + "</table>";
  document.getElementById("err").hidden = !r.save_error; document.getElementById("err").textContent = r.save_error;
  for (const box of document.querySelectorAll("input")) box.onchange = () =>
    send({[box.dataset.e]: {[box.dataset.c]: box.checked}});
}
async function send(events) {
  try { render(await (await fetch("/api/alerts", {method: "POST", headers: {"Content-Type": "application/json"},
                                                  body: JSON.stringify({events})})).json()); }
  catch (e) { document.getElementById("err").hidden = false;
              document.getElementById("err").textContent = "The bridge did not answer. Check you are still on its Wi-Fi."; }
}
fetch("/api/alerts").then(r => r.json()).then(render).catch(() => {});
</script>
"""
