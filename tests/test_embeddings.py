import numpy as np
import pytest

from montauk.embeddings.local import DEFAULT_MODEL_NAME, LocalEmbeddingProvider


@pytest.fixture(scope="module")
def provider() -> LocalEmbeddingProvider:
    return LocalEmbeddingProvider()


class TestLocalEmbeddingProvider:
    def test_dimension_matches_model(self, provider):
        assert provider.dimension == 384

    def test_model_name_defaults_as_documented(self, provider):
        assert provider.model_name == DEFAULT_MODEL_NAME

    def test_embed_returns_one_vector_per_text_with_correct_dimension(self, provider):
        vectors = provider.embed(["Homer works at the power plant.", "Marge paints as a hobby."])
        assert len(vectors) == 2
        for v in vectors:
            assert len(v) == provider.dimension
            assert all(isinstance(x, float) for x in v)

    def test_embed_empty_list_returns_empty_list(self, provider):
        assert provider.embed([]) == []

    def test_semantically_similar_texts_score_higher_than_unrelated(self, provider):
        def cosine(a, b):
            a, b = np.array(a), np.array(b)
            return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))

        anchor, similar, unrelated = provider.embed(
            [
                "Works as a robotics engineer at a technology startup.",
                "Employed as a software engineer building robots.",
                "Enjoys baking sourdough bread on weekends.",
            ]
        )
        assert cosine(anchor, similar) > cosine(anchor, unrelated)
