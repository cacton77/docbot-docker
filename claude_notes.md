# Claude Notes

## InsightFace GPU Acceleration (2026-05-08)

### Problem
The `insightface_node` was running on `CPUExecutionProvider` despite the Jetson Orin Nano having a CUDA-capable GPU.

### Root Cause
Two related issues:

1. **Version mismatch**: `docker-compose.yaml` specified `ONNXRUNTIME_STAGE: dustynv/onnxruntime:r36.2.0` (JetPack 6.0, CUDA 12.2), but the base image was `dustynv/ros:humble-desktop-l4t-r36.4.0` (JetPack 6.2, CUDA 12.6).

2. **ORT 1.16.3 device discovery bug**: The wheel from `r36.2.0` includes `CUDAExecutionProvider` as a compiled-in provider, but its `device_discovery.cc` looks for `/sys/class/drm/card1/device/vendor` — a PCI sysfs file that exists on discrete GPUs but not on the Jetson Orin's integrated SoC GPU (`nvgpu`). This caused ORT to report zero available CUDA devices at runtime, so `CUDAExecutionProvider` never appeared in `get_available_providers()`. Fixed in ORT 1.17+.

3. **Wheel format change**: The newer `dustynv/onnxruntime:1.20.2-r36.4.0` image installs ORT directly into site-packages (no `.whl` file), whereas the old `r36.2.0` image shipped a wheel file. The Dockerfile's find-and-copy approach only handled the old format.

### Fix
- Updated `ONNXRUNTIME_STAGE` in `docker-compose.yaml` to `dustynv/onnxruntime:1.20.2-r36.4.0`.
- Updated the `onnxruntime_src` build stage in `docker/Dockerfile` to handle both formats: wheel file (old) and installed site-packages tarball (new).
- Added explicit `pip3 uninstall onnxruntime` before installing the GPU package to prevent the CPU and GPU packages from coexisting in site-packages.

**Result**: `insightface_node` now loads with `providers: ['CUDAExecutionProvider', 'CPUExecutionProvider']`.

---

## Camera FPS Fix (2026-05-08)

### Problem
`/left/image_raw` was publishing at ~25fps instead of the configured 60fps. rPPG requires at least 60fps.

### Root Cause
The GStreamer pipeline in `src/doc_cameras/launch/stereo_camera.launch.py` used a two-step CPU conversion:

```
nvarguscamerasrc → NV12 (NVMM) → nvvidconv → BGRx (system RAM) → videoconvert → RGB
```

The CPU `videoconvert` step (BGRx → RGB) was processing 1280×720×4 bytes at 60fps ≈ 221 MB/s per camera, causing each `gscam_main` process to consume 56–62% CPU and the system load average to reach ~9. This CPU saturation capped the achievable frame rate at ~25fps.

### Attempted Fix (reverted)
Changed the pipeline to have `nvvidconv` output `RGB` directly, bypassing the CPU `videoconvert` step. This failed with:

```
[FATAL] Cannot link outelement("nvvconv0") -> sink
```

`nvvidconv` on GStreamer 1.20.3 (the version installed in this container) does not support `RGB` as an output format. Supported system-memory output formats include `BGRx`, `RGBA`, `UYVY`, `I420`, etc. — but not packed `RGB`. The pipeline was reverted to the original BGRx intermediate.

### Status
The camera FPS issue (25fps vs target 60fps) is not yet resolved. The `videoconvert` CPU step is not the bottleneck — the high gscam CPU usage (~56% per camera) likely comes from ROS2 message serialization of 2.76 MB frames. The correct long-term fix is the Isaac ROS migration documented below.

---

## Isaac ROS Migration Plan (2026-05-08)

### Motivation
The fundamental issue is that the current stack forces every camera frame through multiple CPU copies:

```
Sensor → NVMM → nvvidconv → BGRx (RAM) → videoconvert → RGB (RAM) → ROS2 serialize → DDS → deserialize → cv_bridge
```

NVIDIA Isaac ROS with NITROS (NVIDIA Isaac Transport for ROS) eliminates these copies by keeping frames in GPU/NVMM memory across nodes using zero-copy type negotiation. The CPU never touches pixel data until a non-NITROS subscriber explicitly pulls a frame.

---

### Current Architecture

