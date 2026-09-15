"""Storage write policy and bounded-buffer settings; not repository state.

Benchmarks may override these settings in an isolated process. Public callers
continue to use Repository arguments and existing environment variables.
"""
from .packs import MAX_DELTA_DEPTH

STORAGE_POLICY = "storage-policy-v2a"
FULL_LEVELS = (1, 9)               # zlib candidates; benchmarks may patch this
COMPRESS_WORKERS_ENV = "VESTIGRAPH_COMPRESS_WORKERS"
COMPRESS_WORKERS_MAX = 8
COMPRESS_LOOKAHEAD_PER_WORKER = 2           # tasks in flight per worker (spec: at most two)
COMPRESS_QUOTA_BYTES = 32 * 1024 * 1024     # reserved input + retained-result bytes, all workers
COMPRESS_CANCEL_POLL_SECONDS = 0.05         # cooperative wait; cannot interrupt running zlib
COMPRESS_MIN_PARALLEL = 2                   # a batch with fewer new objects stays inline ...
COMPRESS_MIN_PARALLEL_BYTES = 256 * 1024    # ... or with less payload than this (pool cost > gain)
DELTA_ENABLED = True               # policy switch (tests flip it); encoder availability is separate
DECODE_CACHE_BYTES = 32 * 1024 * 1024
SCAN_BACKEND_ENV = "VESTIGRAPH_SCAN_BACKEND"     # auto (default) | python | rust
OPAQUE_CHUNK = 1024 * 1024
INLINE_CHUNKS = 64
INLINE_RUNS_BYTES = 16 * 1024
INLINE_TOP_BYTES = 4 * 1024
ROOT_LIMIT = 256 * 1024
CHANGE_ROOT_LIMIT = 16 * 1024
DEDUPE_BATCH = 256
DEDUPE_BATCH_BYTES = 8 * 1024 * 1024
RUNS_MEMORY_LIMIT = 65536
CHANGESET_ALGORITHM = "gds-record-change-v1"
