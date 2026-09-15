"""Format-2 orchestration: prepare, atomic publish and exact restore.

Repository owns public API/leases. Leaf modules own object IO, scan recipes,
change evidence and timing; none imports this orchestrator. Large object/cell
indices remain in the temporary SQLite work database; existing buffers are unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
import uuid

from . import adaptive, policy, timing
from ..vesti_codecs.contract import VestiCodecError
from .errors import (StorageError, IntegrityError, CancelledError, DeltaBaseUnavailable, CORRUPT_DATA_ERRORS)
from ..vesti_formats.contract import VestiFormatError, VestiScanResult, vesti_validate_fields
from ..vesti_formats.recipe import _iter_recipe
from .metadata import MetadataError, canonical, parse
from .packs import LAYOUT_OBJECT_LIMIT, METADATA_OBJECT_LIMIT
# Keep existing read/type entry points used by Repository and capture consumers.
from .object_access import ObjectIndex
from .timing import _Progress, _TimedStream, _item_clock, _timed_sink, _finish_timing, TIMING_KEYS
from .policy import SCAN_BACKEND_ENV, COMPRESS_WORKERS_ENV

def compress_workers_preference(explicit=None):
    try:
        policy = adaptive.parse_policy(explicit)
    except adaptive.PolicyError as exc:
        raise StorageError(str(exc)) from exc
    return policy.max_workers if policy.mode == "fixed" else policy.mode

def scan_backend_preference(explicit=None):
    """Which scanner to use: an explicit argument, else the environment, else python.
    Python stays the default until the native backend has passed its gates (spec P1 §3.1)."""
    value = explicit or os.environ.get(policy.SCAN_BACKEND_ENV) or "python"
    if value not in ("python", "rust", "auto"):
        raise StorageError("scan backend must be python, rust or auto (got %r)" % value)
    return value


WORK_SCHEMA = """
CREATE TABLE pending (hash TEXT PRIMARY KEY, offset INTEGER NOT NULL, raw_size INTEGER NOT NULL,
                      stored_size INTEGER NOT NULL, codec INTEGER NOT NULL, stored_sha256 TEXT NOT NULL,
                      repair INTEGER NOT NULL DEFAULT 0, depth INTEGER NOT NULL DEFAULT 0,
                      base_hash TEXT, base_raw_size INTEGER NOT NULL DEFAULT 0);
