"""Where the vector index and the BGE weights live.

This module exists because the two paths were defined twice, in two places,
and they disagreed — and the disagreement was silent. The index builder
looked for the model under ``models/`` while the searcher looked under
``data/memory/models/``. A user who followed ``models/README.md`` could build
an index, then have the searcher report no model, and never learn that the two
had never looked in the same place.

Worse than a missing model: build with one copy of BGE and search with another
and the vectors are silently incomparable. Retrieval then returns confident
nonsense, which no assertion catches.

The split is by nature, not by accident:

* ``models/`` holds the weights. They ship with the product, are large, and
  are covered by ``models/*`` in .gitignore. Putting them under ``data/``
  would mean a user wiping runtime state loses the model.
* ``data/memory/cache/`` holds the index. It is derived, per-machine, and
  belongs with the other runtime state.

Both resolve through environment variables, which is the only supported way to
point either at a different location — a network share, a read-only image, a
container volume.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Repository root. This file is ``rpa/memory/paths.py``, so two levels up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: BGE-small-zh-v1.5 weights. Shipped asset, so it lives beside the code's
#: other vendored weights rather than under the gitignored data/ tree.
DEFAULT_MODEL_PATH = PROJECT_ROOT / "models" / "bge-small-zh-v1.5"

#: The dense message index. Derived, per-machine, so it lives with the rest of
#: the runtime cache.
DEFAULT_INDEX_PATH = (
    PROJECT_ROOT / "data" / "memory" / "cache" / "vector_index_dense_messages.pkl"
)

MODEL_PATH_ENV = "WECHAT_BGE_MODEL_PATH"
INDEX_PATH_ENV = "WECHAT_HISTORY_INDEX_PATH"


def model_path() -> Path:
    """The BGE weights directory, or an explicit override."""
    return Path(os.environ.get(MODEL_PATH_ENV) or DEFAULT_MODEL_PATH)


def index_path() -> Path:
    """The dense message index file, or an explicit override."""
    return Path(os.environ.get(INDEX_PATH_ENV) or DEFAULT_INDEX_PATH)
