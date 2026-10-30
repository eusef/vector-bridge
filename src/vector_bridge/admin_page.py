"""The admin PIN prompt, added to every page the tuner serves (roadmap 1.1, admin.py).

It wraps `fetch`: when a POST comes back `401 pin_required`, it explains the PIN, asks for it
(or asks for a new one on a card with none yet), logs in, and retries the save once. The
session is an HttpOnly cookie, so page code never sees it. `vbAdmin.change()` changes the PIN.
"""

from __future__ import annotations

ADMIN_JS = r"""
(() => {
  const nativeFetch = window.fetch.bind(window);
  const OWN = ['/api/login', '/api/pin'];
  const WHY = '<p>Anyone who joins this bridge\'s Wi-Fi can open these pages. The admin PIN keeps them from changing ' +
    'your radio, transmit, Wi-Fi or alert settings, or keying your radio, unless they know it.</p>';
  const LIMITS = '<p class="vbp-note">It is a lock on the settings, not encryption: anyone on this Wi-Fi can still ' +
    'hear the receive audio. Forgot the PIN? Connect a monitor and keyboard to the bridge and choose ' +
    '"Reset the admin PIN".</p>';
  const CSS = '.vbp-scrim{position:fixed;inset:0;z-index:9999;background:var(--scrim,rgba(0,0,0,.55));display:flex;' +
    'align-items:center;justify-content:center;padding:16px}' +
    '.vbp-card{background:var(--surface,var(--panel,#1a1f1c));color:var(--text,#fff);border:1px solid var(--line,#2a312c);' +
    'border-radius:12px;max-width:420px;width:100%;max-height:100%;overflow:auto;padding:20px;box-sizing:border-box;' +
    'font:16px/1.4 -apple-system,system-ui,sans-serif;display:flex;flex-direction:column;gap:10px}' +
    '.vbp-card h2{margin:0;font-size:20px}.vbp-card p{margin:0}.vbp-note{color:var(--dim,#9e9e9e);font-size:14px}' +
    '.vbp-card label{font-size:14px;color:var(--dim,#9e9e9e)}' +
    '.vbp-card input{font:inherit;font-size:22px;letter-spacing:4px;min-height:44px;padding:0 12px;border-radius:8px;' +
    'border:1px solid var(--line,#2a312c);background:var(--field,rgba(0,0,0,.2));color:inherit;width:100%;box-sizing:border-box}' +
    '.vbp-err{color:var(--bad-text,#f25a54);min-height:1.4em}.vbp-row{display:flex;gap:8px;justify-content:flex-end}' +
    '.vbp-row button{font:inherit;min-height:44px;padding:0 16px;border-radius:8px;border:1px solid var(--line,#2a312c);' +
    'background:transparent;color:inherit}.vbp-row .vbp-go{background:var(--ok,#3fbf4f);color:var(--on-accent,#0e1210);border:0}';
  let busy = null;  // one prompt at a time; refusals that arrive meanwhile wait for it

  async function postJSON(path, body) {
    try {
      const r = await nativeFetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      const b = await r.json(); b._code = r.status; return b;
    } catch (e) { return {ok: false, error: 'no_answer', _code: 0}; }
  }

  // mode: 'login' (enter the PIN), 'create' (first PIN) or 'change' (new PIN, needs a session).
  function prompt(mode) {
    return new Promise(done => {
      if (!document.getElementById('vbp-css')) {
        const st = document.createElement('style'); st.id = 'vbp-css'; st.textContent = CSS; document.head.appendChild(st);
      }
      const scrim = document.createElement('div');
      scrim.className = 'vbp-scrim';
      let thenChange = false;  // a change refused for want of a session: log in, then back to the change
      const render = m => {
        mode = m;
        const two = m !== 'login';
        const lead = m === 'login'
          ? '<p>Enter the PIN you chose. This browser then stays unlocked for 12 hours of use, or until the bridge restarts.</p>'
          : m === 'create'
            ? '<p>This bridge has no PIN yet. Choose 4 to 8 digits. You will need it here and in apps that key or tune ' +
              'the radio, so pick one you will remember.</p>'
            : '<p>Choose a new PIN of 4 to 8 digits. Apps that use the old PIN will ask for the new one.</p>';
        scrim.innerHTML = '<div class="vbp-card" role="dialog" aria-modal="true" aria-labelledby="vbp-h">' +
          `<h2 id="vbp-h">${m === 'change' ? 'Change Admin PIN' : m === 'create' ? 'Create an Admin PIN' : 'Admin PIN'}</h2>` +
          WHY + lead +
          `<label for="vbp-1">${two ? 'New PIN' : 'PIN'}</label>` +
          '<input id="vbp-1" type="password" inputmode="numeric" pattern="[0-9]*" maxlength="8" autocomplete="off">' +
          (two ? '<label for="vbp-2">Type it again</label>' +
            '<input id="vbp-2" type="password" inputmode="numeric" pattern="[0-9]*" maxlength="8" autocomplete="off">' : '') +
          '<p class="vbp-err" id="vbp-e" role="alert"></p>' + LIMITS +
          '<div class="vbp-row"><button type="button" id="vbp-no">Cancel</button>' +
          `<button type="button" class="vbp-go" id="vbp-ok">${two ? 'Save PIN' : 'Unlock'}</button></div></div>`;
        const ok = scrim.querySelector('#vbp-ok'), err = scrim.querySelector('#vbp-e');
        scrim.querySelector('#vbp-no').onclick = () => finish(false);
        scrim.querySelectorAll('input').forEach(i => { i.onkeydown = e => { if (e.key === 'Enter') ok.click(); }; });
        ok.onclick = async () => {
          const p1 = scrim.querySelector('#vbp-1').value, p2 = two ? scrim.querySelector('#vbp-2').value : p1;
          if (!/^[0-9]{4,8}$/.test(p1)) { err.textContent = 'Use 4 to 8 digits.'; return; }
          if (p1 !== p2) { err.textContent = 'The two PINs differ.'; return; }
          ok.disabled = true; err.textContent = '';
          const r = await postJSON(two ? '/api/pin' : '/api/login', {pin: p1});
          ok.disabled = false;
          if (r.ok && m === 'login' && thenChange) { thenChange = false; return render('change'); }
          if (r.ok) return finish(true);
          if (r.error === 'pin_unset') return render('create');
          if (r.error === 'pin_required') {  // someone created a PIN meanwhile, or the session ended
            thenChange = m === 'change';
            return render('login');
          }
          err.textContent = r.error === 'bad_pin' ? 'That PIN is wrong.'
            : r.error === 'locked' ? `Too many wrong PINs. Try again in ${r.retry_s} s.`
            : r.error === 'no_answer' ? 'The bridge did not answer. Check you are still on its Wi-Fi.'
            : (r.fix || r.error || 'The bridge refused it.');
        };
        scrim.querySelector('#vbp-1').focus();
      };
      const finish = v => { scrim.remove(); done(v); };
      document.body.appendChild(scrim);
      render(mode);
    });
  }

  function once(mode) {
    if (!busy) busy = prompt(mode).finally(() => { busy = null; });
    return busy;
  }

  window.fetch = async function (input, init) {
    const r = await nativeFetch(input, init);
    const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    if (method !== 'POST' || r.status !== 401) return r;
    const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
    if (OWN.includes(path)) return r;
    let b = null;
    try { b = await r.clone().json(); } catch (e) { return r; }
    if (!b || b.error !== 'pin_required') return r;
    if (!(await once(b.pin_set ? 'login' : 'create'))) {
      return new Response(JSON.stringify({ok: false, error: 'Admin PIN needed. Nothing was changed.'}),
                          {status: 401, headers: {'Content-Type': 'application/json'}});
    }
    return nativeFetch(input, init);
  };

  window.vbAdmin = {
    async change() {
      let s = null;
      try { s = await (await nativeFetch('/api/pin', {cache: 'no-store'})).json(); } catch (e) { return false; }
      if (!s.set) return once('create');
      if (!s.unlocked && !(await once('login'))) return false;
      return once('change');
    },
  };
})();
"""

_TAG = ("<script>" + ADMIN_JS + "</script>\n").encode()


def inject(page: bytes) -> bytes:
    """The prompt script just before `</body>`, or at the end of a page without one."""
    i = page.rfind(b"</body>")
    return page[:i] + _TAG + page[i:] if i >= 0 else page + _TAG
