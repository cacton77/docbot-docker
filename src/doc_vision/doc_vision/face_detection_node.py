"""
ROS2 face detection node.

Subscribes to a camera image topic, runs YOLO face detection, crops and
scales each detected face to a fixed resolution, and publishes results as
a doc_interfaces/FaceArray on /vision/faces.

Each face is identified against an in-session memory bank (FaceIdentifier)
and assigned a stable user ID stored in Face.track_id (1-indexed; 0 = unknown).

Also publishes all detected face crops concatenated horizontally as a plain
sensor_msgs/Image on /vision/debug_face, sorted by user ID with labels.
"""

import os
import threading

import cv2
import numpy as np

# torchvision was built without CUDA (FORCE_CUDA=0), so its NMS kernel is CPU-only.
# YOLO post-processing passes CUDA tensors to torchvision.ops.nms — redirect them to
# CPU first, then return the result indices back to the original device.
try:
    import torchvision.ops as _tv_ops
    _orig_nms = _tv_ops.nms
    def _nms_cpu(boxes, scores, iou_threshold):
        dev = boxes.device
        return _orig_nms(boxes.cpu(), scores.cpu(), iou_threshold).to(dev)
    _tv_ops.nms = _nms_cpu
except Exception:
    pass

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from sensor_msgs.msg import Image
from cv_bridge import CvBridge

from doc_interfaces.msg import Face, FaceArray

from .face_identifier import FaceIdentifier


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


def _detect_device() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        # On Jetson, cuda.is_available() returns False due to driver version
        # reporting differences between discrete and integrated GPUs, but the
        # GPU is still usable. Probe it directly before falling back to CPU.
        try:
            torch.zeros(1, device="cuda")
            return "cuda"
        except Exception:
            pass
        if torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"


def _find_yolo_weights() -> str:
    # TensorRT .engine files are device-compiled and fastest; check them first.
    # .pt weights are the fallback — ultralytics will auto-download yolov8n.pt if nothing found.
    search_names = (
        "yolov8n-face.engine", "yolov8n-face.pt",
        "yolov8n.engine",      "yolov8n.pt",
        "yolov11n.engine",     "yolov11n.pt",
    )

    try:
        from ament_index_python.packages import get_package_share_directory
        share = get_package_share_directory("doc_vision")
        for name in search_names:
            path = os.path.join(share, "weights", name)
            if os.path.exists(path):
                return path
    except Exception:
        pass

    pkg_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    for name in search_names:
        path = os.path.join(pkg_root, "weights", name)
        if os.path.exists(path):
            return path

    for name in search_names:
        path = os.path.join("/models", name)
        if os.path.exists(path):
            return path

    return "yolov8n.pt"  # ultralytics will auto-download


YOLO_DEVICE  = _detect_device()
YOLO_WEIGHTS = _find_yolo_weights()


