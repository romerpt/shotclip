#!/usr/bin/env bash
# Build dist/shotclip-installer.sh: one self-extracting file to share.
#   bash shotclip-installer.sh              install (args are passed to install.sh)
#   bash shotclip-installer.sh --uninstall  uninstall
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$SRC_DIR/dist/shotclip-installer.sh"
FILES=(LICENSE shotclip.py install.sh uninstall.sh README.md extension/extension.js extension/metadata.json
       data/io.github.shotclip.svg)

mkdir -p "$SRC_DIR/dist"
cat >"$OUT" <<'EOF'
#!/usr/bin/env bash
# ShotClip installer: clipboard history for GNOME on Wayland (Super+V).
#   bash shotclip-installer.sh              install
#   bash shotclip-installer.sh --uninstall  uninstall (history is kept)
set -euo pipefail
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
sed '1,/^__SHOTCLIP_PAYLOAD__$/d' "$0" | base64 -d | tar -xz -C "$tmp"
if [[ "${1:-}" == "--uninstall" ]]; then
    bash "$tmp/shotclip/uninstall.sh"
else
    bash "$tmp/shotclip/install.sh" "$@"
fi
exit 0
__SHOTCLIP_PAYLOAD__
EOF
tar -C "$SRC_DIR" --transform 's,^,shotclip/,' --owner=0 --group=0 -czf - "${FILES[@]}" | base64 >>"$OUT"
chmod +x "$OUT"
echo "Built $OUT ($(du -h "$OUT" | cut -f1))"