| Component | Package | Notes |
|-----------|---------|-------|
| Base image | `dustynv/ros:humble-desktop-l4t-r36.4.0` | |
| Camera driver | `gscam2` | CPU copy per frame |
| Rectification | `image_proc/rectify_node` × 2 | CPU, ~12% each |
| Disparity | `stereo_image_proc/disparity_node` | CPU |
| Point cloud | `stereo_image_proc/point_cloud_node` | CPU |
| Face detection | `insightface_node` (ORT 1.20.2) | CUDA via onnxruntime-gpu |
| rPPG | `rppg_node` | subscribes to `/left/image_raw` |
| ORT GPU | `dustynv/onnxruntime:1.20.2-r36.4.0` | layered via multi-stage build |

**Entry point**: `src/doc_cameras/launch/full_pipeline.launch.py`
→ `stereo_camera.launch.py` (gscam2 nodes)
→ `stereo_proc.launch.py` (rectify + disparity + point cloud)

---

### Target Architecture

| Component | Package | Notes |
|-----------|---------|-------|
| Base image | `nvcr.io/nvidia/isaac/ros:aarch64-ros2_humble_<hash>` | see Phase 1 |
| Camera driver | `isaac_ros_argus_camera/ArgusStereoNode` | stays in NVMM |
| Rectification | `isaac_ros_image_proc/RectifyNode` × 2 | GPU, NITROS zero-copy |
| Disparity | `isaac_ros_stereo_image_proc/DisparityNode` | GPU, NITROS |
| Point cloud | `isaac_ros_stereo_image_proc/PointCloudNode` | GPU, NITROS |
| Face detection | unchanged | ORT still layered via multi-stage build |
| rPPG | unchanged | same topic `/left/image_raw` |
| ORT GPU | `dustynv/onnxruntime:1.20.2-r36.4.0` | same multi-stage layer as today |

All NITROS nodes must run inside a single `component_container_mt` process — zero-copy only applies within one process. Non-NITROS subscribers (insightface, rPPG) receive standard `sensor_msgs/Image` automatically converted from NITROS types.

---

### Phase 1 — Identify the Correct Base Image Tag

The Isaac ROS base image uses content-addressed hashes, not semantic version tags. The correct hash for JetPack 6.x / L4T r36.4.0 / ROS2 Humble must be looked up from the Isaac ROS release page before starting.

**TODO**: Check https://nvidia-isaac-ros.github.io/getting_started/dev_env_setup.html for the Isaac ROS 4.4.0 (current stable) image hash that corresponds to L4T r36.4.0.

Candidate tags seen in the wild for JetPack 6.x Humble:
- `nvcr.io/nvidia/isaac/ros:aarch64-ros2_humble_b7e1ed6c02a6fa3c1c7392479291c035`
- `nvcr.io/nvidia/isaac/ros:aarch64-ros2_humble_77e6a678c2058abf96bedcb8f7dd4330`

**Verification step**: After pulling the image, run:
```bash
docker run --rm nvcr.io/nvidia/isaac/ros:aarch64-ros2_humble_<hash> \
  cat /etc/nv_tegra_release
```
Confirm the L4T version matches r36.4.x before proceeding.

---

### Phase 2 — Docker / Build Changes

**`docker-compose.yaml`** — change the jetson service build args:
```yaml
# Before
BASE_IMAGE: dustynv/ros:humble-desktop-l4t-r36.4.0

# After
BASE_IMAGE: nvcr.io/nvidia/isaac/ros:aarch64-ros2_humble_<hash>
```

`ONNXRUNTIME_STAGE` stays as-is (`dustynv/onnxruntime:1.20.2-r36.4.0`) — the multi-stage ORT layer in the Dockerfile is image-agnostic and continues to work.

**`docker/Dockerfile`** — changes needed:

1. **Remove the gscam2 source build** (the Jetson-specific step that clones and builds `gscam2` from source). `isaac_ros_argus_camera` replaces it entirely.

2. **Add Isaac ROS apt packages** to `docker/overlay_packages.txt`:
   ```
   ros-humble-isaac-ros-argus-camera
   ros-humble-isaac-ros-image-proc
   ros-humble-isaac-ros-stereo-image-proc
   ros-humble-isaac-ros-nitros
   ```
   These are available from the Isaac ROS apt repository, which is pre-configured in the base image.

3. **PyTorch for Jetson** — the Isaac ROS base image includes CUDA but not PyTorch. The existing Dockerfile block that installs the NVIDIA Jetson PyTorch wheel should continue to work unchanged.

4. **Verify micro_ros_setup** — micro_ros clones and builds against the ROS2 distro in the base image. It should be unaffected, but confirm the base image has the expected ROS2 Humble environment at `/opt/ros/humble`.

