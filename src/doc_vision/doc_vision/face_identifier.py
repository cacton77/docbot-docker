"""
In-session face identity tracker using FaceNet (InceptionResnetV1, VGGFace2).

Pipeline per frame:
  1. Embed each YOLO-cropped face with FaceNet (512-d unit-norm vector).
  2. Compare against per-user running-mean embeddings via cosine similarity.
  3. Greedy assignment: highest-similarity face-user pairs are resolved first.
  4. Faces with no match above the threshold become new users.

match_embeddings() accepts pre-computed unit-norm embeddings (e.g. from
InsightFace's ArcFace model) and skips step 1, so this class can be shared
between the YOLO+FaceNet node and the InsightFace node.

Note: canonical face recognition adds an alignment step between detection and
embedding (affine-warp to a standard pose using 5 facial landmarks). We omit
it in identify_batch() because YOLO crops are typically frontal at conversational
distance. match_embeddings() receives aligned crops from InsightFace already.
"""

import os
import threading

import cv2
import numpy as np

# Cache FaceNet weights in the persistent volume so they survive container restarts.
os.environ.setdefault("TORCH_HOME", "/models")


class FaceIdentifier:
    """Thread-safe, in-session face identity store."""

    def __init__(self, similarity_threshold: float = 0.75, device: str = "cpu"):
        self._threshold = similarity_threshold
        self._device    = device
        self._model     = None
        self._lock      = threading.Lock()

        self._embeddings: dict = {}  # user_id (int) -> unit-norm mean embedding (Tensor)
        self._counts:     dict = {}  # user_id (int) -> sample count (int)
        self._next_id           = 1

    # ── Model loading ──────────────────────────────────────────────────────

    def load_model(self) -> bool:
        """Load InceptionResnetV1 (VGGFace2). Safe to call from any thread."""
        try:
            from facenet_pytorch import InceptionResnetV1
            model = InceptionResnetV1(pretrained="vggface2").eval().to(self._device)
            with self._lock:
                self._model = model
            return True
        except Exception:
            return False

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._model is not None

    # ── Embedding ──────────────────────────────────────────────────────────

    def _embed(self, model, crop_rgb: np.ndarray):
        """
        512-d unit-norm embedding for one RGB numpy crop.
        Caller must NOT hold self._lock.
        """
        import torch
        img = cv2.resize(crop_rgb, (160, 160))
        t = (
            torch.from_numpy(img).permute(2, 0, 1)
            .float().div_(127.5).sub_(1.0)
            .unsqueeze(0).to(self._device)
        )
        with torch.no_grad():
            return self._model(t).squeeze(0)  # (512,)

    # ── Core matching ──────────────────────────────────────────────────────

    def _match(self, embs: list) -> list:
        """
        Assign user IDs to a list of embedding tensors (all on self._device).
        Caller MUST hold self._lock.
        """
        import torch
        import torch.nn.functional as F

        user_ids = list(self._embeddings.keys())
        assigned = [None] * len(embs)
        used:     set = set()

        if user_ids:
            ref = torch.stack([self._embeddings[u] for u in user_ids])  # (M, 512)
            fac = torch.stack(embs)                                      # (N, 512)
            sim = F.cosine_similarity(
                fac.unsqueeze(1), ref.unsqueeze(0), dim=2
            )  # (N, M)
        else:
            sim = None

        # Resolve highest-confidence matches first to reduce greedy errors.
        order = (
            sorted(range(len(embs)),
                   key=lambda i: float(sim[i].max()), reverse=True)
            if sim is not None
            else list(range(len(embs)))
        )

        for fi in order:
            matched = None
            if sim is not None:
                for mi in sim[fi].argsort(descending=True).tolist():
                    uid = user_ids[mi]
                    if uid in used:
                        continue
                    if float(sim[fi, mi]) >= self._threshold:
                        matched = uid
                    break  # only consider the best available slot

            if matched is not None:
                assigned[fi] = matched
                used.add(matched)
                n = self._counts[matched]
                new_mean = (self._embeddings[matched] * n + embs[fi]) / (n + 1)
                self._embeddings[matched] = F.normalize(
                    new_mean.unsqueeze(0), dim=1
                ).squeeze(0)
                self._counts[matched] = n + 1
            else:
                uid = self._next_id
                self._next_id += 1
                self._embeddings[uid] = F.normalize(
                    embs[fi].unsqueeze(0), dim=1
                ).squeeze(0)
                self._counts[uid] = 1
                assigned[fi] = uid
                used.add(uid)

        return assigned

    # ── Public identification methods ──────────────────────────────────────

    def identify_batch(self, crops_rgb: list) -> list:
        """
        Embed crops with FaceNet and assign user IDs.
        Returns 0 for all faces if the FaceNet model is not yet loaded.
        """
        with self._lock:
            model = self._model
        if model is None:
            return [0] * len(crops_rgb)

        embs = [self._embed(model, c) for c in crops_rgb]  # GPU inference, no lock

        with self._lock:
            return self._match(embs)

    def match_embeddings(self, embeddings) -> list:
        """
        Assign user IDs using pre-computed unit-norm embeddings.

        Accepts a list of np.ndarray or torch.Tensor (512-d, unit-norm).
        Does NOT require load_model() — use this when an external model
        (e.g. InsightFace ArcFace) already provides the embeddings.
        Returns a list of int user IDs (1-indexed).
        """
        import torch

        embs = []
        for e in embeddings:
            if isinstance(e, np.ndarray):
                t = torch.from_numpy(e.astype(np.float32))
            else:
                t = e.float()
            embs.append(t.to(self._device))

        with self._lock:
            return self._match(embs)
