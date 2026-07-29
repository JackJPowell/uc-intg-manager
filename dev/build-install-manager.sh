#!/usr/bin/env bash
# build-install-manager.sh
# Builds the Integration Manager with PyInstaller (aarch64) and installs it on the remote.
# Existing installations are upgraded in place, preserving configuration and entities.
#
# Usage:
#   ./dev/build-install-manager.sh [REMOTE_HOST] [PIN]
#
# Arguments:
#   REMOTE_HOST  = IP address of the UC Remote (required)
#   PIN          = Web-configurator PIN (required)

set -euo pipefail

REMOTE_HOST="${1:?Usage: $0 REMOTE_HOST PIN}"
PIN="${2:?Usage: $0 REMOTE_HOST PIN}"
REMOTE_USER="web-configurator"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(cd "$SCRIPT_DIR/.." && pwd)"

DRIVER_ID="$(jq -r .driver_id "$WORKSPACE/driver.json")"
INTG_DIR="intg-manager"
INTG_NAME="manager"
PYTHON_VER="3.11.12-0.3.0"
ARTIFACT_DIR="$WORKSPACE/dist-manager"
ARCHIVE_NAME="uc-intg-manager.tar.gz"

echo "==> Building Integration Manager (driver_id=${DRIVER_ID}) for aarch64..."

cd "$WORKSPACE"

echo "==> Building React UI..."
npm --prefix "$WORKSPACE/ui" ci
npm --prefix "$WORKSPACE/ui" run build

rm -rf "$WORKSPACE/dist/intg-${DRIVER_ID}" "$WORKSPACE/build/intg-${DRIVER_ID}"

docker run --rm --name builder \
    --platform linux/arm64 \
    --user="$(id -u):$(id -g)" \
    -v "$WORKSPACE":/workspace \
    "docker.io/unfoldedcircle/r2-pyinstaller:${PYTHON_VER}" \
    bash -c "
      cd /workspace && \
      python -m pip install --no-cache-dir -q -r requirements.txt && \
      pyinstaller --clean --onedir --name intg-${DRIVER_ID} \
                --collect-all zeroconf \
                --collect-all quart \
                --collect-all hypercorn \
                --add-data 'intg-${INTG_NAME}/static:static' \
                intg-${INTG_NAME}/driver.py"

echo "==> Packaging archive..."
rm -rf "$ARTIFACT_DIR"
mkdir -p "$ARTIFACT_DIR/bin"

mv "$WORKSPACE/dist/intg-${DRIVER_ID}"/* "$ARTIFACT_DIR/bin/"
mv "$ARTIFACT_DIR/bin/intg-${DRIVER_ID}" "$ARTIFACT_DIR/bin/driver"
cp "$WORKSPACE/driver.json" "$ARTIFACT_DIR/driver.json"
cp "$WORKSPACE/intg-manager/static/img/intg-manager.png" "$ARTIFACT_DIR/intg-manager.png"

tar czf "$WORKSPACE/${ARCHIVE_NAME}" -C "$ARTIFACT_DIR" .

echo "==> Archive created: ${ARCHIVE_NAME} ($(du -sh "$WORKSPACE/${ARCHIVE_NAME}" | cut -f1))"

# ---- In-place install / update ---------------------------------------------
echo "==> Updating on remote: https://${REMOTE_HOST}..."
curl --silent --show-error --fail --insecure \
    --location "https://${REMOTE_HOST}/api/intg/install?update=true" \
    --user "${REMOTE_USER}:${PIN}" \
    --form "file=@\"${WORKSPACE}/${ARCHIVE_NAME}\"" \
    | python3 -m json.tool

echo ""
echo "Done. Integration Manager updated on https://${REMOTE_HOST}"
echo "Driver ID: ${DRIVER_ID}"
