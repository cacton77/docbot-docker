import os
from glob import glob
from setuptools import find_packages, setup

package_name = 'doc_control'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', [f'resource/{package_name}']),
        (f'share/{package_name}', ['package.xml']),
        (os.path.join('share', package_name, 'launch'), glob('launch/*.launch.py')),
        (os.path.join('share', package_name, 'config'),  glob('config/*.yaml')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='DocBOT',
    maintainer_email='todo@todo.com',
    description='Servo and motor control nodes for DocBOT.',
    license='MIT',
    entry_points={
        'console_scripts': [
            'servo_node = doc_control.servo_node:main',
        ],
    },
)