---

### Phase 3 — Camera Driver Migration

**File**: `src/doc_cameras/launch/stereo_camera.launch.py`

Replace the `_jetson_node()` function and its GStreamer pipeline string with an `ArgusStereoNode` component loaded into a `component_container_mt`.

**Key changes**:
- `ArgusStereoNode` is a composable node, not a standalone executable. It must be loaded into a `ComposableNodeContainer` (not a `Node`).
- Topic names are identical: `/left/image_raw`, `/right/image_raw`, `/left/camera_info`, `/right/camera_info` — no downstream remapping needed.
- `sensor_mode` maps to `camera_info` calibration; the YAML calibration files (`left_camera.yaml`, `right_camera.yaml`) are passed via `left_camera_info_url` / `right_camera_info_url`.
- Frame rate is controlled by the sensor mode; no GStreamer `framerate=60/1` cap needed.

**Sketch of new launch structure**:
```python
from launch_ros.actions import ComposableNodeContainer
from launch_ros.descriptions import ComposableNode

container = ComposableNodeContainer(
    name='isaac_camera_container',
    namespace='',
    package='rclcpp_components',
    executable='component_container_mt',
    composable_node_descriptions=[
        ComposableNode(
            package='isaac_ros_argus_camera',
            plugin='nvidia::isaac_ros::argus::ArgusStereoNode',
            name='argus_stereo',
            parameters=[{
                'left_camera_info_url':  left_url,
                'right_camera_info_url': right_url,
                'module_id': 0,
            }],
        ),
    ],
)
```

---

### Phase 4 — Image Pipeline Migration

**File**: `src/doc_cameras/launch/stereo_proc.launch.py`

Replace `image_proc/rectify_node` (×2), `stereo_image_proc/disparity_node`, and `stereo_image_proc/point_cloud_node` with their Isaac ROS equivalents, all loaded into the **same** `component_container_mt` as the camera node to enable zero-copy NITROS transport.

**Node mapping**:

| Current | Isaac ROS replacement |
|---------|-----------------------|
| `image_proc/rectify_node` | `isaac_ros_image_proc/RectifyNode` (`nvidia::isaac_ros::image_proc::RectifyNode`) |
| `stereo_image_proc/disparity_node` | `isaac_ros_stereo_image_proc/DisparityNode` |
| `stereo_image_proc/point_cloud_node` | `isaac_ros_stereo_image_proc/PointCloudNode` |

**Important**: All nodes must be in the same container as the camera node for NITROS zero-copy to apply. `full_pipeline.launch.py` should be refactored to instantiate one shared container and pass it to both the camera and proc launch includes (or merged into a single launch file).

**Remapping**: Topic names (`/left/image_rect`, `/stereo/disparity`, `/stereo/points2`) should remain the same to avoid changes to downstream subscribers.

**Constraint**: `RectifyNode` requires even-numbered image dimensions. 1280×720 satisfies this.

---

### Phase 5 — InsightFace / ORT Compatibility

`insightface_node` uses `onnxruntime-gpu` 1.20.2 installed via the existing multi-stage Dockerfile build (from `dustynv/onnxruntime:1.20.2-r36.4.0`). This layer is independent of the base image and should continue to work in the Isaac ROS container because:
- The Isaac ROS base image includes the same CUDA 12.6 / L4T r36.4.0 environment.
- The `onnxruntime-gpu` 1.20.2 wheel was built for r36.4.0 and links against the same CUDA libraries.

**Risk**: The Isaac ROS image may already install a version of `onnxruntime` (CPU or GPU) that conflicts. Check with:
```bash
docker run --rm nvcr.io/nvidia/isaac/ros:aarch64-ros2_humble_<hash> \
  pip3 show onnxruntime onnxruntime-gpu 2>/dev/null
```
If a conflicting version is found, add it to the `pip3 uninstall` list in the Dockerfile's ORT install step.

**No changes needed** to `insightface_node.py` — it already correctly checks `get_available_providers()` and selects `CUDAExecutionProvider` when available.

---

### Phase 6 — Testing Sequence

1. **Image tag verification**: confirm L4T version matches r36.4.0 inside the pulled image.
2. **Build smoke test**: `docker compose --profile jetson build jetson` — confirm no apt errors for Isaac ROS packages.
3. **Camera topics**: after launch, confirm `/left/image_raw` and `/right/image_raw` are live with `ros2 topic hz`.
4. **FPS target**: `ros2 topic hz /left/image_raw` should show ~60fps.
5. **Rectification**: confirm `/left/image_rect` and `/right/image_rect` are published.
6. **Disparity/point cloud**: confirm `/stereo/disparity` and `/stereo/points2` are published.
7. **InsightFace**: confirm `providers: ['CUDAExecutionProvider', ...]` in logs.
8. **rPPG**: confirm `/rppg/bpm` publishes after face is in frame.

