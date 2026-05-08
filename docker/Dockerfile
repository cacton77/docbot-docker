ARG BASE_IMAGE=osrf/ros:humble-desktop-full

# ── Optional onnxruntime-gpu source stage ──────────────────────────────────────
# PyPI has no aarch64 onnxruntime-gpu wheel. For Jetson, pass:
#   ONNXRUNTIME_STAGE=dustynv/onnxruntime:1.20.2-r36.4.0   (JetPack 6.2 / CUDA 12.6)
# via docker-compose build args. All other platforms use the no-op default,
# which leaves the CPU onnxruntime installed by requirements.txt in place.
ARG ONNXRUNTIME_STAGE=onnxruntime_noop
FROM alpine:3.19 AS onnxruntime_noop
RUN mkdir /ort

FROM ${ONNXRUNTIME_STAGE} AS onnxruntime_src
# Older dustynv images (e.g. r36.2.0) ship a .whl file; newer ones (e.g. 1.20.2-r36.4.0)
# install onnxruntime-gpu directly into site-packages with no wheel present.
# Handle both: prefer a wheel file, otherwise tar up the installed package.
RUN mkdir -p /ort && \
    WHL=$(find /opt /dist /packages /data /build /root -maxdepth 5 \
        -name "onnxruntime_gpu*.whl" -type f 2>/dev/null | head -1) && \
    if [ -n "$WHL" ]; then \
        cp "$WHL" /ort/; \
    else \
        SITE=$(python3 -c "import site; print(site.getsitepackages()[0])" 2>/dev/null || echo "") && \
        if [ -n "$SITE" ] && [ -d "$SITE/onnxruntime" ]; then \
            cd "$SITE" && \
            tar czf /ort/onnxruntime_pkg.tar.gz \
                $(ls -d onnxruntime onnxruntime_gpu*.dist-info 2>/dev/null); \
        fi; \
    fi || true

FROM ${BASE_IMAGE}

ARG ROS_DISTRO=humble
ENV ROS_DISTRO=${ROS_DISTRO}

# Platform type: "jetson" or "desktop" (controls conditional build steps)
ARG PLATFORM_TYPE=desktop
ENV PLATFORM_TYPE=${PLATFORM_TYPE}

# The dustynv base image sets jetson.webredirect.org as the default pip index in /etc/pip.conf,
# but that domain no longer resolves. Overwrite system-level config to reset to standard PyPI.
RUN printf '[global]\nindex-url = https://pypi.org/simple/\n' > /etc/pip.conf

# Refresh the ROS 2 apt signing key (key in base image may have expired)
RUN curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.asc | \
    gpg --batch --yes --dearmor -o /usr/share/keyrings/ros-archive-keyring.gpg

