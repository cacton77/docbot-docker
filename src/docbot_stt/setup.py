from glob import glob
import os

from setuptools import find_packages, setup

package_name = 'docbot_stt'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'),
            glob('launch/*.launch.py')),
        (os.path.join('share', package_name, 'config'),
            glob('config/*.yaml')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Colin Acton',
    maintainer_email='cacton@uw.edu',
    description='Transcription action server using faster-whisper. Records '
                'a fixed-duration window of /audio on each goal and returns '
                'the transcript.',
    license='MIT',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'transcribe_node = docbot_stt.transcribe_node:main',
        ],
    },
)
