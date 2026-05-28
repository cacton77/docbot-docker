#!/usr/bin/env python3
"""Speech-to-text action server for DocBot (Phase 1: fixed-duration capture).

Buffers /audio frames in a rolling pre-roll deque so the action goal's
preroll_ms can prepend audio captured *before* the goal arrived. On goal:
records frames for max_duration seconds, runs faster-whisper on the result,
and returns the transcript. VAD endpointing and streaming partials are
deliberately deferred — see ARCHITECTURE.md §6 step 4 ("listen for N
seconds mode first, then add VAD").

Design notes:
  - faster-whisper expects float32 in [-1, 1]; we receive int16 from /audio
    and divide by 32768 once at the end (not per-callback).
  - Model is loaded once at startup. Auto device-resolution tries CUDA then
    falls back to CPU/int8 so the same build runs on Jetson, desktop+GPU,
    and laptops. Loaded with local_files_only=True so a missing image-time
    download fails loudly instead of silently fetching at runtime.
  - One transcribe goal at a time, serialized via _action_lock. Cancellation
    stops capture early but still runs whisper on what was recorded.
"""
import collections
import math
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy

from builtin_interfaces.msg import Duration as DurationMsg

from docbot_interfaces.action import Transcribe
from docbot_interfaces.msg import AudioFrame

from faster_whisper import WhisperModel


