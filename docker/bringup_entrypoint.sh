#!/bin/bash
# Basic entrypoint for ROS / Colcon Docker containers

# Source ROS 2
source /opt/ros/${ROS_DISTRO}/setup.bash

# Source the base workspace, if built
if [ -f /workspaces/base_ws/install/setup.bash ]
then
    source /workspaces/base_ws/install/setup.bash
fi

# Build the overlay workspace
if [ -d /workspaces/shared_ws/src ]
then
    colcon build --base-paths /workspaces/shared_ws/src
fi

# Source the overlay workspace, if built
if [ -f /workspaces/shared_ws/install/setup.bash ]
then
    source /workspaces/shared_ws/install/setup.bash
fi

# Set Middleware to CycloneDDS
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp

exec "ros2 launch viewpoint_generation bringup.launch.py $@"
