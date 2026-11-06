#!/usr/bin/env bash
# Install a pinned gitleaks binary into .tools/ and check its sha256.
# The checksums are copied from the v8.30.1 release checksums file so a
# replaced GitHub asset cannot satisfy a checksum downloaded beside it.

set -euo pipefail

VERSION=8.30.1
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/.tools/gitleaks"
URL_BASE="https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}"

if [[ -x "$DEST" ]]; then
  current="$("$DEST" version 2>/dev/null || true)"
  if [[ "$current" == *"$VERSION"* ]]; then
    exit 0
  fi
fi

os="$(uname -s)"
arch="$(uname -m)"
case "${os}-${arch}" in
  Linux-x86_64)
    asset="linux_x64"
    sum="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
    ;;
  Linux-aarch64)
    asset="linux_arm64"
    sum="e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"
    ;;
  Darwin-arm64)
    asset="darwin_arm64"
    sum="b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"
    ;;
  Darwin-x86_64)
    asset="darwin_x64"
    sum="dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709"
    ;;
  *)
    echo "unsupported platform for gitleaks: ${os}-${arch}" >&2
    exit 1
    ;;
esac

archive="gitleaks_${VERSION}_${asset}.tar.gz"
workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

curl -fsSL "$URL_BASE/$archive" -o "$workdir/$archive"
if command -v sha256sum >/dev/null 2>&1; then
  echo "$sum  $archive" | (cd "$workdir" && sha256sum -c -)
else
  echo "$sum  $archive" | (cd "$workdir" && shasum -a 256 -c -)
fi

tar -xzf "$workdir/$archive" -C "$workdir" gitleaks
mkdir -p "$ROOT/.tools"
install -m 0755 "$workdir/gitleaks" "$DEST"
echo "installed gitleaks ${VERSION} to .tools/gitleaks"
