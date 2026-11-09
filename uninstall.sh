#!/usr/bin/env bash
# Remove ShotClip for the current user. History data in ~/.local/share/shotclip
# and the config in ~/.config/shotclip are kept.
set -euo pipefail

APP_ID="io.github.shotclip"
BIN="$HOME/.local/bin/shotclip"
DESKTOP="$HOME/.local/share/applications/$APP_ID.desktop"
AUTOSTART="$HOME/.config/autostart/$APP_ID.desktop"
ICON="$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg"
STATE_DIR="$HOME/.local/state/shotclip"
SAVED_TRAY="$STATE_DIR/toggle-message-tray.orig"

KB_SCHEMA="org.gnome.settings-daemon.plugins.media-keys"
KB_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/shotclip/"
KB_REL="$KB_SCHEMA.custom-keybinding:$KB_PATH"
EXT_UUID="shotclip@shotclip.github.io"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

# Print the GVariant string list $1 without the entry $2.
list_remove() {
    python3 - "$@" <<'PY'
import sys
from gi.repository import GLib
items = [i for i in GLib.Variant.parse(GLib.VariantType.new("as"), sys.argv[1], None, None).unpack()
         if i != sys.argv[2]]
print(GLib.Variant("as", items).print_(False))
PY
}

# 1. Stop the daemon -----------------------------------------------------------
if [[ -x "$BIN" ]]; then
    say "Stopping ShotClip"
    "$BIN" --quit >/dev/null 2>&1 || true
    for _ in $(seq 20); do pgrep -f "python3 $BIN" >/dev/null || break; sleep 0.1; done
fi
pkill -f "python3 $BIN" 2>/dev/null || true

# 2. Remove the custom keybinding (leave other entries untouched) -------------
current_list="$(gsettings get "$KB_SCHEMA" custom-keybindings)"
new_list="$(list_remove "$current_list" "$KB_PATH")"
say "Custom keybindings: $new_list"
gsettings set "$KB_SCHEMA" custom-keybindings "$new_list"
for key in name command binding; do gsettings reset "$KB_REL" "$key"; done

# 3. Give Super+V back to the notification list --------------------------------
current_tray="$(gsettings get org.gnome.shell.keybindings toggle-message-tray)"
if [[ "$current_tray" == "['<Super>m']" ]]; then
    if [[ -f "$SAVED_TRAY" ]]; then
        say "Restoring toggle-message-tray to $(cat "$SAVED_TRAY")"
        gsettings set org.gnome.shell.keybindings toggle-message-tray "$(cat "$SAVED_TRAY")"
    else
        say "Resetting toggle-message-tray to its default"
        gsettings reset org.gnome.shell.keybindings toggle-message-tray
    fi
else
    say "toggle-message-tray was changed by you since install ($current_tray); leaving it"
fi
rm -f "$SAVED_TRAY" "$STATE_DIR/setup-done"

# 4. Text capture extension ---------------------------------------------------
say "Removing GNOME Shell extension $EXT_UUID"
gnome-extensions disable "$EXT_UUID" >/dev/null 2>&1 || true
enabled="$(gsettings get org.gnome.shell enabled-extensions)"
gsettings set org.gnome.shell enabled-extensions "$(list_remove "$enabled" "$EXT_UUID")"
rm -rf "$EXT_DIR"

# 5. Files ---------------------------------------------------------------------
say "Removing binary and desktop entries"
rm -f "$BIN" "$DESKTOP" "$AUTOSTART" "$ICON"
update-desktop-database "$(dirname "$DESKTOP")" >/dev/null 2>&1 || true

say "Done. History kept in ~/.local/share/shotclip (delete it manually if you want)."
