#!/usr/bin/env python3
"""Launch the GoPro manager node with its parameter file."""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    params = os.path.join(
        get_package_share_directory('gopro_control'), 'params', 'gopro_params.yaml')
    return LaunchDescription([
        Node(
            package='gopro_control',
            executable='gopro_manager',
            name='gopro_manager',
            output='screen',
            parameters=[params],
        ),
    ])