---

### Open Questions

- Does the Isaac ROS base image pre-install any onnxruntime version that would conflict?
- Does `micro_ros_setup` build cleanly against the Isaac ROS base image's ROS2 Humble install?
- Does `argusd` (the Argus daemon) need to be started separately, or is it managed by the container runtime on r36.4.0?

---

## Isaac ROS Migration — Implementation Progress (2026-05-08)

### Base Image Identified

Research into `isaac_ros_common` release-3.2 confirmed the correct base image for our hardware:

```
nvcr.io/nvidia/isaac/ros:aarch64-ros2_humble_b7e1ed6c02a6fa3c1c7392479291c035
```

This image is built on `nvcr.io/nvidia/12.6.11-devel:12.6.11-devel-aarch64-ubuntu22.04` (CUDA 12.6 / Ubuntu 22.04), **not** an L4T base. For JetPack 6+, NVIDIA shifted to standard Ubuntu 22.04 containers — L4T dependencies (Argus daemon, GPU drivers) are provided by the host OS via device mounts, which our `docker-compose.yaml` already handles (`/tmp/argus_socket`, `/dev` bind mounts).

The image bundles: CUDA 12.6, TensorRT 10.3, PyTorch `2.5.0a0+872d972e41.nv24.08` (JP6.1 Jetson wheel), VPI 3.2.4, CV-CUDA 0.5.0, and ROS2 Humble. It targets r36.4 Jetson packages; our board runs r36.5.0, which is forward-compatible.

### Files Changed

| File | Change |
|------|--------|
| `docker-compose.yaml` | `BASE_IMAGE` → confirmed Isaac ROS hash |
| `docker/overlay_packages.txt` | Added `ros-humble-isaac-ros-{argus-camera,image-proc,stereo-image-proc,nitros}`; kept standard packages as non-Jetson fallback |
| `docker/Dockerfile` | Removed gscam2 + vision_opencv Jetson source build; removed `--force-overwrite` (no OpenCV conflict on new base); added skip-if-present guard for PyTorch wheel download (already in base image) |
| `src/doc_cameras/launch/stereo_camera.launch.py` | Replaced `_jetson_node()` (gscam2 `Node`) with `_jetson_container()` (`ArgusStereoNode` in `ComposableNodeContainer`); non-Jetson paths unchanged |
| `src/doc_cameras/launch/stereo_proc.launch.py` | Jetson: `RectifyNode` (`nvidia::isaac_ros::image_proc::RectifyNode`) in GPU container; non-Jetson: CPU `rectify_node` fallback; disparity/point_cloud remapped from `image_rect_color` → `image_rect` |

### Architecture After Migration

```
Sensor (IMX219)
  └─ ArgusStereoNode [component_container_mt] (NVMM, no CPU copy)
       ├─ /left/image_raw  ──► RectifyNode [image_proc_container] ──► /left/image_rect
       └─ /right/image_raw ──► RectifyNode                        ──► /right/image_rect
                                    │
                          stereo_image_proc (CPU)
                          ├─ /stereo/disparity
                          └─ /stereo/points2

/left/image_raw ──────────────────────────────────► insightface_node (CUDA via ORT)
/left/image_raw ──────────────────────────────────► rppg_node
```

### Critical Findings From Base Image Inspection

Running the pulled Isaac ROS base image revealed several assumptions in the plan were wrong:

- **OS**: Ubuntu 20.04 (focal), not Ubuntu 22.04 as initially assumed.
- **Isaac ROS apt repo**: NOT pre-configured. `ros-humble-isaac-ros-*` packages are unavailable until the repo is added.
- **PyTorch**: NOT pre-installed. The base image does not include the NVIDIA Jetson PyTorch wheel.
- **ORT**: NOT pre-installed. No `onnxruntime` of any kind in the base image.

### Dockerfile Fixes Applied (2026-05-08)

Three bugs introduced during the initial migration were fixed:

**1. Isaac ROS apt repo missing**

Added a conditional `RUN` block before the overlay package install that fetches and registers the NVIDIA Isaac ROS apt repo key and sources list entry. Only runs when `PLATFORM_TYPE=jetson`. Uses `$(lsb_release -cs)` to resolve the distro codename at build time so it works regardless of whether the base image turns out to be focal or jammy.

