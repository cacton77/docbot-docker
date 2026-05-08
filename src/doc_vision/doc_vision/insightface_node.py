"""
ROS2 face detection node using InsightFace (RetinaFace + ArcFace).

Compared to the YOLO + FaceNet node, this pipeline adds the canonical
face alignment step: InsightFace detects 5 facial keypoints and affine-warps
each face to a standard 112×112 pose before computing the ArcFace embedding.
This improves recognition accuracy for off-angle or tilted faces.

Model pack 'buffalo_sc' (default, recommended for edge devices):
  - Detection:    SCRFD-500M-GNKPS  (~500 K params, very fast)
  - Recognition:  MobileNet ArcFace  (512-d embeddings)

Model pack 'buffalo_l' (higher accuracy, heavier):
  - Detection:    RetinaFace-R50
  - Recognition:  ArcFace-R100

GPU acceleration uses ONNX Runtime's CUDAExecutionProvider when available.
On Jetson, install onnxruntime-gpu to enable it (see Dockerfile).

Publishes:
  /vision/faces       (doc_interfaces/FaceArray)  — with track_id set
  /vision/debug_face  (sensor_msgs/Image)          — aligned crops, sorted by user ID
"""

import os
import threading

import cv2
import numpy as np

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from sensor_msgs.msg import Image
from cv_bridge import CvBridge

from doc_interfaces.msg import Face, FaceArray

from .face_identifier import FaceIdentifier


def _detect_providers() -> list:
    """Return the best available ONNX Runtime execution providers."""
    try:
        import onnxruntime as ort
        available = ort.get_available_providers()
        if "CUDAExecutionProvider" in available:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    except Exception:
        pass
    return ["CPUExecutionProvider"]


