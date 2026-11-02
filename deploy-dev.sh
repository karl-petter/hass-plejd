#!/bin/bash
set -e

# Pushes this checkout's custom_components/plejd and the pyplejd submodule
# onto a live Home Assistant OS instance, for testing a patch (or gathering
# debug-log evidence of a bug) before opening a PR.
#
# pyplejd is a pip-installed dependency (manifest.json pins
# "pyplejd==0.22.0"), not vendored under custom_components - on HAOS it
# lives inside the `homeassistant` core container's site-packages, not
# under /config. Reaching it needs `docker exec`/`docker cp` over SSH,
# which this script runs via passwordless sudo (confirmed available on
# the Advanced SSH & Web Terminal add-on with Protection mode off).
#
# Usage: ./deploy-dev.sh [user] [host]
# Falls back to HA_SSH_USER/HA_SSH_HOST(/HA_SSH_PORT) from
# ../ha-lksystems/.env.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Shared with ha-lksystems, which lives as a sibling checkout - same
# Home Assistant instance, one set of credentials, not duplicated here.
SHARED_ENV="$SCRIPT_DIR/../ha-lksystems/.env"
if [ -f "$SHARED_ENV" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$SHARED_ENV"
  set +a
fi

USER="${1:-$HA_SSH_USER}"
HOST_ARG="${2:-$HA_SSH_HOST}"

if [ -z "$USER" ] || [ -z "$HOST_ARG" ]; then
  echo "Usage: $0 <user> <host>  (or set HA_SSH_USER/HA_SSH_HOST in ../ha-lksystems/.env)"
  exit 1
fi

SSH_PORT_ARGS=()
if [ -n "$HA_SSH_PORT" ]; then
  SSH_PORT_ARGS=(-p "$HA_SSH_PORT")
fi

HOST="$USER@$HOST_ARG"
REMOTE_TMP="/tmp/pyplejd-dev-deploy"
REMOTE_COMPONENT_DIR="/config/custom_components/plejd"
CONTAINER="homeassistant"

echo "==> Syncing custom_components/plejd..."
ssh "${SSH_PORT_ARGS[@]}" "$HOST" "sudo mkdir -p $REMOTE_COMPONENT_DIR"
rsync -avO --delete --rsync-path="sudo rsync" -e "ssh ${SSH_PORT_ARGS[*]}" --exclude='__pycache__' --exclude='.DS_Store' \
  custom_components/plejd/ \
  "$HOST:$REMOTE_COMPONENT_DIR/"

echo "==> Syncing pyplejd package to a staging dir on the host..."
ssh "${SSH_PORT_ARGS[@]}" "$HOST" "rm -rf $REMOTE_TMP && mkdir -p $REMOTE_TMP"
rsync -avO --delete -e "ssh ${SSH_PORT_ARGS[*]}" --exclude='__pycache__' --exclude='.DS_Store' \
  pyplejd/pyplejd/ \
  "$HOST:$REMOTE_TMP/pyplejd/"

echo "==> Locating pyplejd's installed site-packages path in the $CONTAINER container..."
SITE_PACKAGES_DIR=$(ssh "${SSH_PORT_ARGS[@]}" "$HOST" \
  "sudo docker exec $CONTAINER python3 -c 'import pyplejd, os; print(os.path.dirname(pyplejd.__file__))'")
if [ -z "$SITE_PACKAGES_DIR" ]; then
  echo "Could not locate pyplejd inside the $CONTAINER container." >&2
  echo "Needs passwordless sudo docker access from the SSH add-on's shell." >&2
  exit 1
fi
echo "    Found: $SITE_PACKAGES_DIR"

echo "==> Copying staged pyplejd into the container (overwriting the installed package)..."
ssh "${SSH_PORT_ARGS[@]}" "$HOST" \
  "sudo docker exec $CONTAINER rm -rf '$SITE_PACKAGES_DIR' && sudo docker cp $REMOTE_TMP/pyplejd $CONTAINER:'$SITE_PACKAGES_DIR'"

echo "==> Restarting Home Assistant core so the new pyplejd code is loaded..."
ssh "${SSH_PORT_ARGS[@]}" "$HOST" "sudo docker restart $CONTAINER"

echo "==> Done! Once HA is back up, enable debug logging for pyplejd.ble.connection"
echo "    (see README.md's 'Debug logging' section) and watch Settings -> System -> Logs."
