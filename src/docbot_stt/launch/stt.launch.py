"""Launch the STT node alone, for bring-up testing."""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    pkg_share = get_package_share_directory('docbot_stt')
    default_params = os.path.join(pkg_share, 'config', 'stt.yaml')

    params_arg = DeclareLaunchArgument(
        'params_file',
        default_value=default_params,
        description='Path to ROS 2 parameter YAML for the STT node.',
    )

    stt_node = Node(
        package='docbot_stt',
        executable='transcribe_node',
        name='stt',
        output='screen',
        emulate_tty=True,
        parameters=[LaunchConfiguration('params_file')],
    )

    return LaunchDescription([params_arg, stt_node])
