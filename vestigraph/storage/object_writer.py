"""Batched object persistence; serialization order and cancellation stay on the coordinator."""
import hashlib
import time

from . import adaptive, policy
from ..vesti_codecs.contract import VestiCodecError, VestiPatchRejected
from .errors import (StorageIOError, StorageError, IntegrityError, CancelledError, DeltaBaseUnavailable, CORRUPT_DATA_ERRORS)
from .packs import (PackWriter, encode_full, HEADER_SIZE,
                    LAYOUT_OBJECT_LIMIT, METADATA_OBJECT_LIMIT)
from .object_access import _chain_root
from .timing import _Progress, _item_clock, _timed_sink

def _compress_task(payload, codecs=None):
    t0 = _item_clock()
    codec, stored = encode_full(payload, policy.FULL_LEVELS, codecs=codecs)
    return codec, stored, (_item_clock() - t0) * 1000


def _compress_if_active(payload, cancel, codecs=None):
    # Queued tasks may be picked up before the coordinator observes cancellation.
    # Check at worker entry too, before starting another non-interruptible zlib call.
    if cancel is not None and cancel.is_set():
        raise CancelledError("Save cancelled by the user; nothing was published.")
    return _compress_task(payload, codecs)

class ObjectWriter:
    """Owns bounded dedupe/compression batches and pack writes; no recipe or cell analysis."""

    def __init__(self, prepared, index, work, monitor=None):
        self.prepared = prepared
        self.codecs = prepared.repo.services.codecs
        self.delta_codec = self.codecs.delta_codec
        self.index = index
        self.work = work
        self.monitor = monitor or _Progress()
        self.pack = PackWriter(prepared.spool)
        prepared.pack = self.pack
        self.metadata_objects = 0
        self.new_objects = 0
        self.new_layout_objects = 0
        self.new_payload = 0
        self._buffer = []                # (hash, payload, verify, context) awaiting a batched existence check
        self._buffer_bytes = 0
        self.delta_on = policy.DELTA_ENABLED and prepared.delta and self.delta_codec is not None and self.delta_codec.encoder_available()
        # P3: which reader checks a fresh patch. 'stdlib' is independent of the encoder; 'native'
        # runs bsdiff4.core.patch on the blocks parse_patch already validated (same checks first,
        # bytes compared after). A native internal failure blocks the commit (IntegrityError),
        # it is never counted as "not in subset".
        self.verify_native = prepared.delta_verify == "native" and self.delta_on
        self.workers = prepared.compress_workers
        self.policy = prepared.compression_policy
        self.scheduler = adaptive.get_scheduler()
        self.compress = {"workers": self.workers, "tasks": 0, "parallel_batches": 0, "inline_batches": 0,
                         "max_in_flight": 0, "max_in_flight_bytes": 0, "max_reserved_bytes": 0,
                         "quota_waits": 0, "wait_polls": 0, "worker_ms": 0.0, "wait_ms": 0.0}
        self.compress.update(mode=self.policy.mode, max_workers=self.policy.max_workers,
                             model=adaptive.MODEL_VERSION, backend="cpu", worker_histogram={},
                             reason_counts={}, effective_workers=0, budget_wait_ms=0.0, last_decision=None,
                             last_model_decision=None)
        self.delta = {"attempts": 0, "adopted": 0, "saved_bytes": 0,
                      "rejected": {"no_candidate": 0, "no_saving": 0, "base_unreadable": 0,
                                   "verify_failed": 0, "not_in_subset": 0}}

    # ---- object sink ----
    @_timed_sink
    def put(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        self.metadata_objects += 1
        self._enqueue(digest, payload, verify=True)
        return digest


    def _enqueue(self, digest, payload, verify=False, context=None):
        self._buffer.append((digest, payload, verify, context))
        self._buffer_bytes += len(payload)
        if len(self._buffer) >= policy.DEDUPE_BATCH or self._buffer_bytes >= policy.DEDUPE_BATCH_BYTES:
            self.flush()

    @_timed_sink
    def flush(self):
        if not self._buffer:
            return
        self.monitor.report()                      # also the cancellation point between batches
        t0 = _item_clock()
        digests = list({d for d, _, _, _ in self._buffer})
        existing = self.index.exists_many(digests)
        marks = ",".join("?" * len(digests))
        for row in self.work.execute(f"SELECT hash FROM pending WHERE hash IN ({marks})", digests):
            existing.add(row[0])
        self.monitor.timing["dedupe_query_ms"] += (_item_clock() - t0) * 1000
        todo = []                                     # new objects, in buffer order
        for digest, payload, verify, context in self._buffer:
            repair = 0
            if digest in existing:
                if not verify and not self.prepared.verify_existing_layout:
                    continue
                # Metadata pages are small and every version depends on them: a hit is read back,
                # and a damaged copy is replaced (the index row is repointed at the fresh copy).
                try:
                    self.index.get(digest, METADATA_OBJECT_LIMIT if verify else LAYOUT_OBJECT_LIMIT)
                    continue
                except (CancelledError, StorageIOError):
                    raise
                except CORRUPT_DATA_ERRORS:
                    repair = 1
            existing.add(digest)                      # the same hash twice in one buffer
            todo.append((digest, payload, context, repair, verify))
        if todo:
            self._encode_batch(todo)
        self.pack.rows.clear()
        self._buffer, self._buffer_bytes = [], 0

    def _encode_batch(self, todo):
        size = sum(len(item[1]) for item in todo)
        parallel = len(todo) >= policy.COMPRESS_MIN_PARALLEL and size >= policy.COMPRESS_MIN_PARALLEL_BYTES
        try:
            with self.scheduler.reserve(self.policy, size, len(todo), profile=self.prepared.compression_profile,
                                        parallel=parallel, cancel=self.monitor.cancel) as selected:
                self.workers = selected.workers
                self.compress["effective_workers"] = max(self.compress["effective_workers"], self.workers)
                if self.policy.mode != "fixed":
                    self.compress["workers"] = max(self.compress["workers"], self.workers)
                histogram = self.compress["worker_histogram"]
                histogram[str(self.workers)] = histogram.get(str(self.workers), 0) + 1
                reasons = self.compress["reason_counts"]
                reasons[selected.reason] = reasons.get(selected.reason, 0) + 1
                self.compress["budget_wait_ms"] += selected.wait_ms
                decision = {"workers": self.workers, "requested_workers": selected.requested_workers,
                            "reason": selected.reason, "predicted_ms": selected.predicted_ms,
                            "input_bytes": size, "objects": len(todo), "reserved_bytes": selected.reserved_bytes}
                self.compress["last_decision"] = decision
                self.monitor.report(compression=decision)
                started = time.perf_counter()
                if parallel and self.workers > 1:
                    self.compress["parallel_batches"] += 1
                    self._write_batch_parallel(todo)
                else:
                    self.compress["inline_batches"] += 1
                    for digest, payload, context, repair, verify in todo:
                        self._write_new(digest, payload, context, repair)
                        if not verify:
                            self.new_layout_objects += 1
                elapsed = (time.perf_counter() - started) * 1000
                decision["observed_ms"] = elapsed
                if selected.learn:
                    self.compress["last_model_decision"] = dict(decision)
                self.scheduler.observe(selected, size, elapsed)
        except adaptive.SchedulingCancelled as exc:
            raise CancelledError(str(exc)) from exc

    def _write_batch_parallel(self, todo):
        """Full compression of one batch on a bounded thread pool; everything else stays on this
        thread, in buffer order: delta choice, pack record, pending row. In flight at most
        policy.COMPRESS_LOOKAHEAD_PER_WORKER * workers tasks and policy.COMPRESS_QUOTA_BYTES reserved for
        inputs + retained outputs (one oversized head may run alone). encode_full never
        retains an encoding larger than its input. This is not a total RSS quota: zlib's
        temporary workspace and the existing dedupe batch are separate bounded costs.
        The slowest head item applies back-pressure. A cancel stops submission, waits for the
        calls already running (bounded: one object each) and discards everything; a worker
        exception propagates and the whole prepare is discarded (nothing was published)."""
        from collections import deque
        from concurrent.futures import ThreadPoolExecutor, wait
        cap = policy.COMPRESS_LOOKAHEAD_PER_WORKER * self.workers
        pending = deque()                             # (future, payload_bytes)
        in_flight_bytes = 0
        position = 0
        self.monitor.check()
        pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="vg-compress")
        try:
            for digest, payload, context, repair, verify in todo:
                self.monitor.check()
                # top up: bounded by task count and by payload bytes (the head item always fits)
                while (position < len(todo) and len(pending) < cap
                       and (not pending or 2 * (in_flight_bytes + len(todo[position][1])) <= policy.COMPRESS_QUOTA_BYTES)):
                    self.monitor.check()
                    size = len(todo[position][1])
                    pending.append((pool.submit(_compress_if_active, todo[position][1], self.monitor.cancel, self.codecs), size))
                    in_flight_bytes += size
                    position += 1
                    self.compress["tasks"] += 1
                if position < len(todo) and len(pending) < cap:
                    self.compress["quota_waits"] += 1
                self.compress["max_in_flight"] = max(self.compress["max_in_flight"], len(pending))
                self.compress["max_in_flight_bytes"] = max(self.compress["max_in_flight_bytes"], in_flight_bytes)
                self.compress["max_reserved_bytes"] = max(self.compress["max_reserved_bytes"], 2 * in_flight_bytes)
                future, size = pending[0]
                t0 = _item_clock()
                try:
                    # wait() distinguishes a wait timeout from TimeoutError raised by
                    # a worker. Do not retry a failed worker forever as if it were busy.
                    while not future.done():
                        self.monitor.check()
                        self.compress["wait_polls"] += 1
                        wait((future,), timeout=policy.COMPRESS_CANCEL_POLL_SECONDS)
                    self.monitor.check()
                    codec, stored, worker_ms = future.result()
                except (CancelledError, StorageIOError):
                    raise
                except Exception as exc:  # noqa: BLE001 - a worker fault aborts the whole prepare
                    raise StorageError("full compression worker failed (%s: %s); nothing was published"
                                       % (type(exc).__name__, exc)) from exc
                waited = (_item_clock() - t0) * 1000
                self.monitor.timing["full_compress_ms"] += waited
                self.compress["wait_ms"] += waited
                self.compress["worker_ms"] += worker_ms
                pending.popleft()
                in_flight_bytes -= size
                self.monitor.check()
                self._write_new(digest, payload, context, repair, encoded=(codec, stored))
                if not verify:
                    self.new_layout_objects += 1
                self.monitor.check()                  # cancel between objects, never mid-write
                del future, stored                   # do not retain an old result while topping up
        finally:
            for future, _ in pending:
                future.cancel()                       # not-yet-started tasks; running ones finish
            pool.shutdown(wait=True, cancel_futures=True)

    def _write_new(self, digest, payload, context, repair, encoded=None):
        self.monitor.check()
        if encoded is None:
            t0 = _item_clock()
            try:
                codec, stored = encode_full(payload, policy.FULL_LEVELS, codecs=self.codecs)
            except VestiCodecError as exc:
                raise IntegrityError("full codec self-check failed; nothing was published: %s" % exc) from exc
            self.monitor.timing["full_compress_ms"] += (_item_clock() - t0) * 1000
        else:
            codec, stored = encoded
        self.monitor.check()
        depth, base_hash, base_raw = 0, None, 0
        if self.delta_on and context is not None and self.codecs.delta_selection.eligible(len(payload), len(stored)):
            choice = self._delta_candidate(digest, payload, context, len(stored))
            if choice is not None:
                codec, stored, depth, base_hash, base_raw = choice
        self.monitor.check()
        t0 = _item_clock()
        offset = self.pack.put_record(digest, codec, stored, len(payload), depth, base_hash, base_raw)
        self.monitor.timing["pack_write_ms"] += (_item_clock() - t0) * 1000
        row = self.pack.rows[-1]
        self.work.execute("INSERT OR REPLACE INTO pending VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (digest, offset, row[2], row[3], row[4], row[8], repair, depth, base_hash, base_raw))
        self.new_objects += 1
        self.new_payload += len(payload)

    def _matching_base(self, context, payload_size):
        """Format sinks may match a compatible prior byte region; default has no base."""
        return None

    def _delta_candidate(self, digest, payload, context, full_stored_size):
        """storage-policy-v2a: at most two bases (largest-overlap chunk of the previous version's
        same-name cell, and that chunk's full root), each patch verified with the production reader,
        whole records compared, ties to the full encoding / the shallower chain."""
        best = None
        candidates = []
        rows = self._matching_base(context, len(payload))
        if rows is None:
            self.delta["rejected"]["no_candidate"] += 1
            return None
        first = self.index.row(rows[0])
        if first is None:
            self.delta["rejected"]["no_candidate"] += 1
            return None
        root = _chain_root(self.index, first)
        if root is None:
            raise DeltaBaseUnavailable("delta chain of object %s is broken (cycle, missing base or bad depth); "
                                 "the history index needs repair before more versions are added" % first["hash"])
        if first["depth"] < policy.MAX_DELTA_DEPTH:
            candidates.append(first)
        if root["hash"] != first["hash"]:
            candidates.append(root)
        if not candidates:
            self.delta["rejected"]["no_candidate"] += 1
            return None
        for base_row in candidates:
            self.monitor.check()
            self.delta["attempts"] += 1
            t_base = _item_clock()
            try:
                base = self.index.get(base_row["hash"], LAYOUT_OBJECT_LIMIT)
            except CancelledError:
                raise
            except CORRUPT_DATA_ERRORS as exc:
                self.delta["rejected"]["base_unreadable"] += 1
                raise DeltaBaseUnavailable("delta base %s is unreadable: %s" % (base_row["hash"], exc)) from exc
            t0 = _item_clock()
            self.monitor.timing["base_decode_ms"] += (t0 - t_base) * 1000
            try:
                patch_bytes = self.delta_codec.encode(payload, base=base)
                if not isinstance(patch_bytes, bytes):
                    raise VestiCodecError("Delta encoder must return bytes")
            except VestiPatchRejected:
                # More control triples than the restricted reader accepts: outside the subset,
                # the chunk is stored full (found by the three-way oracle: this used to escape).
                self.delta["rejected"]["not_in_subset"] += 1
                self.monitor.timing["delta_encode_ms"] += (_item_clock() - t0) * 1000
                continue
            except VestiCodecError as exc:
                raise IntegrityError("delta encoder failed; nothing was published: %s" % exc) from exc
            t1 = _item_clock()
            self.monitor.timing["delta_encode_ms"] += (t1 - t0) * 1000
            try:
                restored = self.codecs.registry.decode(self.delta_codec.descriptor.wire_id, patch_bytes,
                                                      len(payload), len(payload), base=base,
                                                      native=self.verify_native)
            except VestiPatchRejected:
                # The encoder produced a patch outside the restricted subset: not adoptable, not a fault.
                self.delta["rejected"]["not_in_subset"] += 1
                self.monitor.timing["delta_verify_ms"] += (_item_clock() - t1) * 1000
                continue
            except VestiCodecError as exc:
                # NativePatchError / NativeUnavailable: the reader itself failed on validated blocks.
                # Stop here: neither a compression miss nor something to retry on another reader.
                self.delta["rejected"]["verify_failed"] += 1
                raise IntegrityError("delta self-check could not run for base %s (%s: %s); nothing was published. "
                                     "Set delta_verify=stdlib or save with delta disabled (delta=False) to keep recording."
                                     % (base_row["hash"], type(exc).__name__, exc)) from exc
            self.monitor.timing["delta_verify_ms"] += (_item_clock() - t1) * 1000
            if restored != payload:
                self.delta["rejected"]["verify_failed"] += 1
                raise IntegrityError("delta self-check failed for base %s: the encoder and the reader disagree; "
                                     "nothing was published" % base_row["hash"])
            record = HEADER_SIZE + len(patch_bytes)
            depth = base_row["depth"] + 1
            key = (record, depth, base_row["hash"])
            if best is None or key < best[0]:
                best = (key, patch_bytes, base_row)
        if best is None:
            return None
        if not self.codecs.delta_selection.accepts(best[0][0], HEADER_SIZE + full_stored_size):
            self.delta["rejected"]["no_saving"] += 1
            return None
        (record, depth, base_hash), patch_bytes, base_row = best
        self.delta["adopted"] += 1
        self.delta["saved_bytes"] += (HEADER_SIZE + full_stored_size) - record
        return self.delta_codec.descriptor.wire_id, patch_bytes, depth, base_hash, base_row["raw_size"]
