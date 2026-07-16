import os
from glob import glob

from setuptools import find_packages, setup

package_name = 'gopro_control'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'), glob('launch/*.py')),
        (os.path.join('share', package_name, 'params'), glob('params/*.yaml')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='melanie',
    maintainer_email='claude@nowyouknow.fr',
    description='Wired GoPro control for the AUV: recording, settings, and a camera-drop watchdog (Vbus power-cycle is delegated to the host revive watcher, not this node).',
    license='Apache-2.0',
    extras_require={'test': ['pytest']},
    entry_points={
        'console_scripts': [
            'gopro_manager = gopro_control.nodes.gopro_manager_node:main',
        ],
    },
)
