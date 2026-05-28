"""Launch the TTS node alone, for bring-up testing."""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    pkg_share = get_package_share_directory('docbot_tts')
    default_params = os.path.join(pkg_share, 'config', 'tts.yaml')

    params_arg = DeclareLaunchArgument(
        'params_file',
        default_value=default_params,
        description='Path to ROS 2 parameter YAML for the TTS node.',
    )

    tts_node = Node(
        package='docbot_tts',
        executable='speak_node',
        name='tts',
        output='screen',
        emulate_tty=True,
        parameters=[LaunchConfiguration('params_file')],
    )

    return LaunchDescription([params_arg, tts_node])
