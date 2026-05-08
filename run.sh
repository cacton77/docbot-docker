#!/bin/bash

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR" || exit 1

if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

CONTAINER_NAME="${CONTAINER_NAME:-ros2-docker-template}"
COMPOSE_PROFILE="${COMPOSE_PROFILE:-rpi}"
COMPOSE_SERVICE="${COMPOSE_SERVICE:-rpi}"

COMPOSE_CMD="docker compose --profile $COMPOSE_PROFILE"

echo "Stopping any existing '$CONTAINER_NAME' containers..."
$COMPOSE_CMD down --remove-orphans
mapfile -t STALE < <(docker ps -aq --filter "name=^${CONTAINER_NAME}")
if [ ${#STALE[@]} -gt 0 ]; then
    docker rm -f "${STALE[@]}" >/dev/null
fi

exec "$SCRIPT_DIR/connect.sh" ros2 launch doc_system bringup.launch.py
 "$@"
