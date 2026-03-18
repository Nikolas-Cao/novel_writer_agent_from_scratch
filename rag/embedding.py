"""
本地嵌入函数（无网络）：用于 Chroma 持久化索引。
"""
import hashlib
import math
import re
from typing import List

from chromadb.api.types import Documents, EmbeddingFunction, Embeddings


class LocalHashEmbeddingFunction(EmbeddingFunction[Documents]):
    """基于 token hash 的轻量本地 embedding。"""

    def __init__(self, dim: int = 128) -> None:
        self.dim = dim

    def _embed_text(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        tokens = re.findall(r"\w+|[^\w\s]", text.lower())
        if not tokens:
            return vec

        for token in tokens:
            digest = hashlib.md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], byteorder="little") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            weight = 1.0 + (digest[5] / 255.0)
            vec[idx] += sign * weight

        norm = math.sqrt(sum(v * v for v in vec))
        if norm <= 1e-9:
            return vec
        return [v / norm for v in vec]

    def __call__(self, input: Documents) -> Embeddings:
        return [self._embed_text(text) for text in input]
