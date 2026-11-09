#!/usr/bin/env bash
# Build dist/shotclip_<version>_all.deb without debhelper (for a quick local
# build or a GitHub release). It uses the same files as the PPA package in
# debian/ (control fields, install scripts, desktop entries).
#   sudo apt install ./dist/shotclip_<version>_all.deb
set -euo pipefail
umask 022

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="io.github.shotclip"
EXT_UUID="shotclip@shotclip.github.io"
VERSION="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$SRC_DIR/shotclip.py")"
CHANGELOG_VERSION="$(sed -n '1s/^shotclip (\(.*\)).*/\1/p' "$SRC_DIR/debian/changelog")"
[[ -n "$VERSION" ]] || { echo "VERSION not found in shotclip.py" >&2; exit 1; }
if [[ "$VERSION" != "${CHANGELOG_VERSION%%~*}" ]]; then
    echo "shotclip.py says $VERSION but debian/changelog says $CHANGELOG_VERSION;" >&2
    echo "add a debian/changelog entry (dch -v $VERSION) first." >&2
    exit 1
fi

OUT_DIR="$SRC_DIR/dist"
PKG="$(mktemp -d)"
trap 'rm -rf "$PKG"' EXIT
chmod 755 "$PKG"

# -- files (the same as debian/rules installs) ------------------------------------
install -Dm755 "$SRC_DIR/shotclip.py" "$PKG/usr/bin/shotclip"
sed -i '1s|^#!.*|#!/usr/bin/python3|' "$PKG/usr/bin/shotclip"
install -Dm644 "$SRC_DIR/extension/extension.js" "$PKG/usr/share/gnome-shell/extensions/$EXT_UUID/extension.js"
install -Dm644 "$SRC_DIR/extension/metadata.json" "$PKG/usr/share/gnome-shell/extensions/$EXT_UUID/metadata.json"
install -Dm644 "$SRC_DIR/data/$APP_ID.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
install -Dm644 "$SRC_DIR/data/$APP_ID.desktop" "$PKG/usr/share/applications/$APP_ID.desktop"
install -Dm644 "$SRC_DIR/data/$APP_ID-autostart.desktop" "$PKG/etc/xdg/autostart/$APP_ID.desktop"
install -Dm644 "$SRC_DIR/README.md" "$PKG/usr/share/doc/shotclip/README.md"
install -Dm644 "$SRC_DIR/debian/copyright" "$PKG/usr/share/doc/shotclip/copyright"

# -- package metadata, taken from debian/control ----------------------------------
install -d "$PKG/DEBIAN"
echo "/etc/xdg/autostart/$APP_ID.desktop" >"$PKG/DEBIAN/conffiles"
field() {  # field NAME: a field of the binary package stanza, joined on one line
    awk -v f="$1" '
        /^Package:/ { bin = 1 }
        bin && $0 ~ "^" f ":" { sub("^" f ":[ ]*", ""); v = $0; on = 1; next }
        bin && on && /^[ \t]/ && f != "Description" { sub(/^[ \t]+/, ""); v = v " " $0; next }
        { on = 0 }
        END { print v }' "$SRC_DIR/debian/control"
}
depends="$(field Depends | sed 's/\${misc:Depends},[ ]*//')"
{
    echo "Package: shotclip"
    echo "Version: $CHANGELOG_VERSION"
    echo "Architecture: all"
    echo "Section: $(sed -n 's/^Section: //p' "$SRC_DIR/debian/control" | head -1)"
    echo "Priority: optional"
    echo "Maintainer: $(sed -n 's/^Maintainer: //p' "$SRC_DIR/debian/control")"
    echo "Installed-Size: $(du -sk --exclude=DEBIAN "$PKG" | cut -f1)"
    echo "Depends: $depends"
    echo "Homepage: $(sed -n 's/^Homepage: //p' "$SRC_DIR/debian/control")"
    sed -n '/^Package:/,$p' "$SRC_DIR/debian/control" | sed -n '/^Description:/,$p'
} >"$PKG/DEBIAN/control"

for script in postinst prerm; do
    grep -v '^#DEBHELPER#$' "$SRC_DIR/debian/shotclip.$script" >"$PKG/DEBIAN/$script"
    chmod 755 "$PKG/DEBIAN/$script"
done

mkdir -p "$OUT_DIR"
DEB="$OUT_DIR/shotclip_${CHANGELOG_VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$PKG" "$DEB" >/dev/null
echo "Built $DEB ($(du -h "$DEB" | cut -f1))"
echo "Install with: sudo apt install $DEB"
