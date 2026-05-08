import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    config = os.path.join(
        get_package_share_directory("doc_vision"),
        "config",
        "insightface.yaml",
    )

    return LaunchDescription([
        Node(
            package="doc_vision",
            executable="insightface_node",
            name="insightface_node",
            parameters=[config],
            output="screen",
        ),
    ])
