"""Default local embedding provider (spec section 20): runs on CPU via
fastembed/ONNX Runtime, no torch, no API token, and no data leaves the
machine at embedding time (the model weights themselves are fetched
from Hugging Face Hub once and cached on disk; see the Dockerfile/README
for pre-baking that into the image for fully offline deployments).
"""

from __future__ import annotations

import threading
import time

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
    #: Provider identity (spec section 20). "local" means embeddings are
    #: computed on this machine and no relationship data leaves it.
    provider = "local"

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


# A server process shares one provider: loading the ONNX model costs ~1s
# and ~100 MB, and the first load may download the weights. A failed load
# is not retried for _RETRY_AFTER_SECONDS so a missing model (no network,
# unwritable cache) costs each request an immediate lexical fallback, not
# a fresh download timeout.
_RETRY_AFTER_SECONDS = 300.0
_default: LocalEmbeddingProvider | None = None
_last_failure: tuple[float, str] | None = None
_lock = threading.Lock()


class EmbeddingProviderUnavailable(RuntimeError):
    pass


def default_provider() -> LocalEmbeddingProvider:
    """The process-wide local provider, loaded on first use."""
    global _default, _last_failure
    with _lock:
        if _default is not None:
            return _default
        if _last_failure is not None and time.monotonic() - _last_failure[0] < _RETRY_AFTER_SECONDS:
            raise EmbeddingProviderUnavailable(_last_failure[1])
        try:
            _default = LocalEmbeddingProvider()
        except Exception as exc:
            _last_failure = (time.monotonic(), f"{type(exc).__name__}: {exc}")
            raise EmbeddingProviderUnavailable(_last_failure[1]) from exc
        _last_failure = None
        return _default
