#!/bin/bash
# Gera dist/Transcritor-<versão>-macOS-<arch>.dmg a partir de dist/Transcritor.app
set -euo pipefail
cd "$(dirname "$0")/../.."
VERSION=$(python3 -c "import re;print(re.search(r'\"(.+)\"', open('src/transcritor/__init__.py').read())[1])")
ARCH=${ARCH:-$(uname -m)}
APP=${APP_DIR:-dist}/Transcritor.app
DMG=dist/Transcritor-${VERSION}-macOS-${ARCH}.dmg
[ -d "$APP" ] || { echo "Rode o PyInstaller primeiro"; exit 1; }

# Assinatura ad-hoc: evita o erro "aplicativo danificado" no Apple Silicon.
# Para distribuir sem alertas do Gatekeeper, assine com um Developer ID e notarize.
codesign --force --deep --sign "${CODESIGN_IDENTITY:--}" "$APP"

STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "Transcritor" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"
echo "Instalador criado: $DMG"