def _annotate_crop(crop_rgb: np.ndarray, label: str) -> np.ndarray:
    """Darken the bottom strip of a crop and overlay a centered label."""
    img   = crop_rgb.copy()
    h, w  = img.shape[:2]
    bar_h = max(16, h // 6)
    img[h - bar_h:] = (img[h - bar_h:].astype(np.float32) * 0.4).astype(np.uint8)
    font      = cv2.FONT_HERSHEY_SIMPLEX
    scale     = 0.38
    thickness = 1
    (tw, th), _ = cv2.getTextSize(label, font, scale, thickness)
    tx = max(2, (w - tw) // 2)
    ty = h - max(3, (bar_h - th) // 2)
    cv2.putText(img, label, (tx, ty), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)
    return img


ONNX_PROVIDERS = _detect_providers()


class InsightFaceNode(Node):

    def __init__(self):
        super().__init__("insightface_node")

        self.declare_parameter("camera_topic",         "/left/image_raw")
        self.declare_parameter("crop_size",            128)
        self.declare_parameter("conf_threshold",       0.4)
        self.declare_parameter("max_faces",            4)
        self.declare_parameter("det_size",             320)
        self.declare_parameter("model_pack",           "buffalo_sc")
        self.declare_parameter("similarity_threshold", 0.75)

        camera_topic        = self.get_parameter("camera_topic").value
        self._crop_size     = self.get_parameter("crop_size").value
        self._conf_thr      = self.get_parameter("conf_threshold").value
        self._max_faces     = self.get_parameter("max_faces").value
        self._det_size      = self.get_parameter("det_size").value
        self._model_pack    = self.get_parameter("model_pack").value
        sim_threshold       = self.get_parameter("similarity_threshold").value

        self._app     = None  # insightface.app.FaceAnalysis, loaded in background
        self._running = True

        self._latest_frame = None
        self._frame_lock   = threading.Lock()
        self._new_frame    = threading.Event()

        self._bridge     = CvBridge()
        # InsightFace returns numpy embeddings (CPU); keep FaceIdentifier on CPU too.
        self._identifier = FaceIdentifier(
            similarity_threshold=sim_threshold,
            device="cpu",
        )

        qos = QoSPresetProfiles.SENSOR_DATA.value
        self.create_subscription(Image, camera_topic, self._image_cb, qos)

        self._pub_faces = self.create_publisher(FaceArray, "/vision/faces",      qos)
        self._pub_debug = self.create_publisher(Image,     "/vision/debug_face", qos)

        self.get_logger().info(
            f"Subscribed to {camera_topic} | "
            f"ONNX providers: {ONNX_PROVIDERS} | model: {self._model_pack}"
        )

        threading.Thread(target=self._load_models,    daemon=True).start()
        threading.Thread(target=self._inference_loop, daemon=True).start()

    # ── Model loading ──────────────────────────────────────────────────────

    def _load_models(self):
        try:
            from insightface.app import FaceAnalysis
            ctx_id = 0 if "CUDAExecutionProvider" in ONNX_PROVIDERS else -1
            app = FaceAnalysis(
                name=self._model_pack,
                root="/models/insightface",
                providers=ONNX_PROVIDERS,
            )
            app.prepare(ctx_id=ctx_id, det_size=(self._det_size, self._det_size))
            self._app = app
            self.get_logger().info(
                f"InsightFace loaded: {self._model_pack} | "
                f"det_size: {self._det_size}×{self._det_size} | "
                f"providers: {ONNX_PROVIDERS}"
            )
        except Exception as e:
            self.get_logger().error(f"InsightFace load failed: {e}")

    # ── ROS2 image callback ───────────────────────────────────────────────────

    def _image_cb(self, msg: Image):
        frame = self._bridge.imgmsg_to_cv2(msg, "bgr8")
        with self._frame_lock:
            self._latest_frame = (frame, msg.header)
        self._new_frame.set()

    # ── Inference loop ────────────────────────────────────────────────────────

    def _inference_loop(self):
        while self._running:
            if not self._new_frame.wait(timeout=0.05):
                continue
            self._new_frame.clear()

            if self._app is None:
                continue

            with self._frame_lock:
                if self._latest_frame is None:
                    continue
                frame, header      = self._latest_frame
                self._latest_frame = None

            try:
                self._detect_and_publish(frame, header)
            except Exception as e:
                self.get_logger().error(f"Detection error: {e}")

    # ── Detection + publishing ────────────────────────────────────────────────

    def _detect_and_publish(self, frame: np.ndarray, header):
        h, w = frame.shape[:2]

        # InsightFace expects BGR (OpenCV convention)
        raw_faces = self._app.get(frame)

        # Filter by confidence, take top max_faces sorted by score
        raw_faces = [f for f in raw_faces if float(f.det_score) >= self._conf_thr]
        raw_faces = sorted(raw_faces, key=lambda f: float(f.det_score), reverse=True)
        raw_faces = raw_faces[: self._max_faces]

        face_msgs  = []
        crop_arrays = []
        embeddings  = []

        for face in raw_faces:
            x1, y1, x2, y2 = map(int, face.bbox[:4])
            x1, y1 = max(x1, 0), max(y1, 0)
            x2, y2 = min(x2, w), min(y2, h)
            if x2 <= x1 or y2 <= y1:
                continue

            # Retrieve the unit-norm ArcFace embedding
            emb = getattr(face, "normed_embedding", None)
            if emb is None:
                raw_emb = getattr(face, "embedding", None)
                if raw_emb is not None:
                    norm = np.linalg.norm(raw_emb)
                    emb = raw_emb / norm if norm > 0 else raw_emb

            # Aligned crop: affine-warp to canonical pose using 5 keypoints
            try:
                from insightface.utils import face_align
                aligned_bgr = face_align.norm_crop(
                    frame, landmark=face.kps, image_size=self._crop_size
                )
            except Exception:
                crop_bgr    = frame[y1:y2, x1:x2]
                aligned_bgr = cv2.resize(
                    crop_bgr, (self._crop_size, self._crop_size),
                    interpolation=cv2.INTER_LINEAR,
                )

            aligned_rgb = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2RGB)

            crop_msg        = self._bridge.cv2_to_imgmsg(aligned_rgb, encoding="rgb8")
            crop_msg.header = header

            f_msg            = Face()
            f_msg.header     = header
            f_msg.track_id   = 0  # filled in after identity matching
            f_msg.confidence = float(face.det_score)
            f_msg.x          = x1
            f_msg.y          = y1
            f_msg.width      = x2 - x1
            f_msg.height     = y2 - y1
            f_msg.center_x   = float((x1 + x2) / 2)
            f_msg.center_y   = float((y1 + y2) / 2)
            f_msg.crop       = crop_msg

            face_msgs.append(f_msg)
            crop_arrays.append(aligned_rgb)
            embeddings.append(emb)

        # Assign user IDs using ArcFace embeddings (no FaceNet needed)
        valid_embs = [e for e in embeddings if e is not None]
        if face_msgs and len(valid_embs) == len(face_msgs):
            user_ids = self._identifier.match_embeddings(valid_embs)
            for f_msg, uid in zip(face_msgs, user_ids):
                f_msg.track_id = uid

        array              = FaceArray()
        array.header       = header
        array.frame_width  = w
        array.frame_height = h
        array.faces        = face_msgs
        self._pub_faces.publish(array)

        # Debug: annotate with user label, sort by user ID, concatenate horizontally
        if face_msgs:
            pairs     = sorted(zip(face_msgs, crop_arrays), key=lambda p: p[0].track_id)
            annotated = []
            for f_msg, crop in pairs:
                label = f"User {f_msg.track_id}" if f_msg.track_id > 0 else "?"
                annotated.append(_annotate_crop(crop, label))
            debug_img = np.concatenate(annotated, axis=1)
            debug_msg = self._bridge.cv2_to_imgmsg(debug_img, encoding="rgb8")
            debug_msg.header = header
            self._pub_debug.publish(debug_msg)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def destroy_node(self):
        self._running = False
        self._new_frame.set()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = InsightFaceNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
