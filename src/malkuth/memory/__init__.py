"""Memory Service — context memory spaces and access control.

컨텍스트 메모리 space 와 접근 제어. 저장소 자격증명은 서비스만 보유한다.
"""

from malkuth.memory.embedding import Embedder, HashEmbedder, cosine, normalize, tokenize
from malkuth.memory.entry import MAX_CONTENT_BYTES, MemoryEntry, MemorySource
from malkuth.memory.index import (
    Chunk,
    Hit,
    IndexQueue,
    IndexRegistry,
    SpaceIndex,
    index_error,
    split_chunks,
)
from malkuth.memory.service import (
    SCOPE_PRECEDENCE,
    AccessToken,
    MemoryService,
    MemorySpace,
    access_denied,
    build_token,
)
from malkuth.memory.store import (
    MemoryStore,
    SqliteMemoryStore,
    storage_error,
    validate_entry,
)

__all__ = [
    "MAX_CONTENT_BYTES",
    "SCOPE_PRECEDENCE",
    "AccessToken",
    "Chunk",
    "Embedder",
    "HashEmbedder",
    "Hit",
    "IndexQueue",
    "IndexRegistry",
    "SpaceIndex",
    "MemoryEntry",
    "MemoryService",
    "MemorySource",
    "MemorySpace",
    "MemoryStore",
    "SqliteMemoryStore",
    "access_denied",
    "build_token",
    "cosine",
    "index_error",
    "normalize",
    "split_chunks",
    "storage_error",
    "tokenize",
    "validate_entry",
]