# Install base dependencies
COPY docker/packages.txt /tmp/packages.txt
RUN apt-get update && \
    grep -v '^#' /tmp/packages.txt | grep -v '^$' | xargs apt-get install -y && \
    rm -rf /var/lib/apt/lists/* /tmp/packages.txt

# Update colcon to latest version via pip to ensure --allow-overriding support
RUN python3 -m pip install --upgrade colcon-common-extensions colcon-core

# Set up colcon mixins (check if default already exists)
RUN colcon mixin list | grep -q "default" || \
    colcon mixin add default https://raw.githubusercontent.com/colcon/colcon-mixin-repository/master/index.yaml && \
    colcon mixin update default

# Update rosdep (it should already be initialized in the base image)
RUN rosdep update

# Create base workspace
RUN mkdir -p /workspaces/base_ws/src
WORKDIR /workspaces/base_ws/src

# Clone base repositories
RUN git clone -b $ROS_DISTRO https://github.com/micro-ROS/micro_ros_setup.git src/micro_ros_setup
RUN sudo apt-get update && rosdep update && \
    rosdep install --from-paths src --ignore-src -y

# Build the workspace without tests and with overrides
WORKDIR /workspaces/base_ws
RUN /bin/bash -c "source /opt/ros/${ROS_DISTRO}/setup.bash && \
    colcon build \
    --executor sequential \
    --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF"

# Create and build micro-ROS agent (separate steps for better error diagnosis)
RUN /bin/bash -c "source /opt/ros/${ROS_DISTRO}/setup.bash && \
    source install/setup.bash && \
    ros2 run micro_ros_setup create_agent_ws.sh"

RUN /bin/bash -c "source /opt/ros/${ROS_DISTRO}/setup.bash && \
    source install/setup.bash && \
    ros2 run micro_ros_setup build_agent.sh"

# Install overlay dependencies
# On Jetson, --force-overwrite is needed: the dustynv base image ships NVIDIA's
# custom opencv-dev that owns the same headers as Ubuntu's libopencv-*-dev
# (pulled in transitively by ROS packages). Only headers are overwritten;
# NVIDIA's CUDA-accelerated shared libraries are unaffected.
COPY docker/overlay_packages.txt /tmp/overlay_packages.txt
RUN apt-get update && \
    grep -v '^#' /tmp/overlay_packages.txt | grep -v '^$' | \
    xargs apt-get install -y \
    $([ "$PLATFORM_TYPE" = "jetson" ] && echo "-o Dpkg::Options::=--force-overwrite") && \
    rm -rf /var/lib/apt/lists/* /tmp/overlay_packages.txt

# On Jetson, build vision_opencv (cv_bridge) and gscam2 from source in an isolated workspace.
# The binary ros-humble-cv-bridge pulls in system libopencv-dev headers that conflict with
# NVIDIA's pre-installed CUDA OpenCV. Building after micro-ROS avoids interference with
# micro_ros_setup's internal rosdep calls.
RUN if [ "$PLATFORM_TYPE" = "jetson" ]; then \
        mkdir -p /tmp/gscam_ws/src && \
        git clone -b humble https://github.com/ros-perception/vision_opencv.git /tmp/gscam_ws/src/vision_opencv && \
        git clone -b main https://github.com/clydemcqueen/gscam2.git /tmp/gscam_ws/src/gscam2 && \
        /bin/bash -c "source /opt/ros/${ROS_DISTRO}/setup.bash && \
            apt-get update && \
            rosdep install --from-paths /tmp/gscam_ws/src --ignore-src -y \
                --skip-keys 'libopencv-dev libopencv-imgproc-dev libopencv-core-dev' && \
            rm -rf /var/lib/apt/lists/* && \
            cd /tmp/gscam_ws && \
            colcon build --merge-install --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF && \
            cp -r install/lib/* /opt/ros/${ROS_DISTRO}/lib/ && \
            cp -r install/share/* /opt/ros/${ROS_DISTRO}/share/ && \
            cp -r install/include/* /opt/ros/${ROS_DISTRO}/include/" && \
        rm -rf /tmp/gscam_ws; \
    fi

# Install PyTorch for non-Jetson platforms before requirements.txt so that
# ultralytics (in requirements.txt) sees torch already present and won't upgrade it.
RUN if [ "$PLATFORM_TYPE" != "jetson" ]; then \
        pip3 install --no-cache-dir --index-url https://pypi.org/simple/ torch torchvision; \
    fi

COPY docker/requirements.txt /tmp/requirements.txt
# Remove distutils-managed system packages that pip cannot cleanly uninstall
# but that pip's dependency resolver will try to upgrade (e.g. sympy 1.9 → 1.14).
# Removing them here lets pip own those packages going forward.
RUN apt-get remove -y python3-sympy 2>/dev/null || true
RUN python3 -m pip install --index-url https://pypi.org/simple/ -r /tmp/requirements.txt && rm /tmp/requirements.txt

# For Jetson: install the NVIDIA-optimized PyTorch wheel AFTER requirements.txt so it
# overrides the generic cu1xx wheel that ultralytics pulls in from standard PyPI.
# --no-deps avoids pip re-resolving transitive deps; the Jetson wheel is API-compatible
# with torch 2.5.x so everything installed by requirements.txt continues to work.
# torchvision from PyPI is installed separately as it is not on the NVIDIA redist server.
# nvidia-cusparselt-cu12 provides libcusparseLt.so.0 which the Jetson torch wheel links
# against but which is not included in the dustynv base image's CUDA toolkit. It is
# registered with ldconfig so the dynamic linker can find it at runtime.
RUN if [ "$PLATFORM_TYPE" = "jetson" ]; then \
        TORCH_WHL="torch-2.5.0a0+872d972e41.nv24.08.17622132-cp310-cp310-linux_aarch64.whl" && \
        curl -L -o "/tmp/${TORCH_WHL}" \
            "https://developer.download.nvidia.com/compute/redist/jp/v61/pytorch/${TORCH_WHL}" && \
        pip3 install --no-cache-dir --force-reinstall --no-deps "/tmp/${TORCH_WHL}" && \
        rm "/tmp/${TORCH_WHL}" && \
        pip3 install --no-cache-dir --index-url https://pypi.org/simple/ \
            nvidia-cusparselt-cu12 && \
        CUSPARSELT_LIB=$(find /usr/local/lib/python3.10 -name "libcusparseLt.so*" -type f 2>/dev/null | head -1 | xargs dirname) && \
        echo "$CUSPARSELT_LIB" > /etc/ld.so.conf.d/nvidia-cusparselt.conf && \
        ldconfig && \
        git clone --depth 1 --branch v0.20.1 \
            https://github.com/pytorch/vision.git /tmp/torchvision && \
        cd /tmp/torchvision && \
        FORCE_CUDA=0 pip3 install --no-build-isolation --no-deps . && \
        cd / && rm -rf /tmp/torchvision; \
    fi

# Install onnxruntime-gpu wheel if one was extracted from ONNXRUNTIME_STAGE.
# Replaces the CPU onnxruntime installed by insightface via requirements.txt.
# On non-Jetson platforms /tmp/ort/ is empty so this RUN is a no-op.
COPY --from=onnxruntime_src /ort/ /tmp/ort/
RUN WHL=$(ls /tmp/ort/onnxruntime_gpu*.whl 2>/dev/null | head -1) && \
    if [ -n "$WHL" ]; then \
        pip3 uninstall -y onnxruntime 2>/dev/null || true && \
        pip3 install --no-cache-dir --force-reinstall --no-deps \
            --index-url https://pypi.org/simple/ "$WHL"; \
    elif [ -f /tmp/ort/onnxruntime_pkg.tar.gz ]; then \
        pip3 uninstall -y onnxruntime 2>/dev/null || true && \
        SITE=$(python3 -c "import site; print(site.getsitepackages()[0])") && \
        tar xzf /tmp/ort/onnxruntime_pkg.tar.gz -C "$SITE"; \
    fi && rm -rf /tmp/ort

# Update rosdep (it should already be initialized in the base image)
RUN rosdep update

# Install CycloneDDS
RUN apt-get update && apt-get install -y --no-install-recommends \
    ros-${ROS_DISTRO}-rmw-cyclonedds-cpp

# Clone shared workspace repositories
RUN mkdir -p /workspaces/shared_ws/src
RUN mkdir -p /data
RUN mkdir -p /models

# Create new user and add to video group
RUN apt-get update && apt-get install -y sudo && rm -rf /var/lib/apt/lists/*

ARG USERNAME=ros_user
ARG UID=1000
ARG GID=${UID}
ARG VIDEO_GID=44
ARG DIALOUT_GID=20

# Create the video group with the same GID as the host
RUN getent group video || groupadd --gid ${VIDEO_GID} video
RUN getent group dialout || groupadd --gid ${DIALOUT_GID} dialout

RUN groupadd --gid ${GID} ${USERNAME} \
    && useradd --uid ${UID} --gid ${GID} --create-home ${USERNAME} \
    && usermod -a -G video ${USERNAME} \
    && usermod -a -G dialout ${USERNAME} \
    # Give sudo permissions without password 
    && echo ${USERNAME} ALL=\(root\) NOPASSWD:ALL > /etc/sudoers.d/${USERNAME} \
    && chmod 0440 /etc/sudoers.d/${USERNAME} \
    && mkdir -p /home/${USERNAME} \
    && chown -R ${UID}:${GID} /home/${USERNAME}

# Set the ownership of the shared workspace to the new user
RUN chown -R ${UID}:${UID} /workspaces/shared_ws/
RUN chown -R ${UID}:${UID} /data/
RUN chown -R ${UID}:${UID} /models/

# Set the user and source entrypoint in the user's .bashrc file
COPY ./docker/entrypoint.sh /
RUN chmod +x /entrypoint.sh
RUN echo "source /entrypoint.sh" >> /home/${USERNAME}/.bashrc

USER ${USERNAME}
ENTRYPOINT [ "/entrypoint.sh" ]