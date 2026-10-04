"""Character-based, lossless windows; IDs and offsets are reproducible."""

import unicodedata
from uuid import NAMESPACE_URL, uuid5

from opspilot.domain import Chunk, Document, InvalidInput

CHUNK_SIZE = 1200
OVERLAP = 200
MAX_CONTENT = 100_000
MAX_CHUNKS = 100


def normalize_content(content: str) -> str:
    text = unicodedata.normalize("NFC", content.replace("\r\n", "\n").replace("\r", "\n"))
    text = text.strip()
    if not text or len(text) > MAX_CONTENT or "\x00" in text:
        raise InvalidInput
    return text


def split_document(document: Document) -> list[Chunk]:
    chunks: list[Chunk] = []
    start = 0
    while start < len(document.content):
        end = min(start + CHUNK_SIZE, len(document.content))
        ordinal = len(chunks)
        chunks.append(
            Chunk(
                id=uuid5(NAMESPACE_URL, f"{document.id}:char-v1:{ordinal}"),
                document_id=document.id,
                tenant_id=document.tenant_id,
                ordinal=ordinal,
                text=document.content[start:end],
                start=start,
                end=end,
                title=document.metadata.title,
                source=document.metadata.source,
            )
        )
        if end == len(document.content):
            break
        start = end - OVERLAP
    if not chunks or len(chunks) > MAX_CHUNKS:
        raise InvalidInput
    return chunks
