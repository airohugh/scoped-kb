"""Embedding client for an Ollama server (`/api/embed`).

Address, model, expected dimension and batch size come from the environment
(`SCOPED_KB_OLLAMA_URL`, `SCOPED_KB_EMBED_MODEL`, `SCOPED_KB_EMBED_DIM`,
`SCOPED_KB_EMBED_BATCH`) or from keyword arguments. A vector whose length does not
match the expected dimension is rejected before anything is written to the database.
"""

import json
import os
import urllib.request

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "bge-m3"
DEFAULT_DIM = 1024
DEFAULT_BATCH = 32


class EmbedError(Exception):
    pass


def _setting(env_name, default, cast=str):
    raw = os.environ.get(env_name)
    return cast(raw) if raw else default


def embed(texts, *, url=None, model=None, dim=None, batch_size=None, timeout=600):
    """Return one vector per text, in order."""
    url = url or _setting("SCOPED_KB_OLLAMA_URL", DEFAULT_URL)
    model = model or _setting("SCOPED_KB_EMBED_MODEL", DEFAULT_MODEL)
    dim = dim or _setting("SCOPED_KB_EMBED_DIM", DEFAULT_DIM, int)
    batch_size = batch_size or _setting("SCOPED_KB_EMBED_BATCH", DEFAULT_BATCH, int)

    out = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        req = urllib.request.Request(
            f"{url.rstrip('/')}/api/embed",
            data=json.dumps({"model": model, "input": batch}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            vectors = json.loads(resp.read())["embeddings"]
        if len(vectors) != len(batch):
            raise EmbedError(f"expected {len(batch)} vectors, got {len(vectors)}")
        for v in vectors:
            if len(v) != dim:
                raise EmbedError(
                    f"model {model} returned {len(v)}-dim vectors, expected {dim}; "
                    "refusing to write (changing the model means re-embedding the whole table)")
        out.extend(vectors)
    return out
