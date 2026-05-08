import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    config = os.path.join(
        get_package_share_directory("doc_control"),
        "config",
        "servo.yaml",
    )

    return LaunchDescription([
        Node(
            package="doc_control",
            executable="servo_node",
            name="servo_node",
            parameters=[config],
            output="screen",
        ),
    ])
