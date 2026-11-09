#!/usr/bin/env bash
# Install ShotClip for the current user (idempotent).
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="io.github.shotclip"
BIN="$HOME/.local/bin/shotclip"
DESKTOP="$HOME/.local/share/applications/$APP_ID.desktop"
AUTOSTART="$HOME/.config/autostart/$APP_ID.desktop"
ICON="$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg"
STATE_DIR="$HOME/.local/state/shotclip"
LOGS="journalctl --user -t shotclip"
SAVED_TRAY="$STATE_DIR/toggle-message-tray.orig"

KB_SCHEMA="org.gnome.settings-daemon.plugins.media-keys"
KB_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/shotclip/"
KB_REL="$KB_SCHEMA.custom-keybinding:$KB_PATH"
EXT_UUID="shotclip@shotclip.github.io"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

# Print a GVariant string list ($1) with $2 appended (if absent) or removed.
list_edit() {
    python3 - "$@" <<'PY'
import sys
from gi.repository import GLib
items = list(GLib.Variant.parse(GLib.VariantType.new("as"), sys.argv[1], None, None).unpack())
if sys.argv[3] == "add" and sys.argv[2] not in items:
    items.append(sys.argv[2])
elif sys.argv[3] == "remove":
    items = [i for i in items if i != sys.argv[2]]
print(GLib.Variant("as", items).print_(False))
PY
}

# 0. Environment: GNOME on Wayland, with a Shell version the extension supports --
supported="$(python3 -c "import json,sys; print(' '.join(json.load(open(sys.argv[1]))['shell-version']))" \
    "$SRC_DIR/extension/metadata.json")"
shell_major="$(gnome-shell --version 2>/dev/null | grep -oE '[0-9]+' | head -1 || true)"
problems=()
[[ "${XDG_SESSION_TYPE:-}" == "wayland" ]] || problems+=("session is '${XDG_SESSION_TYPE:-unknown}', not Wayland")
[[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* ]] || problems+=("desktop is '${XDG_CURRENT_DESKTOP:-unknown}', not GNOME")
[[ " $supported " == *" $shell_major "* ]] || problems+=("GNOME Shell ${shell_major:-not found} (supported: $supported)")
if ((${#problems[@]})) && [[ "${1:-}" != "--force" ]]; then
    printf 'ShotClip needs GNOME on Wayland:\n' >&2
    printf '  - %s\n' "${problems[@]}" >&2
    printf 'Run with --force to install anyway.\n' >&2
    exit 1
fi

# 1. Dependencies (apt only, no pip) -------------------------------------------
missing=()
for pkg in python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gdkpixbuf-2.0; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing+=("$pkg")
done
if ((${#missing[@]})); then
    say "Installing missing packages: ${missing[*]}"
    sudo apt-get install -y "${missing[@]}"
fi

# 2. Stop an older running instance so the new code is picked up ---------------
if [[ -x "$BIN" ]]; then
    "$BIN" --quit >/dev/null 2>&1 || true
    for _ in $(seq 20); do pgrep -f "python3 $BIN" >/dev/null || break; sleep 0.1; done
fi

# 3. Binary --------------------------------------------------------------------
say "Installing $BIN"
install -Dm755 "$SRC_DIR/shotclip.py" "$BIN"
install -Dm644 "$SRC_DIR/data/$APP_ID.svg" "$ICON"
gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

# 4. Desktop entry + autostart -------------------------------------------------
say "Writing desktop entries"
mkdir -p "$(dirname "$DESKTOP")" "$(dirname "$AUTOSTART")" "$STATE_DIR"
cat >"$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=ShotClip
Comment=Screenshot clipboard history
Exec=$BIN
Icon=$APP_ID
Terminal=false
Categories=Utility;
StartupNotify=true
EOF
cat >"$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=ShotClip (daemon)
Comment=Screenshot clipboard history daemon
Exec=systemd-cat -t shotclip $BIN --daemon
Icon=$APP_ID
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOF
update-desktop-database "$(dirname "$DESKTOP")" >/dev/null 2>&1 || true

# 5. Free Super+V (GNOME uses it for the notification list) --------------------
current_tray="$(gsettings get org.gnome.shell.keybindings toggle-message-tray)"
if [[ ! -f "$SAVED_TRAY" && "$current_tray" != "['<Super>m']" ]]; then
    printf '%s\n' "$current_tray" >"$SAVED_TRAY"
fi
say "Setting toggle-message-tray to ['<Super>m'] (was $current_tray)"
gsettings set org.gnome.shell.keybindings toggle-message-tray "['<Super>m']"

# 6. Custom keybinding Super+V, appended without clobbering existing ones ------
current_list="$(gsettings get "$KB_SCHEMA" custom-keybindings)"
new_list="$(list_edit "$current_list" "$KB_PATH" add)"
say "Custom keybindings: $new_list"
gsettings set "$KB_SCHEMA" custom-keybindings "$new_list"
gsettings set "$KB_REL" name "ShotClip"
gsettings set "$KB_REL" command "$BIN"
gsettings set "$KB_REL" binding "<Super>v"

# 7. GNOME Shell extension that forwards copied text to the daemon -----------
say "Installing GNOME Shell extension $EXT_UUID"
install -Dm644 "$SRC_DIR/extension/metadata.json" "$EXT_DIR/metadata.json"
install -Dm644 "$SRC_DIR/extension/extension.js" "$EXT_DIR/extension.js"
enabled="$(gsettings get org.gnome.shell enabled-extensions)"
gsettings set org.gnome.shell enabled-extensions "$(list_edit "$enabled" "$EXT_UUID" add)"
# Works if the shell already knows the extension (reinstall); a brand-new
# extension is only discovered at login on Wayland.
gnome-extensions enable "$EXT_UUID" >/dev/null 2>&1 || true

# 8. Start the daemon ----------------------------------------------------------
say "Starting daemon (logs: $LOGS)"
rm -f "$STATE_DIR/shotclip.log"  # older versions logged to a file
setsid -f systemd-cat -t shotclip "$BIN" --daemon </dev/null
for _ in $(seq 50); do pgrep -f "python3 $BIN --daemon" >/dev/null && break; sleep 0.1; done
sleep 1
if pgrep -f "python3 $BIN" >/dev/null; then
    say "ShotClip is running. Press Super+V to open the picker."
    # The running shell must have *this* version loaded (copies and pastes go
    # through it); `gnome-extensions info` reports the version actually loaded.
    want="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['version'])" \
        "$SRC_DIR/extension/metadata.json")"
    info="$(gnome-extensions info "$EXT_UUID" 2>/dev/null || true)"
    if grep -q "State: ACTIVE" <<<"$info" && grep -qE "Version: $want\$" <<<"$info"; then
        say "Shell extension is active and up to date."
    else
        say "Log out and back in to load the new shell extension (Wayland only loads extension code at login)."
    fi
else
    echo "ShotClip failed to start; see: $LOGS" >&2
    exit 1
fi
