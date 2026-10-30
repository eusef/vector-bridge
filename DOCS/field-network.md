# Field network: access point and captive portal

The bridge boots as its own Wi-Fi network. Nothing else has to exist: no router, no
internet, no phone hotspot.

## Joining

| Item | Value |
|---|---|
| Network name (SSID) | `VectorBridge-` plus a name the owner chose on the Wi-Fi page. A freshly burned card starts as an open network `VectorBridge-XXXX` (last four hex digits of the Pi serial) |
| Passphrase | chosen by the owner on the Wi-Fi page. It is also in `vector-bridge-wifi.txt` on the card's boot partition (readable in any computer), in a QR code on the Wi-Fi page, and from `sudo vector-bridge ap show` |
| Pi address | `10.42.0.1` |
| Home page | `http://vectorbridge/` (any name works on the AP; `http://vectorbridge.local/` via mDNS) |

The name only changes when the owner changes it, so a phone that saved it reconnects by
itself. Give two bridges at one site different names.

## Admin PIN and the console

Anyone who joins the bridge Wi-Fi can open its pages, so every change needs the owner's admin PIN
(4 to 8 digits; `DOCS/web-api.md`). The first save on the web page asks you to create one; the
Wi-Fi sheet can change it. Apps that key, tune or list themselves need it too.

Forgot the PIN or the Wi-Fi passphrase? Connect an HDMI monitor (mini-HDMI adapter) and a USB
keyboard (the Pi Zero 2 W has one USB data port: unplug the radio and use an OTG adapter). The
console opens a menu: "Reset the admin PIN", "Show the bridge Wi-Fi name and passphrase", or
"Command line". After a reset, the next save on the web page asks for a new PIN.

## USB Wi-Fi adapter

The Pi's built-in radio is 2.4 GHz only and drops audio packets under load. On the bench on
2026-10-06 and 07, 0.25 to 1.56 % of the bridge's audio sends failed through it. Through a USB
5 GHz adapter, 0 to 0.03 % failed. Plug an adapter into any USB port and the bridge moves to it
within a few seconds:

- The access point moves to the adapter, with the same name, passphrase and `10.42.0.1`. It runs
  on 5 GHz when the adapter and the country allow it: the first free channel of 149, 153, 157,
  161, 165, 36, 40, 44 and 48 that `iw` lists without radar detection or a "no IR" flag. Otherwise
  it runs on 2.4 GHz. Phones that saved the name rejoin by themselves. It stays WPA2 only on
  every radio (PMF disabled), because the built-in radio cannot offer WPA3: an iPhone, iPad or Mac
  that saw WPA3 on the adapter asks for the passphrase when an unplug falls back to WPA2. A device
  that joined while the adapter offered WPA3 (before this change) has to forget the network once.
- A joined network (home Wi-Fi) moves to the adapter too. The router may hand the adapter a new
  address; `vectorbridge.local` follows it.
- The built-in radio sits idle meanwhile. Unplug the adapter and everything moves back to it.

While the built-in radio carries the network, the home page opens with a notice recommending an
adapter. "Got It" hides it for good in that browser. It never shows while an adapter is in use.
The Wi-Fi page's Radio row says which radio and band are on the air.