class TranscribeNode(Node):
    def __init__(self):
        super().__init__('stt')

        # ------- Parameters -------
        self.declare_parameter('model_size', 'small.en')
        self.declare_parameter('model_root', '/opt/whisper-models')
        self.declare_parameter('device', 'auto')
        self.declare_parameter('cuda_compute_type', 'int8_float16')
        self.declare_parameter('cpu_compute_type', 'int8')
        self.declare_parameter('language', 'en')
        self.declare_parameter('beam_size', 1)
        self.declare_parameter('initial_prompt', '')
        self.declare_parameter('default_record_seconds', 5.0)
        self.declare_parameter('preroll_buffer_seconds', 3.0)
        self.declare_parameter('feedback_interval_ms', 200)
        self.declare_parameter('audio_clip_directory', '/data/utterances')
        self.declare_parameter('save_audio_clips', True)

        self.model_size = self.get_parameter('model_size').value
        self.model_root = self.get_parameter('model_root').value
        self.device_pref = self.get_parameter('device').value
        self.cuda_compute_type = self.get_parameter('cuda_compute_type').value
        self.cpu_compute_type = self.get_parameter('cpu_compute_type').value
        self.language = self.get_parameter('language').value or None
        self.beam_size = int(self.get_parameter('beam_size').value)
        self.initial_prompt = self.get_parameter('initial_prompt').value or None
        self.default_record_seconds = float(
            self.get_parameter('default_record_seconds').value)
        self.preroll_buffer_seconds = float(
            self.get_parameter('preroll_buffer_seconds').value)
        self.feedback_interval_ms = int(
            self.get_parameter('feedback_interval_ms').value)
        self.save_audio_clips = bool(
            self.get_parameter('save_audio_clips').value)
        self.audio_clip_dir = Path(
            self.get_parameter('audio_clip_directory').value)
        if self.save_audio_clips:
            self.audio_clip_dir.mkdir(parents=True, exist_ok=True)

        # ------- Load Whisper model -------
        self._model, self._device, self._compute_type = self._load_model()

        # ------- /audio state -------
        # Discovered on first frame; needed to compute preroll sample counts
        # and the audio_clip wav header.
        self.sample_rate: int | None = None
        # 300 frames at 30ms = 9s, comfortably covers preroll_buffer_seconds.
        self._preroll_buf: collections.deque = collections.deque(maxlen=300)
        self._audio_lock = threading.Lock()

        # Capture state — guarded by _audio_lock. _capture_buffer is appended
        # to from the audio callback while _capture_active is set.
        self._capture_active = False
        self._capture_buffer: list[np.ndarray] = []

        audio_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=32,
        )
        self._cb_group = ReentrantCallbackGroup()
        self._audio_sub = self.create_subscription(
            AudioFrame, '/audio', self._audio_callback,
            audio_qos, callback_group=self._cb_group)

        # ------- Action server -------
        self._action_lock = threading.Lock()
        self._action_server = ActionServer(
            self,
            Transcribe,
            '~/transcribe',
            execute_callback=self._execute_transcribe,
            goal_callback=lambda _g: GoalResponse.ACCEPT,
            cancel_callback=lambda _h: CancelResponse.ACCEPT,
            callback_group=self._cb_group,
        )

        self.get_logger().info(
            f"TranscribeNode ready: action server at "
            f"'{self.get_name()}/transcribe' (model={self.model_size}, "
            f"device={self._device}/{self._compute_type})")

    # --------------------------------------------------------------- model
    def _load_model(self) -> tuple[WhisperModel, str, str]:
        """Load faster-whisper with the configured device + fallback policy."""
        if self.device_pref == 'auto':
            attempts = [('cuda', self.cuda_compute_type),
                        ('cpu', self.cpu_compute_type)]
        elif self.device_pref == 'cuda':
            attempts = [('cuda', self.cuda_compute_type)]
        elif self.device_pref == 'cpu':
            attempts = [('cpu', self.cpu_compute_type)]
        else:
            raise ValueError(
                f"device must be 'auto', 'cuda', or 'cpu', got '{self.device_pref}'")

        last_err: Exception | None = None
        for device, compute_type in attempts:
            try:
                self.get_logger().info(
                    f"Loading faster-whisper '{self.model_size}' "
                    f"on {device}/{compute_type}…")
                # local_files_only=True so we crash on missing cache rather
                # than silently downloading ~480MB on a user's first goal.
                model = WhisperModel(
                    self.model_size,
                    device=device,
                    compute_type=compute_type,
                    download_root=self.model_root,
                    local_files_only=True,
                )
                # CT2 can succeed at load and fail at first matmul on
                # incompatible CUDA setups (pre-Volta GPUs, cuBLAS/driver
                # version mismatch — e.g. CUBLAS_STATUS_NOT_SUPPORTED).
                # Force a 1-second silent transcribe so we discover that
                # here, not on the user's first real goal.
                self._verify_model(model)
                self.get_logger().info(
                    f"Whisper loaded: {self.model_size} "
                    f"({device}/{compute_type})")
                return model, device, compute_type
            except Exception as e:
                last_err = e
                self.get_logger().warn(
                    f"Whisper unusable on {device}/{compute_type}: "
                    f"{type(e).__name__}: {e}")

        raise RuntimeError(
            f"Could not load Whisper '{self.model_size}' on any device "
            f"(last error: {last_err})")

    @staticmethod
    def _verify_model(model: WhisperModel) -> None:
        """Run a tiny transcribe to catch compute-time CUDA failures."""
        silence = np.zeros(16000, dtype=np.float32)  # 1 s @ 16 kHz
        segments_iter, _ = model.transcribe(
            silence, language='en', beam_size=1, without_timestamps=True)
        # Generators are lazy; iterate to actually trigger kernels.
        list(segments_iter)

    # ------------------------------------------------------------ /audio sub
    def _audio_callback(self, msg: AudioFrame) -> None:
        # int16[] arrives as array.array('h', ...) under rclpy; np.frombuffer
        # avoids a Python-level per-element copy.
        try:
            samples = np.frombuffer(msg.samples, dtype=np.int16)
        except (TypeError, BufferError):
            samples = np.array(msg.samples, dtype=np.int16)

        with self._audio_lock:
            if self.sample_rate is None:
                self.sample_rate = int(msg.sample_rate)
                self.get_logger().info(
                    f"First /audio frame seen: sample_rate={self.sample_rate} Hz, "
                    f"frame_samples={samples.shape[0]}")
            self._preroll_buf.append(samples)
            if self._capture_active:
                self._capture_buffer.append(samples)

    # ----------------------------------------------------------- action cb
    def _execute_transcribe(self, goal_handle):
        # Serialize so two simultaneous goals don't interleave their captures.
        with self._action_lock:
            return self._run_transcribe(goal_handle)

    def _run_transcribe(self, goal_handle):
        request = goal_handle.request

        # Wait briefly for the audio stream to come up if it hasn't yet.
        if self.sample_rate is None:
            wait_start = time.monotonic()
            while self.sample_rate is None:
                if goal_handle.is_cancel_requested:
                    return self._fail(goal_handle,
                                      "Canceled before any audio arrived")
                if time.monotonic() - wait_start > 5.0:
                    return self._fail(
                        goal_handle,
                        "No /audio frames received within 5 s. "
                        "Is docbot_audio running?")
                time.sleep(0.05)

        sample_rate = self.sample_rate

        max_dur_sec = (request.max_duration.sec
                       + request.max_duration.nanosec / 1e9)
        if max_dur_sec <= 0:
            max_dur_sec = self.default_record_seconds

        preroll_target_samples = int(
            request.preroll_ms / 1000.0 * sample_rate)

        language = request.language or self.language

        self.get_logger().info(
            f"Transcribe goal: max_duration={max_dur_sec:.1f}s, "
            f"preroll={request.preroll_ms}ms ({preroll_target_samples} "
            f"samples), language={language!r}")

        # --- Start capture, prepended with preroll from the rolling buffer ---
        with self._audio_lock:
            preroll_arrays: list[np.ndarray] = []
            preroll_total = 0
            for arr in reversed(self._preroll_buf):
                if preroll_total >= preroll_target_samples:
                    break
                preroll_arrays.insert(0, arr)
                preroll_total += arr.shape[0]

            # Trim leading samples if we overshot the requested preroll.
            if preroll_arrays and preroll_total > preroll_target_samples:
                overshoot = preroll_total - preroll_target_samples
                preroll_arrays[0] = preroll_arrays[0][overshoot:]
                preroll_total -= overshoot

            self._capture_buffer = list(preroll_arrays)
            self._capture_active = True

        # --- Record for max_dur_sec, publishing feedback ---
        capture_start = time.monotonic()
        last_feedback = capture_start
        feedback_interval = self.feedback_interval_ms / 1000.0
        canceled = False
        while True:
            elapsed = time.monotonic() - capture_start
            if elapsed >= max_dur_sec:
                break
            if goal_handle.is_cancel_requested:
                canceled = True
                break
            now = time.monotonic()
            if now - last_feedback >= feedback_interval:
                self._publish_feedback(goal_handle)
                last_feedback = now
            time.sleep(0.02)

        # --- Stop capture, snapshot the buffer ---
        with self._audio_lock:
            self._capture_active = False
            if self._capture_buffer:
                captured = np.concatenate(self._capture_buffer)
            else:
                captured = np.zeros(0, dtype=np.int16)
            self._capture_buffer = []

        capture_seconds = captured.shape[0] / sample_rate
        self.get_logger().info(
            f"Capture done: {captured.shape[0]} samples "
            f"({capture_seconds:.2f}s, preroll_prepended={preroll_total})")

        # Refuse to call whisper on near-empty captures (would return '').
        min_samples = int(0.3 * sample_rate)
        if captured.shape[0] < min_samples:
            return self._fail(
                goal_handle,
                f"Captured only {capture_seconds:.2f}s of audio "
                f"(min {min_samples / sample_rate:.2f}s required)")

        # --- Optionally save the clip before transcribing so it exists even ---
        # --- if whisper fails. ---
        clip_path = ''
        if self.save_audio_clips:
            try:
                clip_path = self._write_clip(captured, sample_rate)
            except Exception as e:
                self.get_logger().warn(f"Failed to save audio clip: {e}")

        # --- Transcribe ---
        try:
            audio_f32 = captured.astype(np.float32) / 32768.0
            transcribe_start = time.monotonic()
            segments_iter, info = self._model.transcribe(
                audio_f32,
                language=language,
                beam_size=self.beam_size,
                initial_prompt=self.initial_prompt,
                without_timestamps=True,
            )
            segments = list(segments_iter)  # consume generator
            transcribe_elapsed = time.monotonic() - transcribe_start
        except Exception as e:
            return self._fail(
                goal_handle, f"Whisper transcription failed: {e}",
                duration_seconds=capture_seconds,
                audio_clip_path=clip_path)

        transcript = ' '.join(s.text.strip() for s in segments).strip()
        confidence = self._weighted_confidence(segments)

        self.get_logger().info(
            f"Transcribed in {transcribe_elapsed:.2f}s "
            f"({capture_seconds / max(transcribe_elapsed, 1e-6):.1f}× "
            f"real-time, conf={confidence:.2f}): {transcript!r}")

        # --- Result ---
        result = Transcribe.Result()
        result.transcript = transcript
        result.confidence = float(confidence)
        result.duration = self._secs_to_duration(capture_seconds)
        result.audio_clip_path = clip_path
        result.error_message = ''

        if canceled:
            result.success = False
            goal_handle.canceled()
        else:
            result.success = True
            goal_handle.succeed()
        return result

    # ----------------------------------------------------------- feedback
    def _publish_feedback(self, goal_handle) -> None:
        """Compute rms_level from the last ~3 frames and publish feedback."""
        with self._audio_lock:
            if not self._capture_buffer:
                recent = np.zeros(1, dtype=np.int16)
            else:
                recent = np.concatenate(self._capture_buffer[-3:])

        if recent.size > 0:
            recent_f = recent.astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(recent_f * recent_f)))
        else:
            rms = 0.0

        fb = Transcribe.Feedback()
        fb.partial_transcript = ''     # Phase 1: no streaming partials
        fb.rms_level = rms
        fb.speech_active = False       # Phase 1: no VAD
        goal_handle.publish_feedback(fb)

    # ----------------------------------------------------------- helpers
    def _weighted_confidence(self, segments) -> float:
        """Duration-weighted exp(avg_logprob) across segments, in [0, 1]."""
        total_dur = 0.0
        conf_sum = 0.0
        for s in segments:
            dur = max(0.0, getattr(s, 'end', 0.0) - getattr(s, 'start', 0.0))
            logprob = getattr(s, 'avg_logprob', None)
            if logprob is None or dur <= 0:
                continue
            conf_sum += math.exp(logprob) * dur
            total_dur += dur
        return conf_sum / total_dur if total_dur > 0 else 0.0

    def _write_clip(self, samples: np.ndarray, sample_rate: int) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        path = self.audio_clip_dir / f"utterance_{ts}.wav"
        with wave.open(str(path), 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(samples.tobytes())
        return str(path)

    def _fail(
        self,
        goal_handle,
        message: str,
        *,
        duration_seconds: float = 0.0,
        audio_clip_path: str = '',
    ) -> Transcribe.Result:
        # Ensure capture is stopped on any error path that bypasses the
        # normal recording loop (e.g. early-exit due to no /audio).
        with self._audio_lock:
            self._capture_active = False
            self._capture_buffer = []

        self.get_logger().error(message)
        result = Transcribe.Result()
        result.success = False
        result.transcript = ''
        result.confidence = 0.0
        result.duration = self._secs_to_duration(duration_seconds)
        result.audio_clip_path = audio_clip_path
        result.error_message = message
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
    node = TranscribeNode()
    # MultiThreadedExecutor so /audio callbacks keep flowing while an action
    # is executing (the execute thread blocks during recording + Whisper).
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
