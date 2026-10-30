# App listing protocol

Status: built and merged (PR #57, 2026-10-06). Lets a connected client app put its own name, icon and launch link in the Apps card on the bridge's home page, and change them at any time.

## Summary

| Step | Channel | What the app sends |
|---|---|---|
| 1. Connect | UDP 5101, as today | `hello` or `monitor` with `"client":"<app_id>"` |
| 2. Describe itself | HTTP `POST /api/apps` on port 80 | `app_id`, `name`, optional `launch_url`, optional `icon_png` |
| 3. Stay listed | both | UDP keepalives as today; repeat the POST every 60 s and on any change |

The audio packet and the existing control commands do not change. A row appears only while the app's UDP stream is live, so "listed" always means "connected now".

Why HTTP for the description: an icon does not fit in one UDP datagram without IP fragmentation, which is unreliable over Wi-Fi and the tailnet (1280-byte MTU). Port 80 is already open to every client that can reach the bridge.

## 1. Connect (UDP, unchanged)

Send `hello` (the peer) or `monitor` (listen-only) on 5101 as in DOCS/protocol.md. The `client` string must equal the `app_id` the app will POST. It is how the bridge ties the description to the live stream.

The bridge remembers each `client` string by the address (IP and port) that sent it, and labels each live stream like this:

1. A stream whose own address sent a `hello` or `monitor` takes that `client` string. This is the one-socket layout, and every monitor, since a monitor registers from its RX socket. Two apps on one computer, each on its own socket, get their own labels.
2. Otherwise, the stream takes the `client` string from the most recent `hello` or `monitor` from any port on the same IP. This is the separate-socket layout: `hello` comes from the control socket, and the stream lives on the audio socket.

Limit: two separate-socket apps on one computer can be mislabelled by rule 2. A stream registered only by TX audio, with no `hello` from its IP at all, has no `client` string and is listed as an undescribed client.

Send at least one `hello` or `monitor` per session. A repeated `monitor` keepalive with the same `client` string is harmless: it refreshes the registration, as today. Do not re-send `hello` just to refresh the name: in the separate-socket layout each `hello` moves RX and costs about 100 ms of TX (DOCS/protocol.md). The POST is the refresh path.

The row's role follows the UDP command: `hello` makes the peer row, `monitor` a listening row.

## 2. POST /api/apps

The bridge matches the POST to the live stream by source IP, so the POST must leave from the same IP as the UDP. Resolve the bridge address once and use that same literal address (for example `192.0.2.10` or `100.101.102.103`) for both UDP and HTTP. A name resolved twice can land on IPv4 for one and IPv6 or another interface for the other, and the POST then answers `listed:false` forever. A literal address passes the bridge's Host check for writes. A native app sends no `Origin` header, which is fine.

Like every POST, this one needs the admin PIN's token: `Authorization: Bearer <token>` from `POST /api/login`. Without it the bridge answers `401 pin_required` and the app is not listed. The login and retry rules are in DOCS/web-api.md, "Admin PIN".

On the tailnet, the tailnet policy must allow TCP 80 from the app's device to the bridge, as well as UDP 5100 and 5101 (and TCP 4532 for rig control). A blocked port never reaches the bridge, so there is no error body: the POST times out. An app whose UDP works but whose POST times out should suggest checking the policy for TCP 80. Run the POST off the audio path with a short timeout (about 3 s).

Request body, JSON, at most 64 KB:

```json
{
  "app_id": "vector-swl-radio",
  "name": "VectorRadio",
  "launch_url": "https://swl-radio.example-tailnet.ts.net/",
  "icon_png": "<base64 PNG>"
}
```

| Field | Required | Rule |
|---|---|---|
| `app_id` | yes | `^[a-z0-9][a-z0-9._-]{0,47}$`. Stable across versions. Equals the UDP `client` string |
| `name` | yes | 1 to 32 characters after trimming, plain text, no control characters. Shown as the row title, HTML-escaped |
| `launch_url` | no | At most 512 characters. Scheme `http`, `https`, or a custom app scheme (`[a-z][a-z0-9+.-]*:`). Refused: `javascript`, `data`, `file`, `blob`, `vbscript`, `about`. Omit or `null` for a row that is not a link |
| `icon_png` | no | Base64 PNG, square, 64 to 256 pixels a side, at most 24 KB decoded. Omit for the bridge's generic app icon |
| `note` | no | At most 60 characters, plain text, no control characters. Shown under the name, escaped. For a hint the link can't give, for example `Open it on the Mac running it` when `launch_url` is null. Omit for no note |
| `icon_sha256` | no | In place of `icon_png` on a repeat POST: the hex SHA-256 of a PNG this app already sent. Keeps the 60 s refresh small |

Every POST replaces the whole record: a field left out is cleared, not kept. When a POST carries `icon_sha256` for an icon the bridge no longer holds (after a restart), the bridge stores the rest, replies `"icon_needed":true`, and shows the generic icon until the app POSTs again with `icon_png`.

Replies:

| Status | Body | When |
|---|---|---|
| 200 | `{"ok":true,"listed":true}` | stored, and a live UDP stream from this IP carries this `app_id` |
| 200 | `{"ok":true,"listed":false}` | stored, but no such stream yet; the row appears when it starts |
| 400 | `{"ok":false,"error":"bad_json"}` | body is not a JSON object |
| 400 | `{"ok":false,"error":"bad_<field>"}` | `bad_app_id`, `bad_name`, `bad_launch_url`, `bad_note` or `bad_icon` |
| 403 | `{"ok":false,"error":"forbidden_origin"}` | foreign Host or Origin (the existing write check) |
| 403 | `{"ok":false,"error":"wifi_open",...}` | the access point has no passphrase yet; nothing is listed until it does. Retry quietly on the next 60 s cycle |
| 413 | `{"ok":false,"error":"too_large"}` | body over 64 KB |

`listed:false` and `wifi_open` are normal states, not faults: retry silently on the next cycle and show the user nothing.

### The launch link

The bridge shows the link as given and opens it in a new tab. It does not rewrite it or check that it works. The link is opened on whatever device is viewing the bridge page (often a phone), so it must be reachable from there:

- An `http://127.0.0.1:...` link works only on the same computer as the app. Send it only when nothing better exists.
- A tailnet link (`https://<name>.<tailnet>.ts.net/`) works from any of the owner's tailnet devices.
- A custom scheme (`myapp://open`) works only on a device where that app is installed.

This is why the app owns the link and may change it: send a new POST when the better address comes up (for example, after the tailnet connects) or goes away.

## 3. Staying listed and dropping off

- The bridge keeps descriptions in memory only. A bridge restart forgets them.
- The app repeats the POST every 60 s while connected, after every new UDP session (a `hello` or `monitor` reply after a gap or a bridge restart), and right away whenever its name, icon or link changes. A POST is idempotent.
- A row shows only while both hold: a POST from that IP and `app_id` within the last 180 s, and a live UDP peer or monitor from that IP whose `client` is that `app_id`.
- When the app quits, its row goes when the bridge drops its UDP stream (5 s with no keepalive). No delete call is needed.

Two copies of one app on two computers are two rows, told apart by IP address. Rows follow live streams, so an app that reaches the bridge on the LAN and the tailnet at once, with a stream on each, is two rows; only the one whose source IP matches a POST gets the description.

## What the Apps card shows

Read by the home page from `GET /api/apps`:

```json
{"ok": true, "apps": [
  {"app_id": "vector-swl-radio", "name": "VectorRadio", "role": "monitor", "addr": "100.101.102.103:53211",
   "launch_url": "https://...", "note": null, "icon_url": "/api/apps/icon/3f9a1c0e5b7d2a46.png"}
]}
```

- Each live UDP client is one row: icon, name, its role (sending and receiving, or listening only) and the app's `note` when it sent one. Only the peer row carries a health pill, as today: the readiness check that drives it covers the peer only. Monitor rows show "Listening".
- A live client with no description (for example VectorComm today) still gets a row: its `client` string as the name, shown as sent (escaped) even when it does not fit the `app_id` pattern (or "Unnamed app" when it sent none), the generic icon, no link. While the access point has no passphrase, descriptions are refused and the home page shows no Apps card; once it is secured, rows stay undescribed until each app's next POST.
- `addr` is for display only.
- `icon_url` is named by the first 16 hex digits of the PNG's SHA-256, served as `image/png` with `X-Content-Type-Options: nosniff` and a long cache lifetime. A new icon gets a new URL, so the page never shows a stale one.

## Trust

Anyone who can join the bridge's Wi-Fi or tailnet can list an app with a link, the same boundary as the rig control proxy. The checks above stop a listing from running script in the bridge page: the name is escaped, the link scheme is filtered, and the icon is a validated PNG served as an image.

## Suggested values

| App | `app_id` | `name` | `launch_url` |
|---|---|---|---|
| VectorRadio (formerly VectorSWL Radio) | `vector-swl-radio` (its `-client` default, unchanged by the rename) | `VectorRadio`, one word (maintainer, 2026-10-06) | its remote page (https or http, whichever is up) while it is up, else `null` with a note like `Open it on the Mac running it` (maintainer, 2026-10-05) |
| VectorCable (the `vectorcable` terminal app) | `vectorcable` | `VectorCable`, no icon, note `Audio cable for WSJT-X, JS8Call and FLDIGI` in every state | `null` until /downloads/ ships on the bridge, then `http://<the same literal bridge address used for UDP>/downloads/` (decided by CABLES, delegated by the maintainer, 2026-10-05) |
| VectorComm (iPad) | `vector-comm`. Its hello `client` changes from `VectorComm` to match | `VectorComm`, bundled PNG icon | `vectorcomm://`, registered in the same change. Describes itself later, after RX validation (the maintainer's decision, relayed by COMM, 2026-10-05). Until then it is an undescribed row labelled `VectorComm` |

A row with no link shows no bridge hint. Each app may send its own `note` for that (maintainer, 2026-10-05).

## Bridge work (built)

1. Audio service (service.py): remember `client` strings per sending address and label streams by the two rules in section 1 (today the string is only logged), and add `"clients": [{"addr","role","client"}]` to `status`.
2. Web server (tuner.py, apps.py): `POST /api/apps`, `GET /api/apps`, `GET /api/apps/icon/<hash>.png`, with the checks above and an in-memory store. Add `/api/apps` to the POST route list, and cap the body read at 64 KB before reading it (the POST handler has no size cap today).
3. Home page (home_page.py): build the Apps card from `GET /api/apps`.
4. DOCS/protocol.md: add the `clients` status field and a pointer here.
