#!/usr/bin/env bash
# Installe l'indicateur d'usage Claude comme service utilisateur systemd.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EXEC="$SCRIPT_DIR/claude_usage_monitor.py"
UNIT_NAME="claude-usage-monitor.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
TYPELIB_DIR=/usr/lib/x86_64-linux-gnu/girepository-1.0

info() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m/!\\\033[0m %s\n' "$1"; }
fail() { printf '\033[1;31mxxx\033[0m %s\n' "$1" >&2; exit 1; }

# --- dépendances ------------------------------------------------------------

command -v python3 >/dev/null || fail "python3 introuvable"
python3 -c 'import gi' 2>/dev/null || fail "PyGObject manquant: sudo apt install python3-gi"

if [[ ! -e "$TYPELIB_DIR/AyatanaAppIndicator3-0.1.typelib" \
   && ! -e "$TYPELIB_DIR/AppIndicator3-0.1.typelib" ]]; then
  info "typelib AppIndicator manquante, installation via apt"
  sudo apt-get install -y gir1.2-ayatanaappindicator3-0.1 \
    || fail "installe manuellement: sudo apt install gir1.2-ayatanaappindicator3-0.1"
fi

if command -v gnome-extensions >/dev/null; then
  gnome-extensions list --enabled 2>/dev/null | grep -q appindicator \
    || warn "extension GNOME appindicator désactivée — l'icône n'apparaîtra pas.
     Active-la: gnome-extensions enable ubuntu-appindicators@ubuntu.com"
fi

# --- smoke test -------------------------------------------------------------

chmod +x "$EXEC"
info "test de l'accès à l'API"
"$EXEC" --once || fail "l'appel API a échoué — connecte-toi avec \`claude\` puis relance"

# --- unité systemd ----------------------------------------------------------

mkdir -p "$UNIT_DIR"
sed "s|__EXEC__|$EXEC|" "$SCRIPT_DIR/systemd/$UNIT_NAME" > "$UNIT_DIR/$UNIT_NAME"
info "unité écrite: $UNIT_DIR/$UNIT_NAME"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

sleep 2
systemctl --user --no-pager --lines=5 status "$UNIT_NAME" || true

cat <<EOF

Installé. L'icône est dans la barre du haut.

  logs       journalctl --user -u $UNIT_NAME -f
  stop       systemctl --user stop $UNIT_NAME
  désinstall systemctl --user disable --now $UNIT_NAME && rm $UNIT_DIR/$UNIT_NAME
  config     ${XDG_CONFIG_HOME:-\$HOME/.config}/ubuntu-claude-monitor/config.toml
EOF
