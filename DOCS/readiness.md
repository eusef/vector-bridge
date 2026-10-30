# Readiness: the home page

Opening `http://vectorbridge/` answers two questions: can I receive, and can I transmit?
The page never transmits, so it is safe to open while someone is listening. Every setting
opens as a sheet over the page, and the checks keep running underneath it.

## What the page shows

| Card | Green means | Other states |
|---|---|---|
| Receive | audio is arriving and the level sits inside the brackets | No radio, No audio (with the fixes to try in order), Too quiet, Too loud, Frozen, Checking, Paused while the radio transmits |
| Transmit | the transmit checks have passed or been skipped: Unlocked | Locked, with Unlock Transmit; the button is disabled with the reason when receive is red ("Fix receive first") or the bridge battery is below 6% |
| Connect | the name and address to open this page | |
| Apps | audio is reaching the client app | Check (measuring), Off (no client) |
| Station tiles | Radio, Power, Wi-Fi, Alerts | each opens its sheet |

Above the cards, one banner at most: the most urgent fault, or the radio setup state (no radio
chosen, setup did not finish, or the kit's radio has not answered since the bridge started).

## The checks underneath

`/api/readiness` still runs five checks every two seconds:

| # | Check | Green means | How it is checked |
|---|---|---|---|
| 1 | Radio control (CAT) | the radio answers | a read over rigctld on the Pi |
| 2 | Receive audio | audio is present and the level moves | the audio service's level history |
| 3 | Transmit keying (PTT) | the rig keyed and released in the transmit checks | transmit state is only read back here |
| 4 | Transmit safety checks | all passed; bypassed checks show amber "rig unverified" | state from the transmit ladder |
| 5 | Client audio stream | packets are flowing to a connected client | the audio service's packet counters |

Too quiet, Too loud and clipping come from the live level on `/api/status`, judged against the
same brackets as the level tuner over the last three seconds.

## Receive gates transmit

While the receive check is red, the bridge refuses a new client key (`POST /api/ladder`
`key` returns 409). No receive audio almost always means a cabling fault that breaks
transmit audio too. A client that is already keyed stays keyed until it unkeys.

## Faults and alerts

An alert interrupts only when a check that had been green turns red (including by way of
amber), or when the bridge battery crosses a warning level. When several faults are red at
once, the most urgent owns the alert and the top of the page and the rest are listed under
it, in this order:

1. Unexpected transmit
2. Radio control lost
3. Transmit re-locked (not raised yet: the readiness check reports a lock as amber, never red, so a
   re-lock shows on the Transmit card but does not interrupt)
4. Receive audio lost
5. Battery (warn at 20% and 10%, transmit locked at 6%)
6. App disconnected
7. Wi-Fi open

Times on the page are relative ("3 min ago"). The bridge has no clock battery.

## Appearance

The sun button in the header cycles Daylight, Dark and Night Ops with one tap. The Display
sheet also offers "Match the phone". The choice is saved on each phone, not on the bridge.

The level tuner is at `http://vectorbridge/tuner`.