```dockerfile
RUN if [ "$PLATFORM_TYPE" = "jetson" ]; then \
        curl -sSL https://isaac.download.nvidia.com/isaac-ros/repos.key | \
            gpg --batch --yes --dearmor \
                -o /usr/share/keyrings/isaac-ros-archive-keyring.gpg && \
        echo "deb [arch=...] https://isaac.download.nvidia.com/isaac-ros/release-3 $(lsb_release -cs) release" \
            > /etc/apt/sources.list.d/isaac-ros.list; \
    fi
```

**2. Isaac ROS packages in shared overlay_packages.txt broke desktop builds**

Desktop apt sources don't carry `ros-humble-isaac-ros-*` packages, so including them in `overlay_packages.txt` caused desktop builds to fail at the `apt-get install` step.

Fix: split into two files:
- `docker/overlay_packages.txt` — common packages (all platforms)
- `docker/overlay_packages_jetson.txt` — Isaac ROS packages (Jetson only, installed after repo setup)

**3. Incorrect PyTorch skip-guard**

The Dockerfile had a guard that skipped downloading the PyTorch wheel if `import torch` with the expected version hash succeeded. This was based on the (wrong) assumption that the Isaac ROS base image pre-installs PyTorch. Since it does not, the guard would always fall through to the download branch anyway — but the guard logic was confusing and the comment was misleading. Removed the guard entirely; the PyTorch wheel is now always downloaded and installed unconditionally on Jetson builds.

---

## Stereo Rectification Performance Benchmarks (2026-05-13)

### Setup
- Camera: two IMX219 CSI cameras on Jetson Orin NX via gscam2 (`nvarguscamerasrc`)
- Raw image rate: **~30 fps** at 1280×720 (sensor_mode=4; despite framerate=60/1 in pipeline, sensor or DMA caps at 30 fps on this hardware)
- gscam2 publishes standard `sensor_msgs/Image` (rgb8, CPU memory) — no NITROS output

### Results

| Approach | `/left/image_rect` fps | Notes |
|---|---|---|
| Isaac ROS `RectifyNode` in `component_container_mt` (GPU) | **~14–15 fps** | Best result. CUDA rectification; CPU→GPU→CPU DMA per frame |
| CPU `image_proc::RectifyNode` as standalone processes | ~7–10 fps | Worse. Full DDS serialization of 2.76 MB frames between separate processes |
| CPU `image_proc::RectifyNode` as composable nodes w/ IPC | ~6 fps | Worst. IPC comms within container + approximate_sync may cause frame drops |

### Why GPU wins despite DMA overhead
The Isaac ROS `RectifyNode` path is best because:
- It runs inside a `component_container_mt` — the CUDA kernel executes in a dedicated thread, parallelizing left and right camera rectification
- DMA cost (CPU→GPU) is amortized across both cameras sharing one container
- CPU paths require full DDS round-trips between separate processes for 2.76 MB/frame images, which saturates the DDS transport layer

### Root cause of ~50% throughput (30 fps raw → 15 fps rect)
Without a NITROS-native camera source, every frame crosses the CPU↔GPU boundary twice (in and out of RectifyNode). The limit is DMA bandwidth + CUDA stream serialization for two concurrent cameras. This is a known limitation when gscam2 is used instead of `ArgusStereoNode`.

### Path to full 30+ fps rectified
End-to-end NITROS: use `isaac_ros_argus_camera/ArgusStereoNode` (keeps frames in NVMM from sensor to rectification, zero CPU copies). See "Isaac ROS Migration Plan" section above. The stereo pipeline already has the correct architecture; only the camera source needs to change.

### Current state
`stereo_proc.launch.py` uses Isaac ROS GPU `RectifyNode` on Jetson (reverted to this after CPU experiments performed worse). Disparity and point cloud remain CPU-based (`stereo_image_proc` standalone nodes).

---

### Remaining Before Build

1. Run `./install.sh --jetson` to rebuild with all fixes applied.
2. Watch for apt errors during the Isaac ROS package install — if the repo URL returns 404 for the `$(lsb_release -cs)` value (e.g. "focal" packages not present), the distro name in the sources entry may need to be hardcoded to "jammy".
3. Confirm `ros2 topic hz /left/image_raw` shows ~60fps.
4. Confirm `insightface_node` logs show `providers: ['CUDAExecutionProvider', ...]`.
