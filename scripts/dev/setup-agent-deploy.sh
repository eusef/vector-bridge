#!/usr/bin/env bash
# One-time, BENCH CARDS ONLY: let the vectorbridge login enter maintenance, install a wheel and
# exit maintenance with no password, so tools/deploy_pi.sh needs nobody at the keyboard.
# Not part of install.sh or the release image.
#
# Trade-off: whoever holds that login's SSH key can then run code as root on this card
# (vb-maintenance exit runs the installed package as root before locking down).
#
#   sudo vb-maintenance enter                 # must persist, so not in field mode
#   sudo bash /home/vectorbridge/vb-deploy/setup-agent-deploy.sh
#   sudo vb-maintenance exit
#
# Undo (in maintenance mode):
#   sudo rm /etc/sudoers.d/vector-bridge-agent-deploy /usr/local/sbin/vb-agent-install /usr/local/sbin/vb-agent-enter
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
if findmnt -no FSTYPE / | grep -q overlay; then
  echo "refused: field mode, this would vanish at the next reboot. Run 'sudo vb-maintenance enter' first." >&2; exit 3
fi
id vectorbridge >/dev/null
HERE=$(dirname "$0")
install -o root -g root -m 755 "$HERE/vb-agent-install" /usr/local/sbin/vb-agent-install
install -o root -g root -m 755 "$HERE/vb-agent-enter" /usr/local/sbin/vb-agent-enter
TMP=$(mktemp)
cat > "$TMP" <<'RULE'
vectorbridge ALL=(root) NOPASSWD: /usr/local/sbin/vb-agent-enter, /usr/local/sbin/vb-agent-install, /usr/local/bin/vb-maintenance exit
RULE
visudo -cf "$TMP" >/dev/null   # validate before it can break sudo
install -o root -g root -m 440 "$TMP" /etc/sudoers.d/vector-bridge-agent-deploy
rm -f "$TMP"
install -d -o vectorbridge -g vectorbridge -m 755 /home/vectorbridge/vb-deploy
echo "done: password-free deploy rule installed. Now: sudo vb-maintenance exit"