class FaceDetectionNode(Node):

    def __init__(self):
        super().__init__("face_detection_node")

        self.declare_parameter("camera_topic",        "/left/image_raw")
        self.declare_parameter("crop_size",           128)
        self.declare_parameter("conf_threshold",      0.4)
        self.declare_parameter("max_faces",           4)
        self.declare_parameter("imgsz",               320)
        self.declare_parameter("similarity_threshold", 0.75)

        camera_topic        = self.get_parameter("camera_topic").value
        self._crop_size     = self.get_parameter("crop_size").value
        self._conf_thr      = self.get_parameter("conf_threshold").value
        self._max_faces     = self.get_parameter("max_faces").value
        self._imgsz         = self.get_parameter("imgsz").value
        self._half          = (YOLO_DEVICE == "cuda")
        sim_threshold       = self.get_parameter("similarity_threshold").value

        self._identifier = FaceIdentifier(
            similarity_threshold=sim_threshold,
            device=YOLO_DEVICE,
        )

        self.yolo     = None
        self._running = True

        self._latest_frame = None
        self._frame_lock   = threading.Lock()
        self._new_frame    = threading.Event()

        self._bridge = CvBridge()
        qos          = QoSPresetProfiles.SENSOR_DATA.value

        self.create_subscription(Image, camera_topic, self._image_cb, qos)

        self._pub_faces = self.create_publisher(FaceArray, "/vision/faces",      qos)
        self._pub_debug = self.create_publisher(Image,     "/vision/debug_face", qos)

        self.get_logger().info(
            f"Subscribed to {camera_topic} | device: {YOLO_DEVICE} | "
            f"weights: {YOLO_WEIGHTS}"
        )

        threading.Thread(target=self._load_models,    daemon=True).start()
        threading.Thread(target=self._inference_loop, daemon=True).start()

    # ── Model loading ─────────────────────────────────────────────────────────

    def _load_models(self):
        try:
            from ultralytics import YOLO
            self.yolo = YOLO(YOLO_WEIGHTS, task="detect")
            self.get_logger().info(f"YOLO loaded: {YOLO_WEIGHTS}")
        except Exception as e:
            self.get_logger().error(f"YOLO load failed: {e}")

        if self._identifier.load_model():
            self.get_logger().info("FaceNet loaded (VGGFace2) — face identification active")
        else:
            self.get_logger().warn(
                "FaceNet load failed — faces will publish with track_id=0. "
                "Install facenet-pytorch: pip install facenet-pytorch"
            )

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

            if self.yolo is None:
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

        results = self.yolo.predict(
            frame,
            device=YOLO_DEVICE,
            verbose=False,
            conf=self._conf_thr,
            imgsz=self._imgsz,
            half=self._half,
        )
        boxes = results[0].boxes

        face_msgs   = []
        crop_arrays = []  # numpy RGB crops kept for debug image

        if boxes is not None and len(boxes) > 0:
            confs   = boxes.conf.cpu().numpy()
            indices = np.argsort(confs)[::-1][: self._max_faces]

            for i in indices:
                x1, y1, x2, y2 = map(int, boxes.xyxy[i].cpu().numpy())
                x1, y1 = max(x1, 0), max(y1, 0)
                x2, y2 = min(x2, w), min(y2, h)
                if x2 <= x1 or y2 <= y1:
                    continue

                crop_bgr = frame[y1:y2, x1:x2]
                crop_bgr = cv2.resize(
                    crop_bgr,
                    (self._crop_size, self._crop_size),
                    interpolation=cv2.INTER_LINEAR,
                )
                crop_rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)

                crop_msg            = self._bridge.cv2_to_imgmsg(crop_rgb, encoding="rgb8")
                crop_msg.header     = header

                face            = Face()
                face.header     = header
                face.track_id   = 0
                face.confidence = float(confs[i])
                face.x          = x1
                face.y          = y1
                face.width      = x2 - x1
                face.height     = y2 - y1
                face.center_x   = float((x1 + x2) / 2)
                face.center_y   = float((y1 + y2) / 2)
                face.crop       = crop_msg
                face_msgs.append(face)
                crop_arrays.append(crop_rgb)

        # Assign user IDs via FaceNet embedding match; sets face.track_id on each message.
        if face_msgs:
            user_ids = self._identifier.identify_batch(crop_arrays)
            for face, uid in zip(face_msgs, user_ids):
                face.track_id = uid

        array              = FaceArray()
        array.header       = header
        array.frame_width  = w
        array.frame_height = h
        array.faces        = face_msgs
        self._pub_faces.publish(array)

        # Debug: annotate each crop with its user label, sort by user ID, concatenate.
        if face_msgs:
            pairs = sorted(zip(face_msgs, crop_arrays), key=lambda p: p[0].track_id)
            annotated = []
            for face, crop in pairs:
                label = f"User {face.track_id}" if face.track_id > 0 else "?"
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
    node = FaceDetectionNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
