import os
from glob import glob
from setuptools import find_packages, setup

package_name = 'doc_vision'

weights = glob('weights/*.pt') + glob('weights/*.pt2')
data_files = [
    ('share/ament_index/resource_index/packages', [f'resource/{package_name}']),
    (f'share/{package_name}', ['package.xml']),
    (os.path.join('share', package_name, 'launch'), glob('launch/*.launch.py')),
    (os.path.join('share', package_name, 'config'), glob('config/*.yaml')),
]
if weights:
    data_files.append((os.path.join('share', package_name, 'weights'), weights))

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=data_files,
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='DocBOT',
    maintainer_email='todo@todo.com',
    description='Face detection and vision pipeline for DocBOT.',
    license='MIT',
    entry_points={
        'console_scripts': [
            'face_detection_node = doc_vision.face_detection_node:main',
            'insightface_node    = doc_vision.insightface_node:main',
        ],
    },
)
