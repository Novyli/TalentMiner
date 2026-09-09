#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir"

python_bin="${PYTHON_BIN:-$project_dir/venv/bin/python}"
"$python_bin" -m PyInstaller --noconfirm --clean GQITalentRadar.spec

if [[ -n "${MACOS_SIGN_IDENTITY:-}" ]]; then
  codesign --force --deep --options runtime --timestamp \
    --sign "$MACOS_SIGN_IDENTITY" "dist/GQI Talent Radar.app"
  codesign --verify --deep --strict --verbose=2 "dist/GQI Talent Radar.app"
fi

mkdir -p release
dmg_root="$(mktemp -d)"
trap 'rm -rf "$dmg_root"' EXIT
cp -R "dist/GQI Talent Radar.app" "$dmg_root/GQI Talent Radar.app"
ln -s /Applications "$dmg_root/Applications"

arch_name="$(uname -m)"
output="release/GQI-Talent-Radar-macOS-${arch_name}.dmg"
rm -f "$output"
hdiutil create -volname "GQI Talent Radar" -srcfolder "$dmg_root" -ov -format UDZO "$output"

if [[ -n "${MACOS_SIGN_IDENTITY:-}" ]]; then
  codesign --force --timestamp --sign "$MACOS_SIGN_IDENTITY" "$output"
fi

if [[ -n "${NOTARYTOOL_PROFILE:-}" ]]; then
  xcrun notarytool submit "$output" --keychain-profile "$NOTARYTOOL_PROFILE" --wait
  xcrun stapler staple "$output"
fi

echo "Created $output"
