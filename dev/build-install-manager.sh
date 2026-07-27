#!/usr/bin/env bash
# build-install-manager.sh
# Builds the Integration Manager with PyInstaller (aarch64) and installs it on the remote.
# If an existing instance is running it will be deleted first (remote doesn't support in-place update).
#
# Usage:
#   ./dev/build-install-manager.sh [REMOTE_HOST] [PIN] [MAC_IP]
#
# If MAC_IP is provided, an HTTP server is started serving the IM archive and
# build-install-bootstrapper.sh is called automatically with DEV_DOWNLOAD_URL
# pointing at it.  IM itself is NOT installed on the remote in this mode.
#
# Arguments:
#   REMOTE_HOST  = IP address of the UC Remote (required)
#   PIN          = Web-configurator PIN (required)
#   MAC_IP       = (optional) host IP to serve the IM archive for bootstrapper testing

set -euo pipefail

REMOTE_HOST="${1:?Usage: $0 REMOTE_HOST PIN [MAC_IP]}"
PIN="${2:?Usage: $0 REMOTE_HOST PIN [MAC_IP]}"
MAC_IP="${3:-}"
HTTP_PORT="8000"
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

# ---- If MAC_IP given: start HTTP server and install bootstrapper ----------
if [ -n "$MAC_IP" ]; then
    DEV_URL="http://${MAC_IP}:${HTTP_PORT}/${ARCHIVE_NAME}"
    echo ""
    echo "==> Starting HTTP server on port ${HTTP_PORT} serving $WORKSPACE ..."
    lsof -ti tcp:"$HTTP_PORT" | xargs kill -9 2>/dev/null || true
    python3 -m http.server "$HTTP_PORT" --directory "$WORKSPACE" &
    HTTP_PID=$!
    echo "   HTTP server PID: ${HTTP_PID} — serving ${DEV_URL}"
    sleep 1
    echo ""
    echo "==> Calling build-install-bootstrapper.sh with DEV_DOWNLOAD_URL=${DEV_URL}"
    "$SCRIPT_DIR/build-install-bootstrapper.sh" "$REMOTE_HOST" "$PIN" "$DEV_URL"
    echo ""
    echo "Done. HTTP server still running (PID ${HTTP_PID}) — kill it once the upgrade completes."
    echo "  kill ${HTTP_PID}"
    exit 0
fi

# ---- Delete existing instance + driver if present -------------------------
echo "==> Checking for existing installation of ${DRIVER_ID} on remote..."

INSTANCES=$(curl --silent --fail --insecure \
    "https://${REMOTE_HOST}/api/intg/instances?driver_id=${DRIVER_ID}" \
    --user "${REMOTE_USER}:${PIN}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
items = data.get('items', data) if isinstance(data, dict) else data
print('\n'.join(i['id'] for i in items))
" 2>/dev/null || true)

for INSTANCE_ID in $INSTANCES; do
    echo "   Deleting instance: ${INSTANCE_ID}"
    curl --silent --fail --insecure -X DELETE \
        "https://${REMOTE_HOST}/api/intg/instances/${INSTANCE_ID}" \
        --user "${REMOTE_USER}:${PIN}" > /dev/null
done

DRIVER_EXISTS=$(curl --silent --insecure -o /dev/null -w "%{http_code}" \
    "https://${REMOTE_HOST}/api/intg/drivers/${DRIVER_ID}" \
    --user "${REMOTE_USER}:${PIN}")

if [ "$DRIVER_EXISTS" = "200" ]; then
    echo "   Deleting driver: ${DRIVER_ID}"
    curl --silent --fail --insecure -X DELETE \
        "https://${REMOTE_HOST}/api/intg/drivers/${DRIVER_ID}" \
        --user "${REMOTE_USER}:${PIN}" > /dev/null
fi

# ---- Install ---------------------------------------------------------------
echo "==> Installing on remote: https://${REMOTE_HOST}..."
curl --silent --show-error --fail --insecure \
    --location "https://${REMOTE_HOST}/api/intg/install" \
    --user "${REMOTE_USER}:${PIN}" \
    --form "file=@\"${WORKSPACE}/${ARCHIVE_NAME}\"" \
    | python3 -m json.tool

echo ""
echo "Done. Integration Manager installed on https://${REMOTE_HOST}"
echo "Driver ID: ${DRIVER_ID}"
