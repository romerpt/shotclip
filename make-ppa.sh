#!/usr/bin/env bash
# Build the source package for the Launchpad PPA (Launchpad builds the .deb).
#   ./make-ppa.sh             build and sign dist/ppa/shotclip_<version>_source.changes
#   ./make-ppa.sh --upload    ...and upload it to ppa:romerpt/shotclip
#   ./make-ppa.sh --unsigned  build without signing (to check that it builds)
# Needs: dpkg-dev, a GPG key for the debian/changelog e-mail registered on
# Launchpad, and dput for --upload. Builds from the last commit (git archive),
# so commit your changes first.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PPA="${PPA:-ppa:romerpt/shotclip}"
MODE="${1:-}"

if [[ -n "$(git -C "$SRC_DIR" status --porcelain --untracked-files=no)" ]]; then
    echo "Uncommitted changes: commit them first (the package is built from HEAD)." >&2
    exit 1
fi
VERSION="$(dpkg-parsechangelog -l "$SRC_DIR/debian/changelog" -S Version)"
APP_VERSION="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$SRC_DIR/shotclip.py")"
if [[ "${VERSION%%~*}" != "$APP_VERSION" ]]; then
    echo "debian/changelog is at $VERSION but shotclip.py at $APP_VERSION." >&2
    exit 1
fi

OUT="$SRC_DIR/dist/ppa"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
rm -rf "$OUT"
mkdir -p "$OUT"

git -C "$SRC_DIR" archive --prefix="shotclip-$VERSION/" HEAD | tar -x -C "$WORK"
cd "$WORK/shotclip-$VERSION"
sign=()
[[ "$MODE" == "--unsigned" ]] && sign=(-us -uc)
# -nc: the export is already clean; -d: debhelper is only needed by Launchpad.
dpkg-buildpackage -S -sa -d -nc "${sign[@]}"
mv "$WORK"/shotclip_"$VERSION"* "$OUT"/
echo "Source package in $OUT:"
ls -1 "$OUT"

if [[ "$MODE" == "--upload" ]]; then
    dput "$PPA" "$OUT/shotclip_${VERSION}_source.changes"
    echo "Uploaded. Launchpad e-mails you when the build is accepted and done:"
    echo "  https://launchpad.net/~romerpt/+archive/ubuntu/shotclip"
fi
