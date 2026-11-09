#!/usr/bin/env bash
# Build dist/shotclip_<version>_all.deb.
#   sudo apt install ./dist/shotclip_<version>_all.deb
# The package installs ShotClip system-wide; each user's Super+V binding and
# the shell extension are switched on by `shotclip --setup`, which the package
# runs for the user installing it and the daemon runs on every user's first login.
set -euo pipefail
umask 022

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="io.github.shotclip"
EXT_UUID="shotclip@shotclip.github.io"
VERSION="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$SRC_DIR/shotclip.py")"
[[ -n "$VERSION" ]] || { echo "VERSION not found in shotclip.py" >&2; exit 1; }
MAINTAINER="$(git -C "$SRC_DIR" config user.name 2>/dev/null || echo ShotClip) <$(git -C "$SRC_DIR" config user.email 2>/dev/null || echo shotclip@localhost)>"
SHELL_VERSIONS="$(python3 -c "import json,sys; v=json.load(open(sys.argv[1]))['shell-version']; print(min(map(int, v)))" "$SRC_DIR/extension/metadata.json")"

OUT_DIR="$SRC_DIR/dist"
PKG="$(mktemp -d)"
trap 'rm -rf "$PKG"' EXIT
chmod 755 "$PKG"

# -- files -----------------------------------------------------------------------
install -Dm755 "$SRC_DIR/shotclip.py" "$PKG/usr/bin/shotclip"
install -Dm644 "$SRC_DIR/extension/extension.js" "$PKG/usr/share/gnome-shell/extensions/$EXT_UUID/extension.js"
install -Dm644 "$SRC_DIR/extension/metadata.json" "$PKG/usr/share/gnome-shell/extensions/$EXT_UUID/metadata.json"
install -Dm644 "$SRC_DIR/data/$APP_ID.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
install -Dm644 "$SRC_DIR/README.md" "$PKG/usr/share/doc/shotclip/README.md"
if [[ -f "$SRC_DIR/LICENSE" ]]; then
    install -Dm644 "$SRC_DIR/LICENSE" "$PKG/usr/share/doc/shotclip/copyright"
fi

install -d "$PKG/usr/share/applications" "$PKG/etc/xdg/autostart"
cat >"$PKG/usr/share/applications/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=ShotClip
Comment=Clipboard history for screenshots and text
Exec=/usr/bin/shotclip
Icon=$APP_ID
Terminal=false
Categories=Utility;
Keywords=clipboard;history;paste;screenshot;
StartupNotify=true
EOF
cat >"$PKG/etc/xdg/autostart/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=ShotClip (daemon)
Comment=Keeps the clipboard history
Exec=systemd-cat -t shotclip /usr/bin/shotclip --daemon
Icon=$APP_ID
Terminal=false
NoDisplay=true
OnlyShowIn=GNOME;
X-GNOME-Autostart-enabled=true
EOF

# -- package metadata --------------------------------------------------------------
install -d "$PKG/DEBIAN"
echo "/etc/xdg/autostart/$APP_ID.desktop" >"$PKG/DEBIAN/conffiles"
cat >"$PKG/DEBIAN/control" <<EOF
Package: shotclip
Version: $VERSION
Architecture: all
Section: utils
Priority: optional
Maintainer: $MAINTAINER
Installed-Size: $(du -sk --exclude=DEBIAN "$PKG" | cut -f1)
Depends: python3, python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), gir1.2-gdkpixbuf-2.0, gnome-shell (>= $SHELL_VERSIONS), systemd
Description: Clipboard history for GNOME on Wayland (Super+V)
 ShotClip keeps the screenshots, images and text you copy and shows them in a
 picker on Super+V. Pick an item to paste it into the app you were using.
 Items can be pinned, searched and shown as a list, compact rows or tiles.
EOF

# Shared by the maintainer scripts: run a command as the user who installed or
# removes the package, inside their desktop session (if they have one).
read -r -d '' AS_USER <<'EOF' || true
target_user() {
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then echo "$SUDO_USER"
    elif [ -n "${PKEXEC_UID:-}" ]; then id -nu "$PKEXEC_UID" 2>/dev/null || true
    fi
}
as_user() {
    u="$1"; shift
    uid="$(id -u "$u")"
    [ -S "/run/user/$uid/bus" ] || return 1
    runuser -u "$u" -- env XDG_RUNTIME_DIR="/run/user/$uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"
}
EOF

cat >"$PKG/DEBIAN/postinst" <<EOF
#!/bin/sh
set -e
$AS_USER
if [ "\$1" = configure ]; then
    gtk-update-icon-cache -q /usr/share/icons/hicolor >/dev/null 2>&1 || true
    update-desktop-database -q /usr/share/applications >/dev/null 2>&1 || true
    user="\$(target_user)"
    if [ -n "\$user" ]; then
        as_user "\$user" /usr/bin/shotclip --setup >/dev/null 2>&1 || true
        # Upgrade: restart a running daemon so the new version is used.
        if pgrep -u "\$user" -f "shotclip --daemon" >/dev/null 2>&1; then
            as_user "\$user" gdbus call --session --dest $APP_ID --object-path /io/github/shotclip \\
                --method org.gtk.Actions.Activate quit "[]" "{}" >/dev/null 2>&1 || true
            sleep 1
            as_user "\$user" systemd-run --user --quiet --collect \\
                systemd-cat -t shotclip /usr/bin/shotclip --daemon >/dev/null 2>&1 || true
        fi
        home="\$(getent passwd "\$user" | cut -d: -f6)"
        if [ -e "\$home/.local/bin/shotclip" ]; then
            echo "Note: an older per-user ShotClip is installed in \$home/.local; it takes"
            echo "precedence. Remove it with uninstall.sh from the source folder, then log out and in."
        fi
    fi
    echo "ShotClip installed. Log out and back in once to finish (GNOME loads the"
    echo "ShotClip shell extension at login). Then press Super+V."
fi
exit 0
EOF

cat >"$PKG/DEBIAN/prerm" <<EOF
#!/bin/sh
set -e
$AS_USER
if [ "\$1" = remove ]; then
    user="\$(target_user)"
    if [ -n "\$user" ]; then
        as_user "\$user" /usr/bin/shotclip --remove-setup >/dev/null 2>&1 || true
    fi
    pkill -f "/usr/bin/shotclip" >/dev/null 2>&1 || true
fi
exit 0
EOF

cat >"$PKG/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = purge ]; then
    gtk-update-icon-cache -q /usr/share/icons/hicolor >/dev/null 2>&1 || true
    update-desktop-database -q /usr/share/applications >/dev/null 2>&1 || true
fi
exit 0
EOF
chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/prerm" "$PKG/DEBIAN/postrm"

mkdir -p "$OUT_DIR"
DEB="$OUT_DIR/shotclip_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$PKG" "$DEB" >/dev/null
echo "Built $DEB ($(du -h "$DEB" | cut -f1))"
echo "Install with: sudo apt install $DEB"
