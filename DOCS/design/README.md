# VectorBridge web UI design

Exported 2026-10-06 from the design canvas "VectorBridge Home". One board per screen or state, no versions. This folder is the visual reference for the pages; requirements are in `SPEC.md` at the repo root, which wins where the two disagree. Every string, state, colour and layout is in the markup inside `<x-dc>`. Ignore `support.js` and the `<script type="text/x-dc">` block; they belong to the design tool.

The use-case IDs on the boards come from the design brief that preceded these boards (deleted 2026-10-24, in git history); the `*-States`, `*-Outcomes` and `Alert-Modals` boards are matrices of cards, each labelled with the use-case ID it draws.

| Group | Boards |
|---|---|
| A11y | `A11y-Announcements.dc.html` |
| Alert | `Alert-Modals.dc.html` |
| Alerts | `Alerts-Page.dc.html` |
| Apps | `Apps-Ports.dc.html`, `Apps-States.dc.html` |
| Console | `Console-Utility.dc.html` |
| Display | `Display-Sheet.dc.html` |
| Docs | `Docs-Article.dc.html`, `Docs-Index.dc.html` |
| Home | `Home-Daylight.dc.html`, `Home-DesktopKeying.dc.html`, `Home-DifferentRadio.dc.html`, `Home-Faults.dc.html`, `Home-FirstBoot.dc.html`, `Home-Laptop.dc.html`, `Home-LargeText.dc.html`, `Home-NightOps.dc.html`, `Home-NoRadio.dc.html`, `Home-Ready.dc.html`, `Home-Resting.dc.html`, `Home-SetupInterrupted.dc.html`, `Home-Tablet.dc.html`, `Home-Unlocked.dc.html` |
| KitsReset | `KitsReset-Sheet.dc.html` |
| PIN | `PIN-Enter.dc.html`, `PIN-Setup.dc.html` |
| Power | `Power-RestartConfirm.dc.html`, `Power-Sheet.dc.html`, `Power-States.dc.html` |
| Radio | `Radio-Sheet.dc.html` |
| Receive | `Receive-Sheet.dc.html`, `Receive-States.dc.html` |
| Setup | `Setup-ApplyKit.dc.html`, `Setup-CATFailed.dc.html`, `Setup-ChangeWarning.dc.html`, `Setup-Checks.dc.html`, `Setup-ChooseRadio.dc.html`, `Setup-NoControl.dc.html`, `Setup-NotSure.dc.html` |
| Support | `Support-Sheet.dc.html` |
| Tuner | `Tuner-Page.dc.html` |
| TxTimeLimit | `TxTimeLimit-Sheet.dc.html` |
| Unlock | `Unlock-Antenna.dc.html`, `Unlock-AntennaOutcomes.dc.html`, `Unlock-Drive.dc.html`, `Unlock-DriveOutcomes.dc.html`, `Unlock-Frequency.dc.html`, `Unlock-OutsideBand.dc.html`, `Unlock-Skip.dc.html` |
| WiFi | `WiFi-JoinConfirm.dc.html`, `WiFi-Joined.dc.html`, `WiFi-Secure.dc.html`, `WiFi-Sheet.dc.html` |

Decisions carried in the design: battery warn 20% and 10%, lock transmit 6%, shutdown 3%; `signalink-generic` profile (vox, no CAT, no SWR, no ALC); receive gates transmit; fault priority unexpected TX, CAT lost, TX re-locked, RX lost, audio adapter missing, battery, app gone, Wi-Fi open; kit export and import; admin PIN with physical console recovery; maintenance mode without a shell.

ASSUMED, confirm before building: Restart Services (Power-Sheet, Power-RestartConfirm), the Support sheet with saved fault reports, the PIN scope list and 3-try lockout, console menu items beyond PIN and Wi-Fi, Hamlib model numbers shown on Setup-ChooseRadio (verify against `rigctl -l`), and Frequency as its own step in the four-step unlock ladder.
