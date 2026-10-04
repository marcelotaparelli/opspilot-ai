"""Reproducible offline plumbing provider, not a semantic-quality substitute."""

import hashlib
import math
import re

from opspilot.domain import (
    DIMENSIONS,
    EMBEDDING_SPACE_FAKE,
    Chunk,
    GeneratedAnswer,
)


class FakeProvider:
    @property
    def space(self) -> str:
        return EMBEDDING_SPACE_FAKE

    async def embed(self, texts: list[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for text in texts:
            vector = [0.0] * DIMENSIONS
            for token in re.findall(r"\w+", text.casefold()):
                index = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4]) % DIMENSIONS
                vector[index] += 1
            if not any(vector):
                vector[0] = 1
            norm = math.sqrt(sum(value * value for value in vector))
            result.append([value / norm for value in vector])
        return result

    async def answer(self, question: str, chunks: tuple[Chunk, ...]) -> GeneratedAnswer:
        # Extractive evidence cannot execute instructions or change tenant scope.
        return GeneratedAnswer(chunks[0].text, (chunks[0].id,))
