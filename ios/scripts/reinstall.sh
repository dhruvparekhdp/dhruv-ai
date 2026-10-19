#!/bin/bash
#
# Rebuild Jarvis and put it back on the iPhone. Adapted from Orbit (dhruv-ios).
#
# You need this roughly weekly: a free Personal Team provisioning profile is
# valid for 7 days, after which the app refuses to launch on the device. There
# is nothing wrong when that happens — just run this again.
#
#   ./scripts/reinstall.sh              # first paired device
#   ./scripts/reinstall.sh <device-id>  # a specific one
#
# A paid Apple Developer Program membership raises the 7 days to a year and
# makes this script unnecessary. See ios/README.md.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="$REPO_ROOT/Jarvis.xcodeproj"
BUNDLE_ID="com.dhruvparekh.jarvis"

if [ ! -d "$PROJECT" ]; then
    echo "No Jarvis.xcodeproj — it is gitignored and generated. Run:"
    echo "    cd '$REPO_ROOT' && xcodegen"
    exit 1
fi

# --- find the device -------------------------------------------------------

if [ $# -ge 1 ]; then
    DEVICE_ID="$1"
else
    DEVICE_ID=$(xcodebuild -project "$PROJECT" -scheme Jarvis -showdestinations 2>/dev/null \
        | grep "platform:iOS" | grep -v "Simulator" | grep -v "placeholder" \
        | sed -n 's/.*id:\([0-9A-Za-z-]*\).*/\1/p' | head -1)
fi

if [ -z "${DEVICE_ID:-}" ]; then
    echo "No paired iPhone found. Plug it in, unlock it, and trust this Mac."
    exit 1
fi

echo "==> Device $DEVICE_ID"

# --- build -----------------------------------------------------------------
# 'generic/platform=iOS' rather than the specific device: it builds without
# waiting for the device to become available, which fails while it is locked.

echo "==> Building (signed)"
xcodebuild -project "$PROJECT" -scheme Jarvis \
    -destination 'generic/platform=iOS' \
    -allowProvisioningUpdates \
    build > /tmp/jarvis-reinstall.log 2>&1 || {
        echo "Build failed. Last errors:"
        grep -E "error:" /tmp/jarvis-reinstall.log | head -10
        echo "(full log: /tmp/jarvis-reinstall.log)"
        exit 1
    }

APP=$(find ~/Library/Developer/Xcode/DerivedData/Jarvis-*/Build/Products/Debug-iphoneos \
    -maxdepth 1 -name "Jarvis.app" 2>/dev/null | head -1)

if [ -z "${APP:-}" ]; then
    echo "Built, but no Jarvis.app found in DerivedData."
    exit 1
fi

# --- install ---------------------------------------------------------------

echo "==> Installing"
if ! xcrun devicectl device install app --device "$DEVICE_ID" "$APP" \
        > /tmp/jarvis-install.log 2>&1; then
    if grep -q "DeviceLocked" /tmp/jarvis-install.log; then
        echo
        echo "  The iPhone is locked. iOS will not mount the developer disk"
        echo "  image on a locked device — unlock it, keep the screen on, and"
        echo "  run this again. (The error text blames the disk image, not the"
        echo "  lock screen, which is misleading.)"
        exit 1
    fi
    echo "Install failed:"
    grep -iE "error" /tmp/jarvis-install.log | head -10
    exit 1
fi

echo "==> Launching"
if xcrun devicectl device process launch --device "$DEVICE_ID" "$BUNDLE_ID" \
        > /tmp/jarvis-launch.log 2>&1; then
    echo
    echo "Done. Jarvis is on the phone — good for another 7 days."
else
    # Installing works on a locked phone; launching does not. The install is
    # the part that matters, so this is not a failure.
    echo
    echo "Installed fine — but could not auto-launch:"
    if grep -q "Locked" /tmp/jarvis-launch.log; then
        echo "  the phone is locked. Just unlock it and tap Jarvis."
    else
        grep -iE "NSLocalizedFailureReason" /tmp/jarvis-launch.log | head -2
    fi
    echo "Either way, the app is installed and good for another 7 days."
fi
