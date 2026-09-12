#!/bin/bash
# Build the heygent.zip that the README's download link hands out:
# the standalone bundle, signed with the Developer ID, notarized by
# Apple, stapled, and zipped without the AppleDouble (._*) side files
# that make Gatekeeper call the signature unusable.
#
#     scripts/release.sh                 # -> dist/release/heygent.zip
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
# Publishing is separate: upload dist/release/heygent.zip as the one
# asset of a GitHub release; the README link always points at the
# latest release's heygent.zip.
set -euo pipefail

cd "$(dirname "$0")/.."

IDENTITY="${HEYGENT_SIGN_IDENTITY:-Developer ID Application: Lambo Labs, Inc. (UL4H2AU46Z)}"
DIST="${HEYGENT_RELEASE_DIR:-dist/release}"
APP="$DIST/heygent.app"
ZIP="$DIST/heygent.zip"

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

echo "== notarize"
ditto -c -k --keepParent --norsrc --noextattr --noqtn "$APP" "$DIST/notarize.zip"
xcrun notarytool submit "$DIST/notarize.zip" --wait "${notary_args[@]}" \
  | tee "$DIST/notarize.log"
grep -q "status: Accepted" "$DIST/notarize.log" || {
  echo "notarization did not end Accepted; for the reasons:" >&2
  echo "  xcrun notarytool log <id> ${notary_args[*]}" >&2
  exit 1
}
rm "$DIST/notarize.zip"
xcrun stapler staple "$APP"

echo "== zip"
ditto -c -k --keepParent --norsrc --noextattr --noqtn "$APP" "$ZIP"

echo "== verify what a download gets"
check="$(mktemp -d)"
ditto -x -k "$ZIP" "$check"
if unzip -l "$ZIP" | grep -q '/\._'; then
  echo "AppleDouble files inside $ZIP" >&2
  exit 1
fi
codesign --verify --deep --strict "$check/heygent.app"
spctl --assess --type execute --verbose=2 "$check/heygent.app"
xcrun stapler validate "$check/heygent.app"
rm -rf "$check"

echo "ready: $ZIP ($(du -h "$ZIP" | cut -f1))"
