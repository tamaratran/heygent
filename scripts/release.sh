#!/bin/bash
# Build the heygent.dmg that the README's download link hands out: the
# standalone bundle, signed with the Developer ID, notarized by Apple
# and stapled, on a disk image with an Applications alias to drag it
# onto - the image itself signed, notarized and stapled too.
#
#     scripts/release.sh                 # -> dist/release/heygent.dmg
#
# Signing: the "Developer ID Application" identity must be in the
# keychain (HEYGENT_SIGN_IDENTITY overrides the default below).
#
# Notarization, one of:
#   - a notarytool keychain profile:  HEYGENT_NOTARY_PROFILE=heygent
#     (made once with `xcrun notarytool store-credentials heygent ...`)
#   - an App Store Connect API key:   HEYGENT_NOTARY_KEY (path to the .p8),
#     HEYGENT_NOTARY_KEY_ID, HEYGENT_NOTARY_ISSUER_ID
#   - an Apple ID:                    HEYGENT_NOTARY_APPLE_ID,
#     HEYGENT_NOTARY_APP_SPECIFIC_PASSWORD (an app-specific password from
#     account.apple.com, not the account password), HEYGENT_NOTARY_TEAM_ID
#
# Publishing is separate: upload dist/release/heygent.dmg as the one
# asset of a GitHub release; the README link always points at the
# latest release's heygent.dmg.
set -euo pipefail

cd "$(dirname "$0")/.."

IDENTITY="${HEYGENT_SIGN_IDENTITY:-Developer ID Application: Lambo Labs, Inc. (UL4H2AU46Z)}"
DIST="${HEYGENT_RELEASE_DIR:-dist/release}"
APP="$DIST/heygent.app"
DMG="$DIST/heygent.dmg"

notary_args=()
if [ -n "${HEYGENT_NOTARY_PROFILE:-}" ]; then
  notary_args=(--keychain-profile "$HEYGENT_NOTARY_PROFILE")
elif [ -n "${HEYGENT_NOTARY_KEY:-}" ]; then
  notary_args=(--key "$HEYGENT_NOTARY_KEY"
               --key-id "$HEYGENT_NOTARY_KEY_ID"
               --issuer "$HEYGENT_NOTARY_ISSUER_ID")
elif [ -n "${HEYGENT_NOTARY_APPLE_ID:-}" ]; then
  notary_args=(--apple-id "$HEYGENT_NOTARY_APPLE_ID"
               --password "$HEYGENT_NOTARY_APP_SPECIFIC_PASSWORD"
               --team-id "${HEYGENT_NOTARY_TEAM_ID:-UL4H2AU46Z}")
else
  echo "no notarization credentials set (see the top of $0)" >&2
  exit 2
fi

if ! security find-identity -v -p codesigning | grep -qF "$IDENTITY"; then
  echo "signing identity not in the keychain: $IDENTITY" >&2
  exit 2
fi

rm -rf "$DIST"
mkdir -p "$DIST"

echo "== build + sign"
python3 -m conductor.app_bundle --standalone --dest "$DIST" --sign "$IDENTITY"
codesign --verify --deep --strict --verbose=2 "$APP"

notarize() {  # <file> <log name>
  xcrun notarytool submit "$1" --wait "${notary_args[@]}" \
    | tee "$DIST/$2"
  grep -q "status: Accepted" "$DIST/$2" || {
    echo "notarization of $1 did not end Accepted; for the reasons:" >&2
    echo "  xcrun notarytool log <id> ${notary_args[*]}" >&2
    exit 1
  }
}

echo "== notarize the app"
# Submitted as a zip with no extended attributes (and none of the
# AppleDouble ._* files that carry them): what Apple sees is the
# unzipped copy, so a signature that lived only in xattrs is gone by
# then and the service says "the signature of the binary is invalid".
# Catch that here.
ditto -c -k --keepParent --norsrc --noextattr --noqtn "$APP" "$DIST/notarize.zip"
unzipped="$(mktemp -d)"
ditto -x -k "$DIST/notarize.zip" "$unzipped"
codesign --verify --deep --strict "$unzipped/heygent.app"
rm -rf "$unzipped"
notarize "$DIST/notarize.zip" notarize-app.log
rm "$DIST/notarize.zip"
xcrun stapler staple "$APP"

echo "== dmg"
# The Finder window a download is expected to open with: the app and
# an Applications folder to drag it onto. The image is signed and
# notarized in its own right, so opening it is as quiet as opening the
# app; the app inside carries its own stapled ticket.
#
# The window's look - the background with the arrow, the two icons
# placed on it, no toolbar - is Finder's view settings, stored in the
# volume's .DS_Store. Finder writes that file itself, so the image is
# first made writable, mounted, and laid out through Finder, then
# converted into the compressed read-only image that ships.
staging="$(mktemp -d)"
ditto "$APP" "$staging/heygent.app"
ln -s /Applications "$staging/Applications"
mkdir "$staging/.background"
uv run -q scripts/dmg_background.py "$staging/.background/background.png"
rw="$DIST/heygent-rw.dmg"
hdiutil create -quiet -volname heygent -srcfolder "$staging" -fs HFS+ \
  -format UDRW -ov "$rw"
rm -rf "$staging"
hdiutil attach -quiet -readwrite -noverify -noautoopen "$rw"
osascript scripts/dmg_layout.applescript heygent
sync
hdiutil detach -quiet /Volumes/heygent
hdiutil convert -quiet -format UDZO -imagekey zlib-level=9 -ov "$rw" -o "$DMG"
rm "$rw"
codesign --sign "$IDENTITY" --timestamp "$DMG"
notarize "$DMG" notarize-dmg.log
xcrun stapler staple "$DMG"

echo "== verify what a download gets"
spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG"
xcrun stapler validate "$DMG"
mount="$(mktemp -d)"
hdiutil attach -quiet -nobrowse -readonly -mountpoint "$mount" "$DMG"
[ -L "$mount/Applications" ] || { echo "no Applications alias in $DMG" >&2; exit 1; }
codesign --verify --deep --strict "$mount/heygent.app"
spctl --assess --type execute --verbose=2 "$mount/heygent.app"
xcrun stapler validate "$mount/heygent.app"
hdiutil detach -quiet "$mount"

echo "ready: $DMG ($(du -h "$DMG" | cut -f1))"