Any adapter with a Linux driver in the Raspberry Pi OS kernel that can run an access point
works: `/opt/vector-bridge/venv/bin/python -m vector_bridge.wifiradio show` lists what the bridge found, and `iw phy` must list `AP`
under "Supported interface modes". Tested: Panda AC600 (MediaTek MT7610U, `mt76x0u`) and TP-Link
Archer T2U Nano (Realtek RTL8821AU, `rtw88_8821au`, kernel 6.18). An adapter
that cannot be an access point is ignored. The 5 GHz channels follow the Wi-Fi country (R-D7). The
owner picks it on the first-boot sheet or the Wi-Fi page; it is saved in
`/persist/vector-bridge/wifi-country.json`, and `vb-wifi-radio` sets it with `iw reg set` at every
start and within seconds of a change. With no saved country, one on the kernel command line counts
(`cfg80211.ieee80211_regdom=US` on the bench card, written by Raspberry Pi Imager's OS settings).
With neither, the bridge treats every radio as 2.4 GHz only, even if a driver would allow a
5 GHz channel.

`vb-wifi-radio.service` runs `python -m vector_bridge.wifiradio watch` as root and checks every
2 s. A move only re-points the saved profile in NetworkManager's memory, so nothing is written to
the card. Each boot starts on the built-in radio and moves within seconds. The watcher logs each
move to the journal (`journalctl -u vb-wifi-radio`).

## First boot of a burned card

The access point is open until the owner sets a name and passphrase. Until then every page
redirects to `/wifi`, every other change is refused and transmit stays locked, so a
stranger who joins can do nothing but name it. Saving reboots the bridge once into field
mode. Details: `DOCS/image-build.md`.

## Changing the name or passphrase

The top of `http://vectorbridge/wifi` changes both. The new name is on the air within
seconds, with no reboot, and `vector-bridge-wifi.txt` is rewritten. Phones must join again
with the new values. Saving needs the admin PIN (`DOCS/web-api.md`), except on first boot,
while the access point is still open.

## Captive portal

When a phone joins, it fetches a connectivity-check URL to see whether the network has
internet. The bridge's local DNS answers every name with the Pi's address, and the web
server on port 80 answers every probe with a redirect to the home page. The phone shows
that page in its "sign in to network" sheet. Tap "Done" or "Use without internet" to
keep the network; the page is then reachable from any browser as `http://vectorbridge/`.

Clients that ignore the portal: open a browser and type `vectorbridge` or `10.42.0.1`.

## Joining another network

The Wi-Fi page (`http://vectorbridge/wifi`, below the access point settings) joins the bridge
to an existing network, for example a home network to pull an update. One radio carries the
bridge's network at a time (the USB adapter when one is plugged in), so its own access point is
off while it is there, and the page is at
`http://vectorbridge.local/` on that network instead. Anyone on that network can open the
pages and read the status, but every change needs the admin PIN (`DOCS/web-api.md`).

- The page shows a QR code of the bridge's own name and passphrase; a phone camera
  joins from it.
- A new network is remembered and joined right away, nothing restarts. Look for
  `vectorbridge.local` on the other network.
- A remembered network joins in seconds; the page lists them as buttons.
- "Back to the access point" returns without a restart. So does any restart: remembered
  networks are stored with autoconnect off, and the access point always comes up first.
- One remembered network can be marked **Rejoin at boot** (a home base station). Once the
  access point is up at boot, `vb-wifi-home.service` scans, and if that network
  is in range it switches to it. Out of range, as in the field, the access point stays up.
  Some adapters (the TP-Link above) cannot scan while they are the access point, and the bridge
  cannot tell that from a place with no Wi-Fi at all. So when a scan shows no networks, the
  access point pauses for one scan, a few seconds, and comes back if the network is not in range.
  A failed rejoin (router down, passphrase changed) brings the access point back.
- A wrong passphrase puts the access point back; the page says why.

On the bench, `vector-bridge wifi join <ssid>` does the same from a shell (this one is not
remembered past a reboot in field mode) and `vector-bridge ap enable` brings the AP back.

## Documentation on the card

Every file at the top of this DOCS directory is served at
`http://vectorbridge/docs/`. The installer copies the repo to `/opt/vector-bridge/src`, and
the web server reads `DOCS` from there (override with the `VECTOR_BRIDGE_DOCS` environment
variable). The installer leaves `DOCS/archive` (developer records) off the card.

## How it is put together

| Piece | Where |
|---|---|
| AP profile `vector-bridge-ap` | NetworkManager, shared mode, autoconnect priority 100; created by `vector-bridge ap ensure` in the installer on the bench, by `vb-firstboot` on a burned card |
| Profile storage | NetworkManager keyfiles on `/persist/vector-bridge/networks`, written inside a brief rw remount (P10) |
| Radio choice | `src/vector_bridge/wifiradio.py` via `vb-wifi-radio.service`: a USB adapter that can run an access point over the built-in radio, 5 GHz when allowed |
| Wi-Fi page | `src/vector_bridge/wifi_page.py`; the web process writes a request file and root applies it: `apsetup.py` via `vb-ap-apply.path`, `wifijoin.py` via `vb-wifi-join.path` |
| DHCP and DNS | dnsmasq run by NetworkManager for the shared connection; `scripts/ap/dnsmasq-shared.conf` makes every name resolve to `10.42.0.1` |
| Hostname | set to `vectorbridge` by the installer |
| Portal redirects and docs | `src/vector_bridge/portal.py`, wired into the tuner web server |

## Verifying on hardware

Verified on a Pi Zero 2 W on 2026-09-20 with macOS and iPadOS clients (see `DOCS/archive/bringup-log.md`, P3). wlan0
operstate reads `up` in AP mode. To check:

1. Run the installer, reboot, and confirm the AP is up:
   ```
   nmcli -t -f NAME,DEVICE connection show --active
   ```
   Expect `vector-bridge-ap:wlan0`.
2. On a phone, forget any saved bridge network, join the bridge with the
   passphrase from `sudo vector-bridge ap show`. The sign-in sheet should open showing the bridge
   home page within a few seconds.
3. Dismiss the sheet, open a browser, and load `http://vectorbridge/docs/`.
4. From the Pi, confirm DNS answers with the Pi address for any name:
   ```
   dig +short anything.example @10.42.0.1
   ```
