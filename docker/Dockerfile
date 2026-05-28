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

# Remove stale apt sources baked into the base image and purge cached lists so
# apt-get update fetches fresh metadata (avoids "changed Origin/Label" hard errors).
RUN sed -i '/isaac\.download\.nvidia\.com\/isaac-ros\/ubuntu/d' /etc/apt/sources.list 2>/dev/null || true && \
    grep -rl 'isaac\.download\.nvidia\.com/isaac-ros/ubuntu' \
        /etc/apt/sources.list.d/ 2>/dev/null | xargs rm -f || true && \
    grep -rl 'apt\.kitware\.com' \
        /etc/apt/sources.list.d/ 2>/dev/null | xargs rm -f || true && \
    grep -rl 'dl\.yarnpkg\.com' \
        /etc/apt/sources.list.d/ 2>/dev/null | xargs rm -f || true && \
    find /usr/share/keyrings/ \( -name "kitware*.gpg" -o -name "kitware*.asc" \
        -o -name "yarn*.gpg" -o -name "yarn*.asc" \) -delete || true && \
    rm -rf /var/lib/apt/lists/*

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

# Add NVIDIA Isaac ROS apt repository (Jetson only — packages unavailable on desktop)
RUN if [ "$PLATFORM_TYPE" = "jetson" ]; then \
        curl -sSL https://isaac.download.nvidia.com/isaac-ros/repos.key | \
            gpg --batch --yes --dearmor \
                -o /usr/share/keyrings/isaac-ros-archive-keyring.gpg && \
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/isaac-ros-archive-keyring.gpg] https://isaac.download.nvidia.com/isaac-ros/release-3 $(lsb_release -cs) release" \
            > /etc/apt/sources.list.d/isaac-ros.list; \
    fi

# Install common overlay dependencies (all platforms)
COPY docker/overlay_packages.txt /tmp/overlay_packages.txt
RUN apt-get update && \
    grep -v '^#' /tmp/overlay_packages.txt | grep -v '^$' | \
    xargs apt-get install -y && \
    rm -rf /var/lib/apt/lists/* /tmp/overlay_packages.txt

# Install ALSA config that routes default PCM/CTL through PulseAudio. The
# pulse client libs and ALSA→pulse plugin come from overlay_packages.txt
# above; this config tells ALSA to use them. Combined with the pulse socket
# mount in docker-compose, "default" inside the container resolves to the
# host's selected input/output instead of ALSA card 0 (typically HDMI).
COPY docker/asound.conf /etc/asound.conf

# Install Jetson-specific Isaac ROS packages (uses the repo added above)
COPY docker/overlay_packages_jetson.txt /tmp/overlay_packages_jetson.txt
RUN if [ "$PLATFORM_TYPE" = "jetson" ]; then \
        apt-get update && \
        grep -v '^#' /tmp/overlay_packages_jetson.txt | grep -v '^$' | \
        xargs apt-get install -y && \
        rm -rf /var/lib/apt/lists/*; \
    fi && rm -f /tmp/overlay_packages_jetson.txt

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

# For Jetson: install the NVIDIA-optimized PyTorch wheel after requirements.txt so it
# takes precedence over any CPU torch pulled in by ultralytics/insightface.
# torchvision is installed from the PyPI manylinux2014_aarch64 pre-built wheel (cp310)
# instead of being compiled from source — source builds OOM-crash the Jetson.
# The PyPI wheel is a CPU-only build (same as FORCE_CUDA=0 from source) and is
# ABI-compatible with the NVIDIA 2.5.0 torch since both use the same libtorch_cpu ABI.
# nvidia-cusparselt-cu12 provides libcusparseLt.so.0 which the torch wheel links against.
RUN if [ "$PLATFORM_TYPE" = "jetson" ]; then \
        TORCH_WHL="torch-2.5.0a0+872d972e41.nv24.08.17622132-cp310-cp310-linux_aarch64.whl" && \
        curl -L -o "/tmp/${TORCH_WHL}" \
            "https://developer.download.nvidia.com/compute/redist/jp/v61/pytorch/${TORCH_WHL}" && \
        pip3 install --no-cache-dir --force-reinstall --no-deps "/tmp/${TORCH_WHL}" && \
        rm "/tmp/${TORCH_WHL}" && \
        pip3 install --no-cache-dir --index-url https://pypi.org/simple/ \
            nvidia-cusparselt-cu12 && \
        CUSPARSELT_LIB=$(find /usr/local/lib/python3.10 -name "libcusparseLt.so*" -type f 2>/dev/null | head -1 | xargs dirname) && \
        if [ -n "$CUSPARSELT_LIB" ]; then \
            echo "$CUSPARSELT_LIB" > /etc/ld.so.conf.d/nvidia-cusparselt.conf && ldconfig; \
        fi && \
        pip3 install --no-cache-dir --no-deps \
            --index-url https://pypi.org/simple/ \
            "torchvision==0.20.1"; \
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

# Download Piper TTS voice (en_US-lessac-medium, ~60MB). Baked into the image
# under /opt/piper-voices/ rather than /models/ because /models is bind-mounted
# from the host and would shadow image-time downloads at runtime.
RUN mkdir -p /opt/piper-voices && \
    PIPER_VOICE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium" && \
    curl -fsSL "${PIPER_VOICE_URL}/en_US-lessac-medium.onnx" \
        -o /opt/piper-voices/en_US-lessac-medium.onnx && \
    curl -fsSL "${PIPER_VOICE_URL}/en_US-lessac-medium.onnx.json" \
        -o /opt/piper-voices/en_US-lessac-medium.onnx.json

# Download faster-whisper small.en (~480MB) into the image. CPU+int8 is the
# safest combination during build (no CUDA in the build sandbox); runtime
# can pick a different device/compute_type from the same on-disk cache.
RUN mkdir -p /opt/whisper-models && \
    python3 -c "from faster_whisper import WhisperModel; WhisperModel('small.en', device='cpu', compute_type='int8', download_root='/opt/whisper-models')"

# Build gscam2 from source (Jetson only — ros-humble-gscam2 binary not available for arm64/Jammy)
RUN mkdir -p /workspaces/gscam2_ws/src && \
    if [ "$PLATFORM_TYPE" = "jetson" ]; then \
        apt-get update && \
        apt-get install -y --no-install-recommends \
            libgstreamer1.0-dev \
            libgstreamer-plugins-base1.0-dev && \
        rm -rf /var/lib/apt/lists/* && \
        git clone --depth 1 https://github.com/clydemcqueen/gscam2.git \
            /workspaces/gscam2_ws/src/gscam2 && \
        /bin/bash -c "source /opt/ros/${ROS_DISTRO}/setup.bash && \
            cd /workspaces/gscam2_ws && \
            colcon build \
                --executor sequential \
                --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF"; \
    fi

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

RUN if getent group ${GID} > /dev/null 2>&1; then \
        groupmod -n ${USERNAME} "$(getent group ${GID} | cut -d: -f1)"; \
    else \
        groupadd --gid ${GID} ${USERNAME}; \
    fi && \
    if getent passwd ${UID} > /dev/null 2>&1; then \
        OLD="$(getent passwd ${UID} | cut -d: -f1)" && \
        usermod -l ${USERNAME} -d /home/${USERNAME} -m "$OLD"; \
    else \
        useradd --uid ${UID} --gid ${GID} --create-home ${USERNAME}; \
    fi && \
    usermod -a -G video ${USERNAME} && \
    usermod -a -G dialout ${USERNAME} && \
    echo "${USERNAME} ALL=(root) NOPASSWD:ALL" > /etc/sudoers.d/${USERNAME} && \
    chmod 0440 /etc/sudoers.d/${USERNAME} && \
    mkdir -p /home/${USERNAME} && \
    chown -R ${UID}:${GID} /home/${USERNAME}

# Set the ownership of the shared workspace to the new user
RUN chown -R ${UID}:${UID} /workspaces/shared_ws/
RUN chown -R ${UID}:${UID} /workspaces/gscam2_ws/
RUN chown -R ${UID}:${UID} /data/
RUN chown -R ${UID}:${UID} /models/

# Set the user and source entrypoint in the user's .bashrc file
COPY ./docker/entrypoint.sh /
RUN chmod +x /entrypoint.sh
RUN echo "source /entrypoint.sh" >> /home/${USERNAME}/.bashrc

USER ${USERNAME}
ENTRYPOINT [ "/entrypoint.sh" ]