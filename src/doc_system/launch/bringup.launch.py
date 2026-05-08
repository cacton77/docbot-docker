from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource


def generate_launch_description():
    cameras_pkg = get_package_share_directory("doc_cameras")
    vision_pkg  = get_package_share_directory("doc_vision")

    cameras = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            f"{cameras_pkg}/launch/full_pipeline.launch.py"
        )
    )
    vision = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            f"{vision_pkg}/launch/insightface.launch.py"
        )
    )

    return LaunchDescription([cameras, vision])
