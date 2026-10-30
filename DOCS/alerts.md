# Alerts and the status light

Everything here is local. The bridge never sends a notification over the internet, so alerts work off-grid and only reach a phone that has the bridge's home screen open.

## What triggers an alert

An alert fires when something that was working stops. Something that has never worked is shown on the home screen but does not interrupt you.

| Event | Meaning |
|---|---|
| Radio control lost | The radio stopped answering on CAT |
| Receive audio lost | Audio from the radio stopped arriving |
| Unexpected transmit | The radio is keyed and the bridge did not ask for it. For 2 s after the bridge unkeys, a PTT read-back still saying transmit is ignored: the read can race the unkey, and rigctld lags it |
| Transmit re-locked | The transmit checks no longer hold |
| Client disconnected | The computer using the bridge went away |

## Choosing how each alert reaches you

Open Alerts from the home screen. Each event has three switches: phone sound, phone vibrate and bridge beep. Settings are kept on the card and survive a power cut.

## On the phone

- Keep the home screen open. A closed tab cannot alert.
- Tap "Turn on alert sound" once each time you open the page. Phones refuse to play sound until you tap.
- iPhones do not vibrate for web pages. Use sound there.
- To install on an iPhone: open the home screen in Safari, tap Share, then Add to Home Screen. It then opens full screen like an app.
- A locked phone suspends the page. Keep the screen on if you depend on the alert.

## The status light

| Pattern | Meaning |
|---|---|
| Even blink, half a second on and off | The bridge has no readiness result yet, or the Wi-Fi is down |
| One short wink every two seconds | Access point up, waiting for a client |
| Steady on | A client is connected |
| Fast flash | Fault: something that was working has stopped |

A fault clears itself when the failed check recovers. A fault outranks everything else, so
the fast flash continues even if the Wi-Fi goes down.

The light is dark from power-on until the web server starts, and the first readiness result
usually arrives before the even blink is visible, so a normal boot goes from dark straight to
the wink. The ACT LED no longer shows SD card activity.

"Access point up" means the Pi's Wi-Fi interface is up. If you joined the Pi to another Wi-Fi network for maintenance, the light shows the same wink.

## The beeper

Two short beeps and one long beep mark a new alert. Events that go wrong in the same moment share one set of beeps, and it sounds if any of them has bridge beep switched on.

## Hardware

The status light is the Pi's own green ACT LED, set up by the installer. No beeper is fitted yet, and without one the bridge runs normally with no beeps. Both are driven the same way: the bridge writes 1 or 0 to a Linux LED-class file.

| Part | What it needs | Setting |
|---|---|---|
| Status light, no extra parts | The Pi's own green ACT LED | `VECTOR_BRIDGE_LED=/sys/class/leds/ACT/brightness` |
| Status light, external | An LED and resistor on a GPIO pin, declared with `dtoverlay=gpio-led` | `VECTOR_BRIDGE_LED=/sys/class/leds/<label>/brightness` |
| Beeper | An active buzzer (one that sounds on plain DC) on a GPIO pin, through a transistor if it draws more than a few milliamps, declared with `dtoverlay=gpio-led` | `VECTOR_BRIDGE_BEEPER=/sys/class/leds/<label>/brightness` |

A passive piezo needs a tone signal and is not supported by this interface.

Set these as `Environment=` lines in the tuner service. The service runs as the `vector-bridge` user, so that user also needs write access to the chosen file, for example through a udev rule. For the ACT LED, set its trigger to `none` so the kernel stops blinking it for card activity. The installer does all of this for the ACT LED: the `Environment=` line is in `scripts/vector-bridge-tuner.service` and the trigger and ownership come from `scripts/udev/99-vector-bridge-led.rules`.
