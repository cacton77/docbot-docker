#!/bin/bash

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR" || exit 1

# Load environment variables
if [ -f .env ]; then
    source .env
fi

# Set up X11 auth for EGL (nvarguscamerasrc requires a valid EGLDisplay).
# When connected via SSH, DISPLAY is unset — fall back to :0 (the local desktop session).
# Detect the active X11 display from the first socket in /tmp/.X11-unix,
# falling back to :1 (the default on this Jetson). :0 is wrong here.
if [ -z "$DISPLAY" ]; then
    for _sock in /tmp/.X11-unix/X*; do
        [ -S "$_sock" ] && export DISPLAY=":${_sock##*X}" && break
    done
    export DISPLAY="${DISPLAY:-:1}"
fi

XAUTH_FILE="/tmp/.docker.xauth"
touch "$XAUTH_FILE" 2>/dev/null || true

# Try current DISPLAY auth first, then common desktop session auth file locations
if command -v xauth &>/dev/null; then
    xauth nlist "$DISPLAY" 2>/dev/null \
        | sed -e 's/^..../ffff/' \
        | xauth -f "$XAUTH_FILE" nmerge - 2>/dev/null || true
fi

# If still empty, pull from the running desktop session's auth file
if ! xauth -f "$XAUTH_FILE" list 2>/dev/null | grep -q .; then
    for f in \
        "/run/user/$(id -u)/gdm/Xauthority" \
        "$HOME/.Xauthority" \
        "/var/run/lightdm/root/$DISPLAY"; do
        if [ -f "$f" ]; then
            xauth -f "$f" nlist 2>/dev/null \
                | sed -e 's/^..../ffff/' \
                | xauth -f "$XAUTH_FILE" nmerge - 2>/dev/null && break
        fi
    done
fi

xhost +local:docker 2>/dev/null || true

# Default container name if not set
CONTAINER_NAME="${CONTAINER_NAME:-ros2-docker-template}"
COMPOSE_PROFILE="${COMPOSE_PROFILE:-rpi}"
COMPOSE_SERVICE="${COMPOSE_SERVICE:-rpi}"

COMPOSE_CMD="docker compose --profile $COMPOSE_PROFILE"

# Check if a container is already running (exact match or compose run pattern)
RUNNING_CONTAINER=$(docker ps --format '{{.Names}}' | grep -E "^${CONTAINER_NAME}(-.*-run-.*)?$" | head -n 1)

TTY_FLAG=""
[ -t 0 ] && TTY_FLAG="-it"
if [ -n "$RUNNING_CONTAINER" ]; then
    echo "Container '$RUNNING_CONTAINER' is already running. Executing bash..."
    if [ $# -eq 0 ]; then
        docker exec $TTY_FLAG "$RUNNING_CONTAINER" bash -c "source /entrypoint.sh && exec bash"
    else
        docker exec $TTY_FLAG "$RUNNING_CONTAINER" bash -c "source /entrypoint.sh && $*"
    fi
else
    echo "Starting container '$CONTAINER_NAME' (profile: $COMPOSE_PROFILE)..."
    if [ $# -eq 0 ]; then
        $COMPOSE_CMD run --rm "$COMPOSE_SERVICE" /bin/bash
    else
        # Don't use "bash -c" wrapper - let entrypoint handle environment and run command directly
        $COMPOSE_CMD run --rm "$COMPOSE_SERVICE" "$@"
    fi
fi
