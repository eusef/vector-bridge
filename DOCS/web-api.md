# Web pages and HTTP API

The web server (`vector-bridge-tuner.service`, `src/vector_bridge/tuner.py`) listens on TCP port 80. It serves the operator pages and the JSON endpoints behind them. Audio does not go through it: audio and the `hello` and `status` commands are UDP, in `DOCS/protocol.md`. The UDP settings commands (`set_gain` and the like) answer only on the Pi itself, so an app sets levels and the transmit audio cap here, with the PIN.

Every POST needs the admin PIN (below). GETs need nothing: anyone who can reach port 80 can read every page and read-only endpoint.

## Admin PIN

The owner chooses a PIN of 4 to 8 digits. It guards every POST, so someone else on the bridge Wi-Fi (or on a joined network) cannot change the radio, transmit, Wi-Fi or alert settings, key or tune the radio, or list an app. It does not encrypt anything: UDP audio on 5100 stays open to anyone on the network (`DOCS/protocol.md`).

| Step | Request | Reply |
|---|---|---|
| Log in | `POST /api/login` `{"pin": "1234"}`; an app adds `"app": "<its name>"` (below) | `200 {"ok":true,"token":"<32 hex>","device_id":"<16 hex>"}` plus `Set-Cookie: vb_admin=<token>; Path=/; HttpOnly; SameSite=Strict`. With `app`: an app token, the reply echoes `app`, and there is no cookie. `400 bad_app` when `app` is not 1 to 64 printable characters; `500 save_failed` when an app token could not be written; `401 bad_pin`; `429 {"error":"locked","retry_s":N}` after 5 wrong PINs in a row (the wait starts at 60 s and doubles with each further wrong PIN, up to an hour; the right PIN resets it); `409 pin_unset` when the card has no PIN yet |
| Every other POST | send the token: browsers send the cookie by themselves, native apps send `Authorization: Bearer <token>` | without a valid token: `401 {"ok":false,"error":"pin_required","pin_set":true\|false}` before the handler runs; nothing changes |
| Create or change the PIN | `POST /api/pin` `{"pin": "<4 to 8 digits>"}`, optional `app` as for login | creating the first PIN needs no token; changing it needs one. `200` with a fresh token, `device_id` and cookie (an app token and no cookie with `app`), and every other session and app token ends. `400 bad_pin_format`; `401 pin_required` to change without a token; `500 save_failed` (with `detail`) when the card could not be written |
| Ask | `GET /api/pin` | `set` (a PIN exists), `unlocked` (this caller's token is valid) |
| List app tokens | `GET /api/tokens` with a token | `tokens`: `[{"id":"<8 hex>","app":"VectorComm","issued":<unix s>}]`, oldest first; never the tokens themselves. `401 pin_required` without a token |
| Revoke an app token | `POST /api/tokens` `{"revoke": "<id>"}` | `200 {"ok":true,"revoked":"<id>"}`; `404 no_such_token` |

- A browser session (login without `app`) is not tied to an address. It ends when the tuner restarts, when the PIN changes or is reset, or after 12 hours unused (each POST, and each `GET /api/pin` that carries it, restarts the 12 hours). At most 32 sessions are kept; the least recently used goes first.
- An app token (login with `app`) lasts until the PIN changes, the PIN is reset at the console, or the owner revokes it. A restart and idle time do not end it. The bridge keeps only its SHA-256, in `/persist/vector-bridge/app-tokens.json` (mode 0600). At most 16 are kept; a new one past that ends the oldest. `app` is a label for the token list; something like "VectorComm on iPad" tells two installs apart.
- A card with no PIN refuses every POST until one is created. The web pages ask for one at the first save, and explain what it is for.
- While the access point is still open (first boot), the Wi-Fi form posts with no PIN, and every other POST, `/api/login` and `/api/pin` included, gets `403 wifi_open`.
- Forgotten PIN: connect an HDMI monitor and a USB keyboard (the Pi Zero 2 W has one USB data port, so unplug the radio) and choose "Reset the admin PIN" on the console menu, or run `sudo vector-bridge pin reset`. The hash is deleted, every session and app token ends, and the next save on the web page asks for a new PIN. `sudo vector-bridge pin status` says whether one is set.
- The PIN is stored only as a salted PBKDF2-SHA256 hash, in `/persist/vector-bridge/admin-pin.json` (mode 0600).

### How a native app logs in

1. Read `device_id` from `GET /api/status` (no token needed). It is a hash of the Pi's serial number, so it stays the same when the owner renames the Wi-Fi, and two bridges never share it. It is `null` only off a Pi.
2. If the app holds a token stored under that `device_id`, send it as `Authorization: Bearer <token>`. Never send a token stored under another `device_id`: every bridge's access point is 10.42.0.1, so the address alone does not say which bridge this is.
3. With no stored token, or on `401 pin_required`, ask the user for the PIN once, `POST /api/login` with `pin` and `app`, store the token with the `device_id` from the login reply (for example in the Keychain), retry once, and forget the PIN. On `bad_pin`, `locked` or `pin_unset` stop and tell the user; on `pin_set:false`, tell them to set a PIN on the bridge page.

Someone who copies a bridge's `device_id` onto a fake bridge could still collect a token; stopping that needs TLS or a PAKE login, which this version does not have.

### The same token on port 4532 (rig control)

Every client on port 4532 that is not on the Pi itself (loopback) must log in before anything reaches the radio, whatever the rig control access setting (`GET /api/rigctl`): the setting says where the port answers, the admin PIN says who may use it (the maintainer ruled 2026-10-28; before that only tailnet clients under `all` logged in). So WSJT-X, JS8Call and fldigi cannot connect to the bridge directly; VectorCable relays them (it listens on the computer's own 127.0.0.1:4532 and logs in upstream). A login line from a loopback client is answered `RPRT 0` and goes no further, so an app may always send it to a bridge whose `GET /api/rigctl` has `"vb_auth": true`. Never send it to one without that field: it passes the line to rigctld.

- First line: `\vb_auth <token>\n`, the app token from step 3, exactly two words separated by a space. Only an app token works; a browser session token does not.
- Accepted: the reply is `RPRT 0\n`, then the connection is plain Hamlib NET rigctl.
- Refused: a wrong, revoked or missing token, or any other first line, gets `RPRT -9\n` and the bridge closes the connection. So does a client that sends no line within 5 s, and a login while 8 apps are already connected. Nothing it sent reaches rigctld. If rigctld is not answering when the token is accepted, the reply is `RPRT -6\n` and the connection closes.
- At most 2 clients can be waiting to log in at once; a third is closed at once. Waiting clients do not take any of the 8 app slots.
- A logged-in client is checked again every 5 s. If its token is revoked or the PIN changes, the bridge closes it, unkeying the radio if it was keyed.

WSJT-X and JS8Call cannot send this line. VectorCable relays them: they connect to VectorCable on the Mac, and it logs in to 4532 for them.

## Pages

| Path | Page |
|---|---|
| `/` | readiness home screen (`DOCS/readiness.md`) |
| `/setup` | `302` to `/#setup`: the radio setup sheet on the home page (`DOCS/setup-wizard.md`) |
| `/transmit` | `302` to `/#transmit`: the Unlock Transmit sheet on the home page (`DOCS/transmit-checks.md`) |
| `/alerts` | alert settings (`DOCS/alerts.md`) |
| `/wifi` | the home page with the Wi-Fi sheet open (name, passphrase, QR code, joining another network, `DOCS/field-network.md`); while the access point is open, the first-boot home page |
| `/tuner` | level tuner: dBFS meters, clip latch, gain sliders, mixer table |
| `/docs/`, `/docs/<name>` | these documents, rendered from the card |
| `/manifest.json`, `/icon.png` | web app manifest and icon for Add to Home Screen |
| `/wifi-qr.svg` | QR code of the access point credentials; 404 while the access point is open |

## Redirects

| Case | Answer |
|---|---|
| A connectivity probe (Apple, Android, Windows, Firefox, NetworkManager) or a hostname that is not the bridge | `302` to the home page: the captive portal |
| The access point is still open (first boot) | every GET except `/wifi`, `/api/wifi` and `/wifi-qr.svg` gets `302` to `/wifi`; every POST except `/api/wifi` gets `403 {"ok":false,"error":"wifi_open"}` |
| A POST from another site: the Host is not the bridge (a local name like `vectorbridge.local`/`.lan`, or an address), an `Origin` header names a different host than `Host` (`Origin: null` included), or `Sec-Fetch-Site: cross-site` | `403 {"ok":false,"error":"forbidden_origin"}` before any handler runs; clients that send no `Origin` (curl, the iPad app) are unaffected |
| A POST without the admin PIN's token | `401 {"ok":false,"error":"pin_required","pin_set":...}` after the cross-site check, before any handler runs |

## JSON endpoints

Every reply is a JSON object with a boolean `ok`. POST bodies are JSON objects; anything else gets `400 {"ok":false,"error":"bad_json"}`.

### Read only

| GET | Reply |
|---|---|
| `/api/readiness` | `verdict` (`ready`, `ready_with_caveats`, `not_ready`), `verdict_text`, `checks` (each `key`, `title`, `state`, `detail`, `fix`), `alert_seq`, `alert`, `fault`; `alert` and `fault` are most urgent first (`DOCS/readiness.md`) |
| `/api/status` | the UDP `status` telemetry plus `rx_dbfs`, `tx_dbfs`, `rx_rms_dbfs`, `tx_rms_dbfs`, `target_low_dbfs`, `target_high_dbfs`, and `device_id` (see Admin PIN); `503 {"error":"service_unreachable","device_id":...}` if the audio service does not answer |
| `/api/battery` | `present`; when a PiSugar answers on I2C also `model`, `percent`, `plugged` (on PiSugar 2, which also adds `volts`, inferred from a 15 mV voltage step and `null` until power first changes), `level` (`ok`, `low` at 20% or less, `critical` at 10% or less, `tx_locked` at 6% or less; always `ok` when plugged in) and `tx_locked`. The home page shows it top right |
| `/api/mode` | `mode`: `field` (read-only root, safe to pull power), `maintenance` (writable root, don't pull power), or `null` off a Pi. The home page shows it top right |
| `/api/mixers` | `card` and its ALSA `controls` |
| `/api/rigctl` | who can reach the rigctl safety proxy on port 4532: `access` (`off`, `ap`, `wifi` or `all`), `port`, `listening` (the addresses it listens on; `wifi` shows `0.0.0.0`), `vb_auth` (`true`: port 4532 takes the `\vb_auth` login above, whatever `access` is; a bridge without the field passes that line to rigctld, which runs the token's letters as commands, so an app sends it only when this is `true`), `login_required` (`true`: every client but loopback must log in, whatever `access` is; absent on an older bridge, where only tailnet clients under `all` did); `503 no_rigctl_proxy` when the proxy is not running |
| `/api/hamlib/models` | every radio the card's Hamlib knows, from `rigctl -l`: `models` as `{model, mfg, name, status}` sorted by maker; empty when rigctl is missing |
| `/api/hamlib/conf?model=N` | the settings the card's Hamlib takes for model `N`, from `rigctl -m N -L`: `params` as `{name, desc, default, kind}` (`kind` is `range` with `min`/`max`, `combo` or `check` with `options`, or `string`), and `owned`: names set by other fields, refused in `set_conf`; empty `params` when rigctl is missing; `400` when `model` is not a number |
| `/api/wizard` | setup state: `step`, `passed`, `steps`, `profile`, `radios` (each with its keying method, baud, minimum power and what it can report), `interfaces`, `ptt_methods`, `interview`, `kits` (each with its interface, keying, baud and levels) |
| `/api/ladder` | transmit check state: `tx_allowed`, `next`, `gates`, `unverified`, `antenna_verified`, `watchdog_s`, `max_tx_s` and more; `503 no rig control` without CAT |
| `/api/alerts` | `events`, `titles`, `channels`, `save_error` |
| `/api/wifi` | `unsecured` (access point still open), `pending`, `prefix`, `ssid`, `suffix_max`, `country` (the Wi-Fi country in force: the saved one, else the kernel command line's, else `""`) |
| `/api/pin` | `set`, `unlocked` (see Admin PIN) |
| `/api/tokens` | `tokens`, the app tokens (see Admin PIN); needs a token |
| `/api/join` | `on_ap`, `connection`, `state`, `ip`, `saved` (remembered networks), `home` (the one rejoined at boot, or empty), `pending`, `result` |
| `/api/ssh` | `on` (ssh.service is running), `user` (the login account), `pending`, `result`; refused while the access point is open |

### Changes

| POST | Body | Does |
|---|---|---|
| `/api/gain` | `{"tx": 0..100, "rx": 0..100}`, either or both | sets and saves mixer levels through the audio service |
| `/api/tx_audio_cap` | `{"seconds": 0..600}`, a whole number; 0 turns the cap off | sets and saves the transmit audio cap through the audio service (`set_tx_audio_cap` in `DOCS/protocol.md`); `400 bad_seconds`, or `503 service_unreachable` |
| `/api/rigctl` | `{"access": "off" \| "ap" \| "wifi" \| "all"}` | saves it through the audio service (`set_rigctl_access` in `DOCS/protocol.md`), then moves the 4532 listeners at once: `off` loopback only, `ap` loopback and the access point's 10.42.0.1, `wifi` every interface but admitting only a client that came in on loopback or a Wi-Fi interface's address (the AP or a joined network, read at each connection, so it follows a switch; never the tailnet), `all` every interface (every client but loopback logs in with an app token first, whatever the setting: "The same token on port 4532" above). A connected app on an address no longer listened on is closed and unkeyed. Replies as the GET, plus `failed` (`"address: reason"`) for an address that could not be listened on; `400 bad_access`, `400 config_save_failed` (nothing changed), or `503 service_unreachable` (the listeners did not move; the service may still have saved it for the next start, so read the GET and try again) |
| `/api/reset` | `{"scope": "radio" \| "all", "confirm": "RESET"}` | factory reset; never touches Wi-Fi. `radio`: the wizard state, every kit, the radios picked from Hamlib's list (`hamlib-*.toml`; presets installed by `vb-maintenance` stay) and the transmit bypass record; rigctld stops and transmit re-locks. `all`: that, plus gains, transmit audio limit, sound card (Automatic) and rig control access (`wifi`) back to their defaults through the audio service, alert choices back to all on, and saved fault bundles removed. Hand-edited config fields with no page control (ports, jitter, device name) are kept. Replies `ok`, `scope`, `problems` (what the card or service refused) and `wizard` (the fresh report); `400` without `confirm: "RESET"` or with an unknown scope, `409` while the radio is keyed or a client holds the transmit lock |
| `/api/wizard` | `{"action": ...}`: `choose` (`profile`, `interface`, `ptt_method`), `choose_model` (`rig_model`, `interface`, `ptt_method`: any Hamlib model, saved as the user preset `hamlib-<model>`), `answer` (`key`, `value`), `update` (`settings`: `interface`, `cabling_confirmed`, `cat_device`, `cat_baud`, `civaddr`, `ptt_method`, `audio_usb_id` and `audio_usb_path`: the sound card, both empty for automatic; `set_conf`: expert Hamlib settings passed to rigctld as `-C`, `name=value` pairs checked against `/api/hamlib/conf` for the radio, at most 10, never `serial_speed`, `ptt_type`, `civaddr`, a `*_pathname` or a `multicast_*`; cleared when the radio has no Hamlib model (VOX or no radio control); the whole `update` is refused, nothing changed, if any setting is; `ptt_device`: a separate serial port for RTS or DTR keying, empty for the radio control port; both are saved in kits as `cat.set_conf` and `ptt.device`), `goto` (`step`), `advance`, `find_baud` (at the CAT step: tries the kit's baud rate, then 38400, 19200, 9600, 115200, 57600 and 4800, reading only the frequency; saves the first that answers and checks CAT again; up to ~30 s: 27 s measured on the bench), `save_kit` (`name`), `apply_kit` (`name`), `rename_kit` (`name`, `new_name`; the setup in use follows the new name), `delete_kit` (`name`; the setup in use stays, no longer saved under a name) | one wizard step; replies with the fresh state and, for `advance`, `find_baud` and `apply_kit`, a `result` |
| `/api/ladder` | `{"action": ...}`: `confirm_load` (`load`: `antenna` or `dummy`), `drive`, `carrier` (`power_by_hand`), `meter` (`ok`), `override` (`gates`) | runs or skips a transmit check |
| `/api/ladder` | `{"action": "key" \| "heartbeat" \| "unkey", "owner": "<client name>"}` | client PTT, below |
| `/api/ladder` | `{"action": "radio"}`; `{"action": "tune", "freq_hz": <int>, "mode": "PKTUSB" \| "PKTLSB" \| "PKTFM", "listen_only": true?}` | reads, or sets then reads back, the radio's frequency and mode: `radio` (`freq_hz`, `mode`, `band`, `any_band`), and for `tune` also `result` (`gate`, `ok`, `detail`). A tune outside every ham band is refused with `"code": "out_of_band"` unless the operator allowed any band, or the request says `"listen_only": true`. A listen-only tune allows nothing else: it sets no `any_band`, records no bypass, and every fresh key still re-reads the frequency and is refused off band. `band` is null off band |
| `/api/alerts` | `{"events": {"<event>": {"sound": bool, "vibrate": bool, "beep": bool}}}` | saves alert routing |
| `/api/wifi` | `{"suffix": "<name>", "passphrase": "<8 to 63>", "country": "<optional, ISO 3166 code>"}` | renames the access point `VectorBridge-<name>` and sets its passphrase; applied by a root helper. The first-boot sheet sends the Wi-Fi country here too, since no other POST answers while the access point is open |
| `/api/wifi_country` | `{"country": "<ISO 3166 code, e.g. US>"}` | R-D7: the Wi-Fi country, which decides the 5 GHz channels a USB adapter may use; until one is set the access point stays on 2.4 GHz. `vb-wifi-radio` sets it in the kernel within a few seconds and moves the access point to 5 GHz when it can (`vector_bridge.wificountry`) |
| `/api/join` | `{"action": "join", "ssid", "passphrase"}`, `{"action": "up", "ssid"}`, `{"action": "ap"}` or `{"action": "home", "ssid"}` (empty `ssid` clears) | joins a new or remembered network, returns to the access point, or marks the network rejoined at boot; refused while the access point is open |
| `/api/ssh` | `{"action": "on", "password": "<8 to 64>"}` or `{"action": "off"}` | R-D4: on sets a new login password and starts SSH, off stops it and keeps the password; kept across reboots (`vector_bridge.sshswitch`). The password travels to the bridge like every other setting (plain HTTP, on the bridge's own Wi-Fi or the joined network); the web process hashes it and only the hash is written to the card, applied by a root helper. Needs a PIN session: an app token gets `403 pin_session_required`. Refused while the access point is open |

## Client PTT

A client app that keys the radio over CAT, RTS or DTR uses `/api/ladder`:

1. `{"action":"key","owner":"<name>"}` keys. `200` with `"ok":true` when keyed, `409` when refused: the transmit checks have not passed or been skipped, another owner holds the transmit lock, the access point is still open, a capped rig is resting after a transmission reached the cap, receive audio is red, or the bridge battery is at 6% or below on battery power. The reply does not say which.
2. `{"action":"heartbeat","owner":"<name>"}` at least every `watchdog_s` seconds (3 s) while keyed. Without it the watchdog unkeys the radio. Unless the carrier check read SWR over CAT (`antenna_verified`), a transmission is capped at `max_tx_s` (15 s): the first heartbeat at or after 15 s unkeys it, and the next key is refused for 15 s. That covers a rig that cannot report SWR, an SWR confirmed on the radio's own meter, and a skipped carrier check. With SWR read over CAT, `max_tx_s` is `null` and only the watchdog applies.
3. `{"action":"unkey","owner":"<name>"}` releases.

The owner name `ladder` is reserved. A VOX setup needs none of this: the audio keys the radio.

Replies from `POST /api/ladder`:

| Status | When | Body |
|---|---|---|
| `200` | the action ran. For `key`, `heartbeat` and `unkey`, it took effect. For `drive` and `carrier`, the check ran to the end, passed or not: read `result.ok` | the ladder report, with `"ok": true` for the PTT actions, and `result` (`gate`, `ok`, `detail`) for `drive`, `carrier` and `meter` |
| `409` | `key` refused (step 1); `heartbeat` from an owner that is not keyed, for example after the watchdog or the cap dropped it; `unkey` from an owner that does not hold the lock | the ladder report with `"ok": false`, no reason |
| `409` | the owner is `ladder`; a check that cannot start (an earlier gate not done, no radio picked, transmit in use); a bad `load`; a bad gate list in `override`, or `override` before the load is confirmed; `meter` with no check waiting for it | `{"ok": false, "error": "<reason>"}`, plus `"code": "out_of_band"` for a `tune` outside the ham bands |
| `502` | rigctld could not be reached or answered with an error, outside a running check (a CAT error during `drive` or `carrier` fails that check with a `200` instead) | `{"ok": false, "error": "<rigctld error>"}` |
| `400` | an unknown `action`, a PTT action without a non-empty `owner` string, or `meter` without a boolean `ok` | `{"ok": false, "error": "unknown_action"}` |
| `503` | no rig control (no CAT) | `{"ok": false, "error": "no rig control"}` |

`GET /api/ladder` can close the connection with no reply (issue #26). This happens when the radio, interface or preset changed while a client was keyed, and the unkey hits a CAT error. A client should treat it like a `502`.
