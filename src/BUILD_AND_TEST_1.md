# Building and Testing: First Two Packages

This is the first slice of the DocBot ROS 2 workspace: the custom interfaces
package and the audio capture node. Together they get you to a system where
the microphone is being captured, frames are flowing on `/audio`, and you can
ask the node to dump the last N seconds to a WAV file for inspection.

## Layout

Copy both directories into your workspace's `src/`:

```
docbot-docker/
└── src/
    ├── docbot_interfaces/      ← from packages/docbot_interfaces/
    └── docbot_audio/           ← from packages/docbot_audio/
```

## Container prerequisites

The audio node needs **PortAudio** (system) and **sounddevice** (Python).
Add to your `docker/Dockerfile` (or run inside the container once to verify
before committing to the image):

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    libportaudio2 \
 && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir sounddevice numpy
```

The `--device /dev/snd` part is already handled — your `docker-compose.yaml`
mounts `/dev` wholesale and sets `privileged: true`, which is the simplest
path to ALSA access.

Inside the container, verify the mic is visible:

```bash
arecord -l
python3 -c "import sounddevice as sd; print(sd.query_devices())"
```

Note the index or name of your desired input device — that goes into
`audio.yaml` under `device:`.

## Build

From inside the container, at the workspace root:

```bash
cd /workspaces/shared_ws
colcon build --packages-select docbot_interfaces
source install/setup.bash
colcon build --packages-select docbot_audio
source install/setup.bash
```

Two-step build is important the first time: `docbot_audio` depends on the
generated interfaces from `docbot_interfaces`, so the interfaces must be
built and sourced before the audio package's build environment can resolve
the imports.

## Run

```bash
ros2 launch docbot_audio audio_capture.launch.py
```

You should see startup logs like:

```
[audio_capture]: Opening audio: device=None rate=16000 Hz channels=1 frame_samples=480
[audio_capture]: Audio capture running: forensic buffer = 60.0s (2000 frames), publishing /audio @ 33.3 Hz
```

## Quick verification

In a second terminal (also sourced):

```bash
# Confirm topic and rate
ros2 topic list
ros2 topic hz /audio          # should report ~33 Hz
ros2 topic info /audio        # should show docbot_interfaces/msg/AudioFrame

# Check service is up
ros2 service list | grep dump_buffer
ros2 service type /audio_capture/dump_buffer
```

## Exercise the dump service

Ask for the last 5 seconds of audio:

```bash
ros2 service call /audio_capture/dump_buffer \
  docbot_interfaces/srv/DumpAudioBuffer \
  '{end_time: {sec: 0, nanosec: 0}, window: {sec: 5, nanosec: 0}, tag: "smoketest"}'
```

`end_time: {sec: 0, nanosec: 0}` means "now"; window is 5 seconds. You should
get a response with `success: true` and a path under `/data/audio_dumps/`.
Inspect with:

```bash
ls -l /data/audio_dumps/
soxi /data/audio_dumps/smoketest_*.wav     # if sox is installed
# Or open in DaVinci Resolve / Audacity from the host side
```

## Override device at runtime

If `default` doesn't pick the right mic, override via params:

```bash
# Inline override on the command line
ros2 launch docbot_audio audio_capture.launch.py \
  params_file:=<(echo 'audio_capture: {ros__parameters: {device: "USB"}}')

# Or edit src/docbot_audio/config/audio.yaml and rebuild
```

The `device` field accepts either a numeric index as a string ("2") or a
substring of the device name ("USB", "ReSpeaker", etc.).

## What to look for

- **`ros2 topic hz /audio` is ~33 Hz, stable.** If it drifts or stalls,
  PortAudio is likely struggling — check `arecord -L` for buffer underruns
  and try increasing `frame_ms` to 40 or 50.
- **`ros2 topic echo /audio --no-arr` is non-empty.** The `--no-arr` flag
  suppresses the huge `samples` array; useful for sanity checks.
- **Forensic dump WAV plays back as expected.** Listen to it — if the audio
  is fine, the whole pipeline is ready for downstream consumers.

## Next packages, in order

1. `docbot_tts` — simplest leaf; gets playback working.
2. `docbot_stt` — action server for transcription.
3. `docbot_trigger` — wake word.
4. `docbot_llm` — Ollama client (after `OLLAMA_SETUP.md` is done).
5. `docbot_health` — log + escalation services.
6. `docbot_dialog` — the orchestrator state machine.
7. `docbot_bringup` — composes everything with one launch file.
