# Known issues

What does not work yet in VectorBridge 0.2.0, or works with a catch, and what to do about it. Each item says what you will see, then what to do.

## Wi-Fi

### The first-boot network is open

A freshly written card starts as an open network, `VectorBridge-XXXX`. Until you save a passphrase, anyone in range can join it and claim the bridge first, and the passphrase you choose crosses that open network unencrypted. While the network is open the bridge shows only the Wi-Fi page and refuses to transmit.

What to do: do the first setup where no one else is nearby. If someone else got there first, write the card again. You must choose a new network name, so phones that saw the open network do not try the old, open entry.

### No internet on the bridge's own Wi-Fi

The bridge's network has no internet and does not pass any through. Your phone may say the network has no internet, and other apps on it may use mobile data instead. The bridge's own pages and the apps that talk to the bridge still work.

What to do: nothing; this is expected. Joining a home network from the Wi-Fi page swaps the bridge's own network for that one, since one radio carries one network at a time.

### Home Wi-Fi rejoins only at boot

The bridge rejoins the home network you marked only when it starts up. If that network was out of range at power-up, or drops later, the bridge stays on (or goes back to) its own network. It does not keep looking.

What to do: on the Wi-Fi page, tap the saved network to join it now, or restart the bridge once you are in range.

### The Pi's built-in Wi-Fi drops audio

The Pi Zero 2 W's own radio is 2.4 GHz only. Under audio load it loses up to about 1.6% of packets, in stalls of up to 3 s, which costs decodes. Through a USB adapter on 5 GHz the loss was 0 to 0.03%. The home page shows a notice while the built-in radio is in use, until you tap Got It in that browser.

What to do: use a USB Wi-Fi adapter that can run an access point, and set your Wi-Fi country on the Wi-Fi page: until it is set, the bridge stays on 2.4 GHz. The Panda AC600 (MT7610U) is the one tested most. The TP-Link Archer T2U Nano also works but cannot scan while it hosts the bridge's network, and it has stalled during transmit with an IC-705 on the bench.

### The Wi-Fi page can take two or three tries on a phone

When you join a saved network or change the SSH setting on the Wi-Fi page, the network name box can take the keyboard back just as the admin PIN prompt is opening, and the action does not go through. Seen on an iPhone with VectorBridge 0.2.0.

What to do: tap the button again; it usually works on the second or third try. A fix is planned for the next release.

### The sign-in page may not open on its own

On iPhone, iPad and Mac a sign-in page opens when you join the bridge. Android has not been tested.

What to do: if no page opens, browse to `http://vectorbridge/` (or `http://10.42.0.1/`).

## Audio and apps

### One app at a time sends and receives audio

The bridge serves one audio client. Whichever app last connected takes over, with no warning, and the app before it stops getting audio. Up to two more apps can listen only (for example JS8Call beside the iPad app); a third listener is refused.

What to do: run one transmitting app at a time. Close the other app before you switch.

### Anyone on the bridge's Wi-Fi can take the audio

Joining the bridge's network is enough to take the audio stream, listen to the receiver, or play the test tone. No PIN is asked. Settings changes need the admin PIN.

What to do: give the Wi-Fi passphrase only to people you trust, and change it on the Wi-Fi page if it gets out.

### WSJT-X, JS8Call and fldigi need VectorCable for rig control

Port 4532 asks every app for the admin PIN (as an app token) before it answers a command, so nobody on the Wi-Fi can retune or key the radio without it. WSJT-X, JS8Call and fldigi cannot log in by themselves. Earlier cards (0.2.0 and before) let them connect straight to the bridge; this one does not.

What to do: run VectorCable on the computer, log in with the PIN once, and point the app's Hamlib NET rigctl at VectorCable's own address, 127.0.0.1:4532.

### A radio on VOX transmits whatever audio arrives

On a VOX setup (SignaLink or a sound card on VOX) there is no transmit lock: the radio keys on any audio an app sends, and anyone on the bridge's Wi-Fi can send audio. The only limit is the Transmit Time Limit (120 s by default).

What to do: keep the passphrase private, as above. Prefer radio control (CAT) keying where your radio has it, so the transmit checks and lock apply.

### Long transmissions need a higher Transmit Time Limit

Transmit audio that runs 120 s with no 1 s pause is muted until it pauses, and a desktop app keying through port 4532 is unkeyed at the same 120 s. FT8, FT4, every JS8 speed, JT65, JT9 and WSPR fit; long fldigi messages (Olivia, MFSK), JS8Call multi-frame messages and long Tune presses may not.

What to do: raise the limit on the home page's Transmit Time Limit sheet (0 to 600 s, 0 turns it off). While the antenna is unverified, transmissions are cut at 15 s regardless.

