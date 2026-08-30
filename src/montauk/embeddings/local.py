"""Default local embedding provider (spec section 20): runs on CPU via
fastembed/ONNX Runtime, no torch, no API token, and no data leaves the
machine at embedding time (the model weights themselves are fetched
from Hugging Face Hub once and cached on disk; see the Dockerfile/README
for pre-baking that into the image for fully offline deployments).
"""

from __future__ import annotations

from fastembed import TextEmbedding

DEFAULT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def _lookup_dimension(model_name: str) -> int:
    for entry in TextEmbedding.list_supported_models():
        if entry["model"] == model_name:
            return int(entry["dim"])
    raise ValueError(
        f"unknown fastembed model {model_name!r}; see TextEmbedding.list_supported_models() for options"
    )


class LocalEmbeddingProvider:
    def __init__(self, model_name: str = DEFAULT_MODEL_NAME):
        self.model_name = model_name
        self._dimension = _lookup_dimension(model_name)
        self._model = TextEmbedding(model_name=model_name)

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [vector.astype("float32").tolist() for vector in self._model.embed(texts)]
