from abc import ABC, abstractmethod
from dataclasses import dataclass
import hashlib
import json
import re
from pathlib import Path
from time import perf_counter

import numpy as np

from easy_writing.storage.database import StorageError


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    text: str
    token_count: int


class EmbeddingProvider(ABC):
    model_name: str
    revision: str
    dimension: int
    max_tokens: int

    @abstractmethod
    def count_tokens(self, text: str, *, query=False) -> int: ...

    @abstractmethod
    def encode(self, texts: list[str], *, query=False) -> np.ndarray: ...

    @property
    def metadata(self):
        return {"model_name": self.model_name, "revision": self.revision,
                "dimension": self.dimension, "max_tokens": self.max_tokens,
                "split_policy": "binary-character-window-no-truncation-v1", "normalized": True}

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.metadata, sort_keys=True).encode()).hexdigest()

    def split(self, text: str) -> list[Segment]:
        if not text.strip():
            return []
        segments, start = [], 0
        while start < len(text):
            low, high = start + 1, len(text)
            if self.count_tokens(text[start:low]) > self.max_tokens:
                raise StorageError("嵌入模型无法容纳单个字符及特殊 token。")
            end = low
            while low <= high:
                mid = (low + high) // 2
                if self.count_tokens(text[start:mid]) <= self.max_tokens:
                    end, low = mid, mid + 1
                else:
                    high = mid - 1
            segment = text[start:end]
            tokens = self.count_tokens(segment)
            segments.append(Segment(start, end, segment, tokens))
            start = end
        return segments

    def checked_encode(self, texts, *, query=False):
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        if any(self.count_tokens(text, query=query) > self.max_tokens for text in texts):
            raise StorageError("嵌入输入超过模型上限；文档须切分，过长查询须缩短。")
        matrix = np.asarray(self.encode(texts, query=query), dtype=np.float32)
        if matrix.shape != (len(texts), self.dimension) or not np.isfinite(matrix).all():
            raise StorageError("嵌入结果的维度或数值无效。")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if (norms <= 1e-12).any():
            raise StorageError("嵌入模型返回零向量。")
        return matrix / norms


class FakeEmbeddingProvider(EmbeddingProvider):
    """仅用于流程测试的字符哈希向量，不能作为真实语义检索评测。"""

    model_name, revision, dimension, max_tokens = "fake-char-hash", "v1", 64, 128

    def count_tokens(self, text, *, query=False):
        return len(text) + 2

    def encode(self, texts, *, query=False):
        rows = []
        for text in texts:
            row = np.zeros(self.dimension, dtype=np.float32)
            for term in re.findall(r"[\w]", text):
                index = int.from_bytes(hashlib.sha256(term.encode()).digest()[:4], "little") % self.dimension
                row[index] += 1
            if not row.any():
                row[0] = 1
            rows.append(row)
        return np.asarray(rows)


class BGEEmbeddingProvider(EmbeddingProvider):
    query_prefix = "为这个句子生成表示以用于检索相关文章："

    def __init__(self, model_name="BAAI/bge-small-zh-v1.5", *, cache_folder=".cache/models", revision=None):
        started = perf_counter()
        try:
            if Path(model_name).is_dir():
                root = Path(model_name)
                if not (root / "config.json").is_file() or not (list(root.glob("*.safetensors")) or list(root.glob("pytorch_model*.bin"))):
                    raise ValueError("本地 BGE 目录缺少配置或权重")
                hasher = hashlib.sha256()
                for path in sorted(Path(model_name).rglob("*")):
                    relative = path.relative_to(model_name)
                    if path.is_file() and path.suffix in (".json", ".txt", ".safetensors", ".bin") and not any(part.startswith(".") for part in relative.parts):
                        hasher.update(path.relative_to(model_name).as_posix().encode())
                        with path.open("rb") as stream:
                            for chunk in iter(lambda: stream.read(1024*1024), b""):
                                hasher.update(chunk)
                resolved = "local-" + hasher.hexdigest()
                load_revision = None
            else:
                ref = Path(cache_folder) / ("models--" + model_name.replace("/", "--")) / "refs" / (revision or "main")
                cached = ref.read_text().strip() if ref.is_file() else ""
                if revision and re.fullmatch(r"[0-9a-f]{40}", revision):
                    resolved = revision
                elif re.fullmatch(r"[0-9a-f]{40}", cached):
                    resolved = cached
                else:
                    from huggingface_hub import model_info
                    resolved = model_info(model_name, revision=revision or "main", token=False).sha
                load_revision = resolved
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(model_name, device="cpu", cache_folder=str(cache_folder),
                                             revision=load_revision, trust_remote_code=False)
        except ImportError:
            raise StorageError("本地 BGE 需要语义依赖，请安装 pip install -e \".[semantic]\"。") from None
        except Exception:
            raise StorageError("本地 BGE 加载失败，请检查模型缓存、网络与依赖兼容性。") from None
        self.model_name = model_name
        self.revision = resolved
        getter = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
        self.dimension = getter()
        self.max_tokens = min(int(self.model.max_seq_length), 512)
        self.load_ms = round((perf_counter() - started)*1000, 2)

    @property
    def metadata(self):
        return {**super().metadata, "query_prefix": self.query_prefix, "pooling": "sentence-transformers-model-config"}

    def count_tokens(self, text, *, query=False):
        value = self.query_prefix + text if query else text
        return len(self.model.tokenizer(value, add_special_tokens=True, truncation=False, verbose=False)["input_ids"])

    def encode(self, texts, *, query=False):
        texts = [self.query_prefix + text for text in texts] if query else texts
        return self.model.encode(texts, batch_size=32, show_progress_bar=False, normalize_embeddings=True, convert_to_numpy=True)