### A high Transmit level distorts the signal on a QMX

The Transmit level is a software gain with no limiter. On a QMX (and cards like it) a level pushed high can clip the transmit audio, which distorts the signal and splatters on the band. When this happens the transmit status shows the `audio_clipping` warning.

What to do: keep the QMX Transmit level at or below 90. If the warning appears, lower the level until it clears.

## Radios

### Supported, experimental and not yet supported

The IC-705 has passed the transmit checks on hardware. The QRP Labs QMX and the Yaesu FT-891 on a Digirig are experimental. The Elecraft KX2's transmit checks have not been run yet, and a KX2 on VOX with no radio control cable cannot finish setup yet. The other choices in setup (uSDX, any radio with a Digirig, any radio with a SignaLink, a sound card on VOX, and any radio picked from the Hamlib search) have not been tested with this release. The truSDX is not supported over its single USB cable yet (audio and radio control on one link); support is planned for a later release. Until then it needs a USB sound card, with a mono cable into its MIC jack.

What to do: on an experimental radio, watch the first transmissions with a power meter or dummy load.

### Digirig: a short key-up when radio control starts, and box swaps

With a Digirig, opening the serial port can raise its keying lines, so the radio may key for a moment when radio control starts. After you swap one Digirig box for another, the bridge can keep using the old box's sound card.

What to do: have the antenna or a dummy load connected when the bridge starts. Restart the bridge after a box swap.

## Admin PIN and security

### Whoever saves first sets the PIN

A new card, or one whose PIN was reset at the console, has no PIN. The first save on any settings page asks for a new PIN, with no login, so anyone joined to the bridge at that moment can choose it.

What to do: set the PIN yourself right after first boot, before anyone else joins, and again right after a console reset.

### Wrong PINs lock out everyone

After 5 wrong PINs the bridge makes everyone wait before a new login, starting at 1 minute and doubling up to 1 hour. Browsers and apps that are already logged in keep working.

What to do: wait, or restart the bridge, which clears the count. Reset a forgotten PIN from the HDMI console (`DOCS/field-network.md`, "Admin PIN and the console").

### The Wi-Fi QR code shows the passphrase without a PIN

`http://vectorbridge/wifi-qr.svg` shows the bridge's Wi-Fi passphrase as a QR code to anyone who can reach the bridge, including people on a home network it has joined.

What to do: join the bridge only to home networks whose users you trust.

### Pages use plain HTTP

The bridge has no HTTPS. On its own network the Wi-Fi passphrase protects what you send, the PIN included; on a joined home network, anyone on that network could see it, and a fake bridge could collect an app's login.

What to do: type the PIN only on networks you trust.

## Card, clock and power

### Receive audio clips on a fresh card

The example receive level (50) that ships on the card is too high for some radios: the home page says "Too loud" and decodes fail.

What to do: turn down the radio's data output or headphone level, then lower the Receive level slider in Receive Audio.

### A restart can hang

On the bench card about 1 restart in 3 hung and needed the power pulled. It is not yet known whether a fresh card does the same.

What to do: if the bridge has not come back after a few minutes, pull the power and plug it back in. The card is read-only in normal use, so this is safe, unless the home page says "Maintenance mode. Do not pull power."

### Not yet proven on hardware

These are built and covered by tests but have not yet been run on a real card: the radio unkeying when the bridge crashes or is stopped while transmitting, and the Transmit Time Limit cutting RF on a radio keyed by VOX.

What to do: do not rely on them as your only safeguard; stay near the radio while it transmits.

### The clock is wrong until the bridge reaches the internet

The Pi has no clock battery and nothing on the bridge sets its clock from your phone. With no internet, as in the field, the times in its logs and support bundles can be wrong.

What to do: keep the clock right on the device running your digital-mode app; that is the clock FT8 and JS8 timing needs.

### Changes made outside the web page are lost at restart

The card's system files are read-only in normal use. Anything changed from the command line outside the settings folder is gone after a restart. Settings saved from the web page are kept.

What to do: change settings from the web page. Lasting changes outside it need maintenance mode, which works only while the bridge is joined to another network (the installer also needs internet); it is a bench task, explained in `DOCS/card-layout.md`. There is no software update in the web page; updating the bridge is done the same way, by the maintainer in maintenance mode.

### PiSugar battery readings

On a PiSugar 2 the home page shows no plugged-in state until the power source changes once. The PiSugar 3 and 2 Pro have not been tested.

What to do: plug in or unplug the charger once to get a reading; on a PiSugar 3 or 2 Pro, check the battery on the PiSugar itself.