CREATE TABLE seen (hash TEXT PRIMARY KEY);
"""

def _stamp(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _now():
    return datetime.now(timezone.utc).isoformat()

def _unlink_orphan(path):
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logging.getLogger(__name__).warning("Retained temporary orphan %s: %s", path.name, exc)


class PreparedSnapshot:
    """Single-use result of a scan; owns a spooled pack and a work database until commit/discard."""

    def __init__(self, repo, path):
        self.repo = repo
        self.source = path
        self.checkpoint_id = uuid.uuid4().hex
        self.spool = repo.tmp_dir / ("vg2-%s.pack" % self.checkpoint_id)
        self.work_path = repo.tmp_dir / ("vg2-%s.work.sqlite3" % self.checkpoint_id)
        self.state = "open"                 # open -> committed | discarded
        self.lease = None                   # set by Repository when it holds a transient lease for us
        self.on_close = None                # Repository hook: the snapshot no longer blocks other prepares
        self.parent = None
        self.parent_id = None
        self.manifest = None
        self.changeset = None
        self.raw_sha256 = None
        self.normalized_sha256 = None
        self.size = 0
        self.format_analysis = None
        self.top_cells = None
        self.pack = None
        self.work = None                    # sqlite3 connection to the work database
        self.stamp_before = None
        self.stats = {}
        self.recovery = None
        self.verify_existing_layout = False
        self.delta = True                   # policy switch for this snapshot (a rescue save may disable it)
        self.delta_verify = "stdlib"        # P3: reader used for the self-check (stdlib | native)
        self.compress_workers = 1           # P4: full-compression threads per dedupe batch
        self.compression_policy = adaptive.Policy("auto", 4)
        self.compression_profile = "initial"

    @property
    def sha256(self):
        return self.raw_sha256

    def discard(self):
        if self.state != "open":
            return
        self.state = "discarded"
        try:
            self._drop_resources()
        finally:
            self._release()

    def _drop_resources(self):
        if self.pack is not None:
            self.pack.abort()
            self.pack = None
        if self.work is not None:
            self.work.close()
            self.work = None
        _unlink_orphan(self.spool)
        _unlink_orphan(self.work_path)

    def _release(self):
        lease, self.lease = self.lease, None
        if lease is not None:
            lease.release()
        hook, self.on_close = self.on_close, None
        if hook is not None:
            hook(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.discard()
        return False

def _open_work(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA temp_store=FILE")
    db.executescript(WORK_SCHEMA)
    return db


def prepare_snapshot(repo, path, parent, **options):
    """Exactly one recovery attempt; never mask an encoder/self-check failure."""
    original = _stamp(Path(path).stat())
    started = time.perf_counter()
    try:
        return _prepare_snapshot_attempt(repo, path, parent, **options)
    except DeltaBaseUnavailable as exc:
        if _stamp(Path(path).stat()) != original:
            raise StorageError("Source changed before recovery retry; save a stable copy and retry.") from exc
        recovery = {"kind": "delta_base_unreadable", "reason": str(exc)[:500],
                    "prior_attempt_ms": round((time.perf_counter() - started) * 1000, 1),
                    "policy": "delta disabled; every reused object verified/repaired from current source",
                    "old_versions_restorable": "not_verified"}
        return _prepare_snapshot_attempt(repo, path, parent, **{**options, "use_delta": False,
                                           "verify_existing_layout": True, "recovery": recovery})


def _prepare_snapshot_attempt(repo, path, parent, *, analyze=True, use_delta=True, evidence=None,
                     progress=None, cancel=None, scan_backend_pref=None, delta_verify=None,
                     compress_workers=None, compress_max_workers=None, verify_existing_layout=False,
                     recovery=None) -> PreparedSnapshot:
    path = Path(path)
    prepared = PreparedSnapshot(repo, path)
    prepared.delta = bool(use_delta)
    prepared.verify_existing_layout = verify_existing_layout
    prepared.recovery = recovery
    try:
        codec = repo.services.codecs.delta_codec
        prepared.delta_verify = codec.verify_preference(delta_verify) if codec else "stdlib"
    except VestiCodecError as exc:
        raise StorageError(str(exc)) from exc
    try:
        prepared.compression_policy = adaptive.parse_policy(compress_workers, compress_max_workers)
        # Initialize before acquiring resources so configuration errors release the writer lease.
        adaptive.get_scheduler()
    except adaptive.PolicyError as exc:
        raise StorageError(str(exc)) from exc
    prepared.compress_workers = (prepared.compression_policy.max_workers
                                 if prepared.compression_policy.mode == "fixed" else 1)
    prepared.compression_profile = "edit" if parent else "initial"
    preference = scan_backend_preference(scan_backend_pref)
    scan_diag = {"backend": "none"}
    started = time.perf_counter()
    monitor = _Progress(progress, cancel, total=path.stat().st_size)
    prepared.monitor = monitor
    index = ObjectIndex(repo)
    try:
        prepared.parent = parent
        prepared.parent_id = parent["id"] if parent else None
        prepared.work_path.unlink(missing_ok=True)
        path_before = _stamp(path.stat())
        with path.open("rb") as raw_stream:
            before = _stamp(os.fstat(raw_stream.fileno()))
            head = raw_stream.read(repo.services.formats.probe_size)
            raw_stream.seek(0)
            handler = repo.services.formats.storage_for_content(head)
            prepared.format_handler = handler
            work = _open_work(prepared.work_path)
            prepared.work = work
            handler.setup_work(work)
            monitor.phase = "loading_previous"
            monitor.report(force=True)
            t0 = _item_clock()
            prev_loaded = handler.load_previous(index, parent, work)
            if getattr(index, "previous_corruption", None) and not verify_existing_layout:
                raise DeltaBaseUnavailable("previous object index is unreadable: " + index.previous_corruption)
            monitor.timing["load_previous_ms"] = (_item_clock() - t0) * 1000
            monitor.phase = "scanning"
            monitor.report(force=True)
            scan_mark = monitor.window_start()
            stream = _TimedStream(raw_stream, monitor) if timing.TIMING else raw_stream
            sink = handler.create_sink(prepared, index, work, monitor)
            try:
                result, scan_diag = handler.scan(stream, sink, monitor, preference)
            except VestiFormatError as exc:
                # Structural failure alone allows a fresh raw-byte attempt.
                # Cancellation/provider/runtime failures propagate; nothing is published.
                sink.pack.abort()
                prepared.spool.unlink(missing_ok=True)
                work.close()
                prepared.work_path.unlink(missing_ok=True)
                handler = repo.services.formats.opaque_for_content(head, str(exc))
                prepared.format_handler = handler
                work = _open_work(prepared.work_path)
                prepared.work = work
                handler.setup_work(work)
                prev_loaded = handler.load_previous(index, parent, work)
                monitor.bytes = 0
                sink = handler.create_sink(prepared, index, work, monitor)
                raw_stream.seek(0)
                result, scan_diag = handler.scan(stream, sink, monitor, preference)
            after = _stamp(os.fstat(raw_stream.fileno()))
        if not isinstance(result, VestiScanResult):
            raise StorageError("Invalid file-format scan result")
        monitor.window_close("scan", scan_mark)
        if (before != after or path_before != _stamp(path.stat())
                or before[:4] != path_before[:4] or result.size != before[2]):
            raise StorageError("Source changed while being read; pause editing/saving and retry the checkpoint.")
        prepared.stamp_before = path_before
        monitor.phase = "encoding"
        monitor.report(force=True)
        format_fields = vesti_validate_fields(handler.finish_snapshot(sink, result))
        manifest = {
            "format": 2, "storage_policy": policy.STORAGE_POLICY,
            "size": result.size, "sha256": result.raw_sha256,
            "segments": result.segments, **format_fields,
        }
        decoder = repo.services.formats.storage_for_manifest(manifest)
        if decoder.format_id != handler.format_id:
            raise StorageError("File-format provider selected an incompatible persisted reader")
        normalized_sha256 = manifest["normalized_sha256"]
        analysis_info = manifest["format_analysis"]
        prepared.manifest = manifest
        prepared.raw_sha256 = result.raw_sha256
        prepared.normalized_sha256 = normalized_sha256
        prepared.size = result.size
        prepared.format_analysis = analysis_info
        monitor.phase = "analyzing_changes"
        monitor.report(force=True)
        changes_mark = monitor.window_start()
        try:
            prepared.changeset = handler.build_changes(index, parent, prepared, sink, analyze, prev_loaded, evidence)
        except CancelledError:
            raise
        except CORRUPT_DATA_ERRORS as exc:
            # A damaged previous version must not block saving a new one (ValueError covers
            # base64/binascii and JSON decoding faults in a damaged page).
            prepared.changeset = handler.build_changes(index, None, prepared, sink, False, False, evidence)
            prepared.changeset["from_checkpoint_id"] = parent["id"] if parent else None
            prepared.changeset["file"]["before_raw_sha256"] = parent["sha256"] if parent else None
            prepared.changeset["coverage"]["status"] = "unavailable"
            prepared.changeset["coverage"]["reason"] = "previous version index unreadable: %s" % exc
        monitor.window_close("changes", changes_mark)
        sink.flush()
        t0 = _item_clock()
        sink.pack.finish()
        monitor.timing["pack_fsync_ms"] = (_item_clock() - t0) * 1000
        pack_bytes = sink.pack.size if sink.pack.count else 0
        unique_layout = work.execute("SELECT count(*) FROM seen").fetchone()[0]
        manifest["counts"] = {
            "layout_chunk_refs": sink.layout_refs,
            "unique_layout_objects": unique_layout,
            "metadata_objects": sink.metadata_objects,
            "new_objects": sink.new_objects,
            "new_layout_objects": sink.new_layout_objects,
            "new_metadata_objects": sink.new_objects - sink.new_layout_objects,
            "new_pack_bytes": pack_bytes,
        }
        manifest["storage"] = {
            "delta_encoder": sink.delta_codec.encoder_version() if sink.delta_on else None,
            "delta_codec": sink.delta_codec.descriptor.codec_id if sink.delta_codec else None, "delta_max_depth": policy.MAX_DELTA_DEPTH,
            "delta_attempts": sink.delta["attempts"], "delta_adopted": sink.delta["adopted"],
            "delta_saved_bytes": sink.delta["saved_bytes"], "delta_rejected": sink.delta["rejected"],
            "previous_index_loaded": prev_loaded,
        }
        if len(canonical(manifest)) > policy.ROOT_LIMIT:
            raise StorageError("Manifest root exceeds 256 KiB; this file cannot be stored in format 2 (report it).")
        prepared.top_cells = handler.snapshot_top_cells(manifest)
        work.commit()
        total_ms = (time.perf_counter() - started) * 1000
        timing_values = _finish_timing(monitor, total_ms)
        prepared.stats = {"new_objects": sink.new_objects, "new_payload_bytes": sink.new_payload,
                          "new_pack_bytes": pack_bytes, "reused_refs": sink.layout_refs - sink.new_objects,
                          "timing_ms": timing_values, "scan": scan_diag,
                          "delta_verify": ("native" if sink.verify_native else "stdlib") if sink.delta_on else None,
                          "compress": dict(sink.compress),
                          "reads": {"metadata_objects_read": index.metadata_reads, "layout_objects_read": index.layout_reads,
                                    "delta_decodes": index.delta_decodes, "pack_opens": index.reader.opens}}
        if recovery is not None:
            prepared.stats["recovery"] = recovery
        monitor.phase = "prepared"
        monitor.report(force=True)
        return prepared
    except BaseException:
        prepared.discard()
        raise
    finally:
        index.close()

def commit_snapshot(repo, prepared, *, title, source, segment_id, encoded_metadata, filename):
    """Publish the pack, then one SQLite transaction. Returns the checkpoint id."""
    if prepared.state != "open":
        raise StorageError("This prepared snapshot was already %s." % prepared.state)
    if prepared.repo is not repo:
        raise StorageError("Prepared snapshot belongs to a different repository.")
    try:
        if _stamp(prepared.source.stat()) != prepared.stamp_before:
            raise StorageError("Source changed after it was scanned; retry the checkpoint.")
    except BaseException as exc:
        prepared.discard()
        if isinstance(exc, OSError):
            raise StorageError("Source became unavailable after scanning; retry the checkpoint.") from exc
        raise
    pack = prepared.pack
    pack_id = prepared.checkpoint_id
    t_publish = time.perf_counter()
    try:
        prepared.work.close()
        prepared.work = None
        if pack.count:
            target = repo.pack_path(pack_id)
            if target.exists():
                raise StorageError("Pack path already exists; refusing to overwrite.")
            os.replace(prepared.spool, target)
            repo.sync_dir(target.parent)
        else:
            prepared.spool.unlink(missing_ok=True)
        manifest_json = canonical(prepared.manifest).decode("utf-8")
        change_json = canonical(prepared.changeset).decode("utf-8") if prepared.changeset else None
        if change_json is not None and len(change_json.encode("utf-8")) > policy.CHANGE_ROOT_LIMIT:
            raise StorageError("ChangeSet root exceeds 16 KiB; report this file.")
        publish_ms = (time.perf_counter() - t_publish) * 1000
        t_db = time.perf_counter()
        with repo._connect() as db:
            db.execute("ATTACH DATABASE ? AS work", (str(prepared.work_path),))
            try:
                db.execute("BEGIN IMMEDIATE")
                capture_id = getattr(prepared, "capture_queue_id", None)
                if capture_id is None:
                    repo._open_segment(db, segment_id)
                else:
                    from ..capture_pipeline import validate_capture
                    validate_capture(db, repo, capture_id, segment_id, prepared.source, prepared.raw_sha256)
                from .. import history_order
                before_id = getattr(prepared, "history_before", None)
                head = (history_order.predecessor(db, before_id) if before_id is not None
                        else history_order.head(db))
                head_id = head["id"] if head else None
                if head_id != prepared.parent_id:
                    raise StorageError("Another version was committed after this snapshot was prepared; "
                                       "the snapshot is discarded, retry the checkpoint.")
                if pack.count:
                    db.execute("INSERT INTO packs(id, checkpoint_id, size, created_at) VALUES(?,?,?,?)",
                               (pack_id, prepared.checkpoint_id, pack.size, _now()))
                    db.execute("INSERT INTO objects(hash,pack,offset,raw_size,stored_size,codec,depth,"
                               "base_hash,base_raw_size,stored_sha256) "
                               "SELECT hash, ?, offset, raw_size, stored_size, codec, depth, base_hash, base_raw_size, stored_sha256 "
                               "FROM work.pending WHERE repair = 0 ON CONFLICT(hash) DO NOTHING", (pack_id,))
                    db.execute("INSERT INTO objects(hash,pack,offset,raw_size,stored_size,codec,depth,"
                               "base_hash,base_raw_size,stored_sha256) "
                               "SELECT hash, ?, offset, raw_size, stored_size, codec, depth, base_hash, base_raw_size, stored_sha256 "
                               "FROM work.pending WHERE repair = 1 ON CONFLICT(hash) DO UPDATE SET "
                               "pack=excluded.pack, offset=excluded.offset, raw_size=excluded.raw_size, "
                               "stored_size=excluded.stored_size, codec=excluded.codec, depth=excluded.depth, "
                               "base_hash=excluded.base_hash, base_raw_size=excluded.base_raw_size, "
                               "stored_sha256=excluded.stored_sha256", (pack_id,))
                db.execute('''INSERT INTO checkpoints
                    (id,parent_id,segment_id,title,source,created_at,filename,size,sha256,manifest,metadata)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                           (prepared.checkpoint_id, head_id, segment_id, title or filename,
                            source, _now(), filename, prepared.size, prepared.raw_sha256, manifest_json,
                            encoded_metadata))
                history_order.publish(db, prepared.checkpoint_id, before_id,
                                      getattr(prepared, "history_import_key", None), prepared.raw_sha256)
                if change_json is not None:
                    db.execute("INSERT INTO changesets(checkpoint_id, root) VALUES(?,?)",
                               (prepared.checkpoint_id, change_json))
                if capture_id is not None:
                    from ..capture_pipeline import acknowledge_capture
                    acknowledge_capture(db, capture_id, prepared.checkpoint_id,
                                        checkpoint_metadata=json.loads(encoded_metadata))
                db.commit()
            finally:
                try:
                    db.execute("DETACH DATABASE work")
                except sqlite3.Error:
                    pass
        prepared.state = "committed"
        prepared.pack = None
        try:
            prepared.work_path.unlink(missing_ok=True)
        except OSError as exc:
            # The transaction committed: the version IS published. A work file that cannot
            # be removed right now is an orphan for cleanup_orphans, never a failed save
            # (reporting failure here would make the caller retry and publish a duplicate).
            logging.getLogger(__name__).warning("published %s but could not remove %s: %s",
                                                prepared.checkpoint_id, prepared.work_path.name, exc)
        timing = prepared.stats.setdefault("timing_ms", {})
        timing["publish_ms"] = round(publish_ms, 1)
        timing["db_commit_ms"] = round((time.perf_counter() - t_db) * 1000, 1)
        return prepared.checkpoint_id
    except BaseException:
        # A published pack without rows is quarantined by the next lease holder.
        prepared.state = "discarded"
        prepared.pack = None
        _unlink_orphan(prepared.work_path)
        raise
    finally:
        prepared._release()

def export_v2(repo, record, stream):
    """Write the exact original bytes of `record` (a checkpoint row) to `stream`.
    Returns (size, sha256, pack_opens). Raises StorageError on any inconsistency."""
    root = record["manifest"]
    if not isinstance(root, dict) or root.get("format") != 2:
        raise StorageError("Unsupported manifest; use the matching Vestigraph version.")
    if root.get("size") != record["size"] or root.get("sha256") != record["sha256"]:
        raise StorageError("Manifest and checkpoint row disagree; restore metadata from backup.")
    index = ObjectIndex(repo)
    whole = hashlib.sha256()
    total = 0
    try:
        handler = repo.services.formats.storage_for_manifest(root)
        for data in handler.restore_parts(root, index):
            total += len(data)
            if total > root["size"]:
                raise StorageError("Output longer than declared; restore metadata from backup.")
            stream.write(data)
            whole.update(data)
        digest = whole.hexdigest()
        if total != root["size"] or digest != root["sha256"]:
            raise StorageError("File integrity mismatch; restore complete metadata and objects from backup.")
        return total, digest, index.reader.opens
    except (MetadataError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise StorageError("Manifest metadata is corrupt; preserve the history and run an integrity check.") from exc
    finally:
        index.close()
