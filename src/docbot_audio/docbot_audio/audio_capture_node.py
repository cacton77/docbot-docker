#!/usr/bin/env python3
"""Audio capture node for DocBot.

Opens a single microphone via PortAudio (sounddevice), publishes /audio at the
configured frame rate, maintains a rolling forensic buffer of the most recent
audio, and serves DumpAudioBuffer requests to write WAV snapshots to disk.

Design notes:
  - PCM 16-bit signed little-endian mono at 16 kHz is the universal speech
    format: Silero VAD, openWakeWord, and Whisper all consume it natively.
  - The audio thread is PortAudio's; ROS publishing happens from inside it.
    rclpy publishers are thread-safe.
  - Consumers that need a short pre-roll (e.g. STT after a trigger fires)
    maintain their own rolling buffer of /audio. This node owns only the
    longer forensic buffer used by escalations.
"""
import collections
import threading
import wave
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy import signal

import rclpy
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from rclpy.time import Time

from docbot_interfaces.msg import AudioFrame
from docbot_interfaces.srv import DumpAudioBuffer


class AudioCaptureNode(Node):
    def __init__(self):
        super().__init__('audio_capture')

        # ------- Parameters -------
        self.declare_parameter('device', 'default')
        self.declare_parameter('sample_rate', 16000)
        self.declare_parameter('channels', 1)
        self.declare_parameter('sample_format', 'S16_LE')
        self.declare_parameter('frame_ms', 30)
        self.declare_parameter('forensic_seconds', 60.0)
        self.declare_parameter('dump_directory', '/data/audio_dumps')

        device_param = self.get_parameter('device').value
        if device_param in (None, '', 'default'):
            self.device = None
        elif isinstance(device_param, str) and device_param.isdigit():
            self.device = int(device_param)
        else:
            self.device = device_param

        # Target rate is what /audio messages advertise and downstream consumers
        # (VAD, wake word, Whisper) expect. The capture device may not support
        # this rate natively (e.g. Scarlett interfaces are 44.1/48k+ only), so
        # we open the stream at the device's native rate and resample.
        self.sample_rate = int(self.get_parameter('sample_rate').value)
        self.channels = int(self.get_parameter('channels').value)
        self.sample_format = self.get_parameter('sample_format').value
        self.frame_ms = int(self.get_parameter('frame_ms').value)
        self.frame_samples = int(self.sample_rate * self.frame_ms / 1000)

        forensic_seconds = float(self.get_parameter('forensic_seconds').value)
        self.forensic_max_frames = max(
            1, int(forensic_seconds * 1000 / self.frame_ms))

        self.dump_directory = Path(self.get_parameter('dump_directory').value)
        self.dump_directory.mkdir(parents=True, exist_ok=True)

        # The AudioFrame.samples field is int16[]; we only support S16_LE.
        if self.sample_format != 'S16_LE':
            self.get_logger().error(
                f"sample_format='{self.sample_format}' not supported; "
                f"forcing S16_LE")
            self.sample_format = 'S16_LE'

        # ------- State -------
        self._frame_index = 0
        self._buf_lock = threading.Lock()
        # Each entry: (frame_index, stamp_msg, samples_int16_ndarray)
        self._forensic_buf = collections.deque(maxlen=self.forensic_max_frames)
        self._publish_failures = 0

        # ------- Publisher -------
        qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=32,
        )
        self._pub = self.create_publisher(AudioFrame, 'audio', qos)

        # ------- Service -------
        self._dump_srv = self.create_service(
            DumpAudioBuffer, '~/dump_buffer', self._dump_callback)

        # ------- Resolve native capture rate -------
        # PortAudio rejects rates the hardware can't do, so we ask the device
        # what it natively supports and open at that rate, then resample to the
        # target rate before publishing.
        try:
            dev_info = sd.query_devices(self.device, 'input')
            self.native_sample_rate = int(dev_info['default_samplerate'])
        except Exception as e:
            self.get_logger().warn(
                f"Could not query device for native rate ({e}); "
                f"falling back to target rate {self.sample_rate} Hz")
            self.native_sample_rate = self.sample_rate

        # Native block size chosen so that after resampling we get ~frame_samples
        # output samples per callback (within ±1 at non-integer ratios).
        self.native_frame_samples = int(round(
            self.frame_samples * self.native_sample_rate / self.sample_rate))

        # Resample ratio in lowest terms: up/down = target/native.
        # For 48000→16000 this is 1/3; for 44100→16000 it is 160/441.
        if self.native_sample_rate != self.sample_rate:
            ratio = Fraction(self.sample_rate, self.native_sample_rate)
            self._resample_up = ratio.numerator
            self._resample_down = ratio.denominator
            self._resample = True
        else:
            self._resample_up = 1
            self._resample_down = 1
            self._resample = False

        # ------- Open input stream -------
        if self._resample:
            self.get_logger().info(
                f"Opening audio: device={self.device} "
                f"native_rate={self.native_sample_rate} Hz "
                f"→ target_rate={self.sample_rate} Hz "
                f"(resample {self._resample_up}/{self._resample_down}) "
                f"channels={self.channels} "
                f"native_block={self.native_frame_samples} "
                f"target_frame={self.frame_samples}")
        else:
            self.get_logger().info(
                f"Opening audio: device={self.device} rate={self.sample_rate} Hz "
                f"channels={self.channels} frame_samples={self.frame_samples}")

        try:
            self._stream = sd.InputStream(
                device=self.device,
                samplerate=self.native_sample_rate,
                channels=self.channels,
                dtype='int16',
                blocksize=self.native_frame_samples,
                callback=self._audio_callback,
            )
            self._stream.start()
        except Exception as e:
            self.get_logger().fatal(f"Failed to open audio device: {e}")
            raise

        self.get_logger().info(
            f"Audio capture running: forensic buffer = "
            f"{forensic_seconds:.1f}s ({self.forensic_max_frames} frames), "
            f"publishing /audio @ {1000/self.frame_ms:.1f} Hz")

    # --------------------------------------------------------------- audio cb
    def _audio_callback(self, indata, frames, time_info, status):
        """Called from PortAudio thread on each captured block."""
        if status:
            self.get_logger().warn(f"Audio status: {status}", throttle_duration_sec=2.0)

        # Approximate stamp of first sample: now - (frames-1)/native_rate.
        # `frames` is in native samples here; resampling doesn't change the
        # wall-clock time of the first sample.
        now = self.get_clock().now()
        offset_ns = int((frames - 1) * 1e9 / self.native_sample_rate)
        stamp_msg = (now - Duration(nanoseconds=offset_ns)).to_msg()

        # Contiguous copy; sounddevice may reuse the buffer after callback returns
        if indata.ndim > 1 and self.channels == 1:
            samples = indata[:, 0].copy()
        else:
            samples = indata.reshape(-1).copy()

        # Resample to target rate. resample_poly is stateless; per-chunk
        # boundary artifacts are below the noise floor for 30ms speech frames.
        if self._resample:
            resampled = signal.resample_poly(
                samples.astype(np.float32),
                up=self._resample_up,
                down=self._resample_down)
            np.clip(resampled, -32768, 32767, out=resampled)
            samples = resampled.astype(np.int16)
            # Normalize length to exactly frame_samples; ±1 sample drift is
            # possible at non-integer ratios (e.g. 1323 native @ 44.1k → 480
            # target, but ceil-based output can come out 480 or 481).
            n = samples.shape[0]
            if n > self.frame_samples:
                samples = samples[:self.frame_samples]
            elif n < self.frame_samples:
                samples = np.concatenate([
                    samples,
                    np.zeros(self.frame_samples - n, dtype=np.int16),
                ])

        # Append to forensic buffer (deque is thread-safe for append, but we
        # take the lock to coordinate with dump_callback's iteration)
        with self._buf_lock:
            self._forensic_buf.append(
                (self._frame_index, stamp_msg, samples))

        # Build and publish
        msg = AudioFrame()
        msg.header.stamp = stamp_msg
        msg.header.frame_id = 'mic'
        msg.sample_rate = self.sample_rate
        msg.channels = self.channels
        msg.sample_format = AudioFrame.SAMPLE_FORMAT_PCM_S16LE
        msg.frame_index = self._frame_index
        # int16[] accepts a Python list; .tolist() at 480 samples is cheap
        msg.samples = samples.tolist()

        try:
            self._pub.publish(msg)
        except Exception as e:
            self._publish_failures += 1
            if self._publish_failures % 100 == 1:
                self.get_logger().error(
                    f"Publish failed ({self._publish_failures} total): {e}")

        self._frame_index += 1

    # --------------------------------------------------------------- dump srv
    def _dump_callback(self, request, response):
        """Write a slice of the forensic buffer to disk as a WAV file."""
        # Resolve end_time: if zero, use now
        if request.end_time.sec == 0 and request.end_time.nanosec == 0:
            end_time = self.get_clock().now()
        else:
            end_time = Time.from_msg(request.end_time)

        window_ns = (request.window.sec * 1_000_000_000
                     + request.window.nanosec)
        if window_ns <= 0:
            response.success = False
            response.message = "window must be > 0"
            return response

        start_time = end_time - Duration(nanoseconds=window_ns)
        start_ns = start_time.nanoseconds
        end_ns = end_time.nanoseconds

        # Collect samples whose stamp falls in [start, end]
        selected = []
        with self._buf_lock:
            for _, stamp_msg, samples in self._forensic_buf:
                frame_ns = (stamp_msg.sec * 1_000_000_000
                            + stamp_msg.nanosec)
                if start_ns <= frame_ns <= end_ns:
                    selected.append(samples)

        if not selected:
            response.success = False
            response.file_path = ""
            response.num_samples = 0
            response.message = (
                "No frames in requested window "
                "(buffer empty or window outside retention)")
            return response

        audio = np.concatenate(selected)
        num_samples = int(audio.shape[0])

        # Safe filename
        tag = (request.tag or "dump").strip()
        tag = "".join(c if c.isalnum() or c in "._-" else "_" for c in tag)
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.dump_directory / f"{tag}_{ts_str}.wav"

        try:
            with wave.open(str(path), 'wb') as wf:
                wf.setnchannels(self.channels)
                wf.setsampwidth(2)  # int16
                wf.setframerate(self.sample_rate)
                wf.writeframes(audio.tobytes())
        except Exception as e:
            response.success = False
            response.file_path = ""
            response.num_samples = 0
            response.message = f"Failed to write WAV: {e}"
            return response

        # Note clipping if the buffer didn't have the full window
        requested_samples = int(window_ns * self.sample_rate / 1e9)
        clip_note = ""
        if num_samples < requested_samples * 0.95:
            clip_note = (f" (clipped: got {num_samples} of "
                         f"~{requested_samples} requested)")

        response.success = True
        response.file_path = str(path)
        response.num_samples = num_samples
        response.message = f"Wrote {num_samples} samples to {path}{clip_note}"
        self.get_logger().info(response.message)
        return response

    # --------------------------------------------------------------- shutdown
    def shutdown(self):
        try:
            if hasattr(self, '_stream'):
                self._stream.stop()
                self._stream.close()
                self.get_logger().info("Audio stream stopped.")
        except Exception as e:
            self.get_logger().warn(f"Error during shutdown: {e}")


def main(args=None):
    rclpy.init(args=args)
    node = AudioCaptureNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
