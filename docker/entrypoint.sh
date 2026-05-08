#!/bin/bash
# Basic entrypoint for ROS / Colcon Docker containers

# Source ROS 2
source /opt/ros/${ROS_DISTRO}/setup.bash

# Source the base workspace, if built
if [ -f /workspaces/base_ws/install/setup.bash ]
then
    source /workspaces/base_ws/install/setup.bash
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

# Set Middleware to CycloneDDS
# Disabled: micro-ROS agent is built with FastDDS, so the rest of the stack
# must also be on FastDDS for entities to discover each other.
# export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp

exec "$@"