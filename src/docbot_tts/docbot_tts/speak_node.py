#!/usr/bin/env python3
"""TTS playback node for DocBot.

Action server that synthesizes a response with Piper, gates the wake-word
trigger while playing, and publishes progress feedback.

Design notes:
  - Piper outputs at the voice's native sample rate (22050 Hz for
    lessac-medium). The output device may not support that rate (Scarlett
    interfaces are 44.1k+ only), so we open the OutputStream at the device's
    native rate and resample with scipy.signal.resample_poly — the same
    pattern docbot_audio uses on capture.
  - SetMicGate is best-effort. At startup the wake-word node may not exist
    yet, and during operation we never want a missing gate service to block
    playback. Gate failures only log a warning.
  - One Speak goal at a time. A second goal blocks on _action_lock until the
    first finishes (rather than queueing arbitrarily inside ROS).
"""
import threading
import time
from fractions import Fraction
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy import signal

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from builtin_interfaces.msg import Duration as DurationMsg

from docbot_interfaces.action import Speak
from docbot_interfaces.srv import SetMicGate

from piper.voice import PiperVoice


class SpeakNode(Node):
    def __init__(self):
        super().__init__('tts')

        # ------- Parameters -------
        self.declare_parameter(
            'model_path', '/opt/piper-voices/en_US-lessac-medium.onnx')
        self.declare_parameter(
            'config_path', '/opt/piper-voices/en_US-lessac-medium.onnx.json')
        self.declare_parameter('playback_device', 'default')
        self.declare_parameter('output_channels', 2)
        self.declare_parameter('pre_play_gate_ms', 100)
        self.declare_parameter('post_play_gate_ms', 400)
        self.declare_parameter('set_gate_service', '/wake_word/set_gate')
        self.declare_parameter('default_length_scale', 1.0)
        self.declare_parameter('progress_chunk_ms', 100)

        model_path = self.get_parameter('model_path').value
        config_path = self.get_parameter('config_path').value
        if not Path(model_path).exists():
            self.get_logger().fatal(f"Piper model not found: {model_path}")
            raise FileNotFoundError(model_path)

        playback_param = self.get_parameter('playback_device').value
        if playback_param in (None, '', 'default'):
            self.playback_device = None
        elif isinstance(playback_param, str) and playback_param.isdigit():
            self.playback_device = int(playback_param)
        else:
            self.playback_device = playback_param

        self.output_channels = int(self.get_parameter('output_channels').value)
        if self.output_channels < 1:
            self.output_channels = 1
        self.pre_play_gate_ms = int(self.get_parameter('pre_play_gate_ms').value)
        self.post_play_gate_ms = int(self.get_parameter('post_play_gate_ms').value)
        self.set_gate_service_name = self.get_parameter('set_gate_service').value
        self.default_length_scale = float(
            self.get_parameter('default_length_scale').value)
        self.progress_chunk_ms = int(
            self.get_parameter('progress_chunk_ms').value)

        # ------- Load Piper voice -------
        self.get_logger().info(f"Loading Piper voice: {model_path}")
        self._voice = PiperVoice.load(model_path, config_path=config_path)
        self.synth_sample_rate = int(self._voice.config.sample_rate)

        # Pick the synthesis call shape once. piper-tts has revised the API
        # several times: <=1.2 used synthesize_stream_raw → bytes; 1.2.x used
        # synthesize(text, length_scale=…) → AudioChunk; current versions use
        # synthesize(text, syn_config=SynthesisConfig(...)) → AudioChunk.
        self._synthesize_chunks = self._select_synthesis_api()
        self.get_logger().info(
            f"Piper voice loaded: native_rate={self.synth_sample_rate} Hz")

        # ------- Resolve output device native rate -------
        # Same approach as docbot_audio: open at the device's native rate and
        # resample. PortAudio rejects rates the hardware doesn't support.
        try:
            dev_info = sd.query_devices(self.playback_device, 'output')
            self.device_sample_rate = int(dev_info['default_samplerate'])
            self.get_logger().info(
                f"Output device: '{dev_info['name']}' "
                f"(max_output_channels={dev_info['max_output_channels']}, "
                f"default_samplerate={self.device_sample_rate})")
        except Exception as e:
            self.get_logger().warn(
                f"Could not query output device for native rate ({e}); "
                f"falling back to synth rate {self.synth_sample_rate} Hz")
            self.device_sample_rate = self.synth_sample_rate

        if self.synth_sample_rate != self.device_sample_rate:
            ratio = Fraction(self.device_sample_rate, self.synth_sample_rate)
            self._resample_up = ratio.numerator
            self._resample_down = ratio.denominator
            self.get_logger().info(
                f"Will resample {self.synth_sample_rate} Hz → "
                f"{self.device_sample_rate} Hz "
                f"(ratio {self._resample_up}/{self._resample_down})")
        else:
            self._resample_up = 1
            self._resample_down = 1

        # ------- SetMicGate client -------
        # Reentrant cb group so the action's execute thread can call into the
        # service client without deadlocking on the default mutex group.
        self._cb_group = ReentrantCallbackGroup()
        self._gate_client = self.create_client(
            SetMicGate, self.set_gate_service_name,
            callback_group=self._cb_group)

        # ------- Action server -------
        self._action_lock = threading.Lock()
        self._action_server = ActionServer(
            self,
            Speak,
            '~/speak',
            execute_callback=self._execute_speak,
            goal_callback=self._goal_callback,
            cancel_callback=self._cancel_callback,
            callback_group=self._cb_group,
        )

        self.get_logger().info(
            f"SpeakNode ready: action server at '{self.get_name()}/speak'")

    # --------------------------------------------------------------- action cb
    def _goal_callback(self, goal_request):
        if not goal_request.text or not goal_request.text.strip():
            self.get_logger().warn("Rejecting Speak goal: empty text")
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def _cancel_callback(self, _goal_handle):
        self.get_logger().info("Speak goal cancellation requested")
        return CancelResponse.ACCEPT

    def _execute_speak(self, goal_handle):
        # Serialize so two simultaneous Speak goals don't fight for the
        # OutputStream. The second will block here until the first finishes.
        with self._action_lock:
            return self._run_synthesis(goal_handle)

    # --------------------------------------------------------------- gate srv
    def _set_gate(self, gated: bool, timeout: float = 0.5) -> bool:
        """Best-effort gate change; returns True on confirmed success."""
        if not self._gate_client.wait_for_service(timeout_sec=0.05):
            self.get_logger().warn(
                f"SetMicGate service '{self.set_gate_service_name}' "
                f"not available; proceeding without gating",
                throttle_duration_sec=10.0)
            return False
        req = SetMicGate.Request()
        req.gated = gated
        future = self._gate_client.call_async(req)
        # We're in the action's execute thread under MultiThreadedExecutor; the
        # service response is delivered by a different executor thread, so a
        # short busy-wait is the simplest correct pattern here.
        start = time.monotonic()
        while not future.done() and (time.monotonic() - start) < timeout:
            time.sleep(0.01)
        if not future.done():
            self.get_logger().warn(
                f"SetMicGate call timed out after {timeout:.2f}s "
                f"(gated={gated})")
            return False
        resp = future.result()
        if resp is None or not resp.success:
            self.get_logger().warn(
                f"SetMicGate(gated={gated}) returned failure")
            return False
        return True

    # ----------------------------------------------------------- synth + play
    def _run_synthesis(self, goal_handle):
        request = goal_handle.request
        text = request.text.strip()
        rate = request.rate if request.rate > 0 else 1.0
        # Piper's length_scale inverts speech rate: 0.5 → twice as fast.
        length_scale = self.default_length_scale / rate

        preview = text[:60] + ('…' if len(text) > 60 else '')
        self.get_logger().info(
            f"Speaking ({len(text)} chars, rate={rate:.2f}, "
            f"length_scale={length_scale:.2f}): {preview}")

        # ------- Synthesize whole utterance into memory -------
        # Piper is fast (>10× real-time on a desktop CPU, ~3-5× on Orin Nano),
        # so synth-then-play is simpler than streaming-while-resampling. Total
        # added latency for a sentence is well under 200ms.
        synth_start = time.monotonic()
        try:
            chunks = self._synthesize_chunks(text, length_scale)
        except Exception as e:
            return self._fail(goal_handle, f"Piper synthesis failed: {e}")

        if not chunks:
            return self._fail(goal_handle, "Piper produced no audio")

        audio_native = np.concatenate(chunks)
        synth_elapsed = time.monotonic() - synth_start
        synth_dur = audio_native.shape[0] / self.synth_sample_rate
        self.get_logger().info(
            f"Synthesized {synth_dur:.2f}s in {synth_elapsed:.2f}s "
            f"({synth_dur / max(synth_elapsed, 1e-6):.1f}× real-time)")

        # ------- Resample to output device rate -------
        if self._resample_up != self._resample_down:
            resampled = signal.resample_poly(
                audio_native.astype(np.float32),
                up=self._resample_up, down=self._resample_down)
            np.clip(resampled, -32768, 32767, out=resampled)
            audio_play = resampled.astype(np.int16)
        else:
            audio_play = audio_native

        # Duplicate mono → N-channel interleaved (PortAudio expects shape
        # (frames, channels) for multi-channel writes). Most consumer outputs
        # refuse to play mono streams; stereo with both channels equal is the
        # safe default.
        if self.output_channels > 1:
            audio_play = np.repeat(
                audio_play[:, np.newaxis], self.output_channels, axis=1)

        total_samples = audio_play.shape[0]
        total_seconds = total_samples / self.device_sample_rate

        # ------- Gate mic, play, ungate -------
        gate_set = self._set_gate(True)
        if self.pre_play_gate_ms > 0:
            time.sleep(self.pre_play_gate_ms / 1000.0)

        chunk_samples = max(
            1, int(self.device_sample_rate * self.progress_chunk_ms / 1000))
        canceled = False
        played_samples = 0
        try:
            with sd.OutputStream(
                    device=self.playback_device,
                    samplerate=self.device_sample_rate,
                    channels=self.output_channels,
                    dtype='int16') as stream:
                pos = 0
                while pos < total_samples:
                    if goal_handle.is_cancel_requested:
                        stream.abort()
                        canceled = True
                        break
                    end = min(pos + chunk_samples, total_samples)
                    stream.write(audio_play[pos:end])
                    pos = end
                    played_samples = pos
                    fb = Speak.Feedback()
                    fb.progress = float(pos) / float(total_samples)
                    goal_handle.publish_feedback(fb)
        except Exception as e:
            if gate_set:
                self._set_gate(False)
            return self._fail(goal_handle, f"Playback failed: {e}")

        # Brief tail before releasing the gate, so the speaker decay and any
        # remaining device-buffer audio don't trigger the wake word.
        if not canceled and self.post_play_gate_ms > 0:
            time.sleep(self.post_play_gate_ms / 1000.0)
        if gate_set:
            self._set_gate(False)

        # ------- Result -------
        result = Speak.Result()
        if canceled:
            played_seconds = played_samples / self.device_sample_rate
            result.success = False
            result.duration = self._secs_to_duration(played_seconds)
            self.get_logger().info(
                f"Speak canceled at {played_seconds:.2f}s of "
                f"{total_seconds:.2f}s")
            goal_handle.canceled()
            return result

        result.success = True
        result.duration = self._secs_to_duration(total_seconds)
        goal_handle.succeed()
        return result

    # --------------------------------------------------------- piper api shim
    def _select_synthesis_api(self):
        """Return a callable (text, length_scale) -> list[np.ndarray int16].

        piper-tts has shipped three incompatible synthesis APIs in the last
        year. We probe once at init so the hot path stays branch-free.
        """
        voice = self._voice

        if hasattr(voice, 'synthesize'):
            # Try the current SynthesisConfig-based API first.
            try:
                from piper import SynthesisConfig  # noqa: F401
                def _synth_config_api(text: str, length_scale: float):
                    from piper import SynthesisConfig
                    cfg = SynthesisConfig(length_scale=length_scale)
                    return [self._chunk_to_int16(c)
                            for c in voice.synthesize(text, syn_config=cfg)]
                # Verify with a 1-character probe so we fail at init, not
                # on the user's first goal.
                _synth_config_api('a', 1.0)
                self.get_logger().info(
                    "Piper API: synthesize(text, syn_config=SynthesisConfig)")
                return _synth_config_api
            except (ImportError, TypeError):
                pass

            # Older kwargs-based synthesize().
            try:
                def _synth_kwargs_api(text: str, length_scale: float):
                    return [self._chunk_to_int16(c)
                            for c in voice.synthesize(
                                text, length_scale=length_scale)]
                _synth_kwargs_api('a', 1.0)
                self.get_logger().info(
                    "Piper API: synthesize(text, length_scale=…)")
                return _synth_kwargs_api
            except TypeError:
                pass

        if hasattr(voice, 'synthesize_stream_raw'):
            def _synth_stream_raw_api(text: str, length_scale: float):
                return [np.frombuffer(b, dtype=np.int16)
                        for b in voice.synthesize_stream_raw(
                            text, length_scale=length_scale)]
            self.get_logger().info("Piper API: synthesize_stream_raw")
            return _synth_stream_raw_api

        raise RuntimeError(
            "Installed piper-tts has no recognized synthesis method "
            "(checked synthesize / synthesize_stream_raw)")

    @staticmethod
    def _chunk_to_int16(chunk) -> np.ndarray:
        """Coerce a piper AudioChunk (or raw bytes) into an int16 ndarray."""
        if hasattr(chunk, 'audio_int16_array'):
            return chunk.audio_int16_array
        if hasattr(chunk, 'audio_int16_bytes'):
            return np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            return np.frombuffer(bytes(chunk), dtype=np.int16)
        raise TypeError(f"unknown piper chunk type: {type(chunk)!r}")

    def _fail(self, goal_handle, message: str) -> Speak.Result:
        self.get_logger().error(message)
        try:
            self._set_gate(False)
        except Exception:
            pass
        result = Speak.Result()
        result.success = False
        result.duration = self._secs_to_duration(0.0)
        goal_handle.abort()
        return result

    @staticmethod
    def _secs_to_duration(secs: float) -> DurationMsg:
        d = DurationMsg()
        d.sec = int(secs)
        d.nanosec = int((secs - int(secs)) * 1e9)
        return d


def main(args=None):
    rclpy.init(args=args)
    node = SpeakNode()
    # MultiThreadedExecutor so the action's execute callback can call the
    # SetMicGate service client without deadlocking.
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
