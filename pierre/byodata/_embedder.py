"""Live Gemini embedder for ``rank_upload``: L2-normalised 768-dim vectors."""
from __future__ import annotations

import math
from typing import List, Sequence

from pierre.config import GEMINI_API_KEY, GEMINI_EMBEDDING_MODEL

_DIMENSIONS = 768
_BATCH_SIZE = 100


def _normalize(vector: List[float]) -> List[float]:
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


class GeminiEmbedder:
    def __init__(self, api_key: str = GEMINI_API_KEY, model: str = GEMINI_EMBEDDING_MODEL):
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model

    def _embed(self, texts: Sequence[str], task_type: str) -> List[List[float]]:
        from google.genai import types

        out: List[List[float]] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            response = self._client.models.embed_content(
                model=self._model,
                contents=list(texts[start:start + _BATCH_SIZE]),
                config=types.EmbedContentConfig(
                    task_type=task_type, output_dimensionality=_DIMENSIONS),
            )
            out.extend(_normalize(list(e.values)) for e in response.embeddings)
        return out

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        return self._embed(texts, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> List[float]:
        return self._embed([text], "RETRIEVAL_QUERY")[0]
