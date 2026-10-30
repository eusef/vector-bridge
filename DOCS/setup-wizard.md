# Radio setup

Tap **Set Up a Radio** on the home page, or the Radio tile. Setup opens as a sheet over the
home page. `http://vectorbridge/setup` still works: it opens the same sheet. Setup takes one
radio from unknown to ready and saves the result as a named kit.

## Choose a radio

The sheet lists your kits first, then the radios the bridge knows (Elecraft KX2, QRP Labs QMX,
uSDX, Yaesu FT-891, Icom IC-705, any radio with a Digirig, any radio with a SignaLink, and your
own presets). Pick what carries the audio separately: the radio's own USB, a Digirig, or a
SignaLink. Each choice sets the keying method and baud rate; change them later under Advanced.

## The checks

After you choose, the bridge runs the checks in order and stops at the first that does not
pass. Nothing here transmits.

- **Cables in**: the Pi sees the USB sound device
- **Receive level**: receive audio is present and the level is moving. On the common
  `12d1:0010` USB adapters, no audio can mean the adapter came up without its microphone
  input: see the jack-detect note in `DOCS/kx2-settings.md`
- **Radio control**: the radio answers on CAT; "not used" for setups with no CAT. An empty
  port ("automatic") uses the one USB serial device in `/dev/serial/by-id`; a radio that shows
  several interfaces, like the IC-705, counts as one and its lowest interface is used. With
  two or more devices plugged in, the check names them: type the radio's path under Advanced
  (`/dev/serial/by-id/...`). A typed path is always used as is. A radio whose profile has
  CAT, such as the KX2, cannot pass this check without its CAT cable, even if it will key on
  VOX. To run without radio control, choose "Any radio with a Digirig" or "Any radio with a
  SignaLink"
- **Keying**: the keying path is configured and the radio reports receive

Keying itself is proven only in Unlock Transmit, after you confirm the antenna or dummy load
(`DOCS/transmit-checks.md`). Setup checks settings and never keys.

## It remembers where you were

The position is saved on the card after every change. Close the browser, lose Wi-Fi or lose
power, and the home page shows "Radio setup did not finish" with the steps that passed;
**Continue Setup** picks up at the step that stopped. Changing a setting makes its check, and
every check after it, run again. Choosing a different radio starts the checks over, and the
page warns first when a radio is already set up: changing it re-locks transmit.

## I'm not sure what I have

Three questions stand in for the radio list: how many USB cables run to the radio side,
whether there is a small box between the bridge and the radio, and what the box says on it.
The answers suggest an interface and keying method. A SignaLink sets keying to VOX.

## This isn't working

The checks show the likeliest causes in plain language, and "This isn't working" lists them
for the check that stopped. Where cables are involved the first is always the same: a cable
in the wrong jack, usually the microphone jack.

## Kits

Once the keying check has passed you can name and save a kit. A kit holds the radio, the
interface, the cabling answers, the receive and transmit levels, and the CAT and PTT
settings. Kits live in `vector-bridge/kits/` on the persist partition.

Next time, tap the kit at the top of the sheet. The bridge loads its settings, restores its
levels, re-runs every check in order, and stops on the first that does not pass. With the
same hardware plugged in, that is one tap to Unlock Transmit.

## The Radio sheet

Once a radio is set up, the Radio tile opens its sheet: how it is connected, the keying
method, the test power (the radio's own minimum), and the kit. **Advanced** holds the Hamlib
model, serial port, baud rate and keying method; changing any of them re-runs the radio
control and keying checks and re-locks transmit.
