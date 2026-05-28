#!/bin/bash
# Basic entrypoint for ROS / Colcon Docker containers

# Source ROS 2
source /opt/ros/${ROS_DISTRO}/setup.bash

# Source the base workspace, if built
if [ -f /workspaces/base_ws/install/setup.bash ]
then
    source /workspaces/base_ws/install/setup.bash
fi

# Source gscam2 workspace (Jetson only, built into image)
if [ -f /workspaces/gscam2_ws/install/setup.bash ]
then
    source /workspaces/gscam2_ws/install/setup.bash
fi

# Source the overlay workspace, if built. If not, build it.
if [ -f /workspaces/shared_ws/install/setup.bash ]
then
    source /workspaces/shared_ws/install/setup.bash
else
    echo "Shared workspace not found. Building shared workspace..."
    if (cd /workspaces/shared_ws && colcon build); then
        source /workspaces/shared_ws/install/setup.bash
        echo "✓ Shared workspace built and sourced"
    else
        echo "⚠ Shared workspace build failed"
    fi
fi

# Middleware selection. Honors RMW_IMPLEMENTATION from the environment
# (set via docker-compose / .env). Defaults to FastDDS because the
# micro-ROS agent is built against FastDDS — the rest of the stack must
# match for entities to discover each other.
export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}

exec "$@"