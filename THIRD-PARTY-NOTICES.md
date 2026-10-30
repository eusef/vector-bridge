# Third-party notices

VectorBridge is free software under the GNU General Public License, version 3 or (at your option) any later version. The full text is in `LICENSE`.

This file lists the third-party work VectorBridge includes or installs, and where each license is found.

## Included in this repository

| Component | Where | License |
|---|---|---|
| Lucide icons | The inline SVG icons in `src/vector_bridge/home_page.py` | ISC; the icons that came from Feather are MIT. Both texts below, as Lucide publishes them. |
| PiShrink | `scripts/image/pishrink.sh`, used only to build the image; it is not installed on the card | MIT. Text below. |

## Installed on the card by `scripts/install.sh`

| Component | How | License |
|---|---|---|
| NumPy | pip, into `/opt/vector-bridge/venv` | BSD-3-Clause, with bundled parts under 0BSD, MIT, Zlib and CC0-1.0. The texts are in the package's `.dist-info` folder in the venv. |
| pyalsaaudio | pip, built on the card | Python Software Foundation License. The text is in the package's `.dist-info` folder in the venv. |
| Hamlib (`libhamlib-utils`, for `rigctl` and `rigctld`) | apt, from Debian trixie-backports | `/usr/share/doc/libhamlib-utils/copyright` on the card |
| Raspberry Pi OS and the Debian packages it and `install.sh` install (NetworkManager, dnsmasq, ALSA, OpenSSH and others) | apt | `/usr/share/doc/<package>/copyright` on the card |

## Source code for the packages on the card

Many of the Debian and Raspberry Pi OS packages on the card, the Linux kernel among them, are under the GNU GPL or LGPL. Each release image comes with a package list: `vector-bridge-packages.txt` on the card's boot partition, published next to the image as `vector-bridge-VERSION-packages.txt`. It lists every installed package with its source package and version.

The source of those versions is available from Debian (snapshot.debian.org) and Raspberry Pi (archive.raspberrypi.com, and github.com/raspberrypi/linux for the kernel). For at least three years after each release, and for as long as that release is offered for download, the maintainer will also give anyone who asks a copy of the complete corresponding source for the packages in its list, for no more than the cost of providing it. Ask through an issue on the project's GitHub repository, naming the release.

## Lucide

ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

---

The following Lucide icons are derived from the Feather project:

airplay, alert-circle, alert-octagon, alert-triangle, aperture, arrow-down-circle, arrow-down-left, arrow-down-right, arrow-down, arrow-left-circle, arrow-left, arrow-right-circle, arrow-right, arrow-up-circle, arrow-up-left, arrow-up-right, arrow-up, at-sign, calendar, cast, check, chevron-down, chevron-left, chevron-right, chevron-up, chevrons-down, chevrons-left, chevrons-right, chevrons-up, circle, clipboard, clock, code, columns, command, compass, corner-down-left, corner-down-right, corner-left-down, corner-left-up, corner-right-down, corner-right-up, corner-up-left, corner-up-right, crosshair, database, divide-circle, divide-square, dollar-sign, download, external-link, feather, frown, hash, headphones, help-circle, info, italic, key, layout, life-buoy, link-2, link, loader, lock, log-in, log-out, maximize, meh, minimize, minimize-2, minus-circle, minus-square, minus, monitor, moon, more-horizontal, more-vertical, move, music, navigation-2, navigation, octagon, pause-circle, percent, plus-circle, plus-square, plus, power, radio, rss, search, server, share, shopping-bag, sidebar, smartphone, smile, square, table-2, tablet, target, terminal, trash-2, trash, triangle, tv, type, upload, x-circle, x-octagon, x-square, x, zoom-in, zoom-out

The MIT License (MIT) (for the icons listed above)

Copyright (c) 2013-present Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## PiShrink

The MIT License (MIT)

Copyright (c) 2026 Drew Bonasera

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
