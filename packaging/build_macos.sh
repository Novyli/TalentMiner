#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir"

python_bin="${PYTHON_BIN:-$project_dir/venv/bin/python}"
"$python_bin" -m PyInstaller --noconfirm --clean TalentMiner.spec

if [[ -n "${MACOS_SIGN_IDENTITY:-}" ]]; then
  codesign --force --deep --options runtime --timestamp \
    --sign "$MACOS_SIGN_IDENTITY" dist/TalentMiner.app
  codesign --verify --deep --strict --verbose=2 dist/TalentMiner.app
fi

mkdir -p release
dmg_root="$(mktemp -d)"
trap 'rm -rf "$dmg_root"' EXIT
cp -R dist/TalentMiner.app "$dmg_root/TalentMiner.app"
ln -s /Applications "$dmg_root/Applications"

arch_name="$(uname -m)"
output="release/TalentMiner-macOS-${arch_name}.dmg"
rm -f "$output"
hdiutil create -volname TalentMiner -srcfolder "$dmg_root" -ov -format UDZO "$output"

if [[ -n "${MACOS_SIGN_IDENTITY:-}" ]]; then
  codesign --force --timestamp --sign "$MACOS_SIGN_IDENTITY" "$output"
fi

if [[ -n "${NOTARYTOOL_PROFILE:-}" ]]; then
  xcrun notarytool submit "$output" --keychain-profile "$NOTARYTOOL_PROFILE" --wait
  xcrun stapler staple "$output"
fi

echo "Created $output"
