"""Measured, process-local compression scheduling; never part of layout truth.

Online model: byte-weighted EWMA milliseconds/MiB for (initial/edit, mean-object-size
bucket, batch-size bucket, worker count), excluding shared-budget contention.
Probe each admissible count on actual new work, then prefer the
cheapest measured choice; within 10% prefer fewer workers. Re-probe every 32
batches to follow changing workload/load. No synthetic training in the save path.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import math
import os
import threading
import time

MIB = 1024 * 1024
MAX_WORKERS = 8
MODEL_VERSION = "compression-cost-v2"


class PolicyError(ValueError):
    pass


class SchedulingCancelled(RuntimeError):
    pass


def _integer(value, name, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise PolicyError(f"{name} must be an integer {lo}..{hi}")
    try:
        result = int(value)
    except ValueError:
        raise PolicyError(f"{name} must be an integer {lo}..{hi}") from None
    if not lo <= result <= hi:
        raise PolicyError(f"{name} must be an integer {lo}..{hi}")
    return result


@dataclass(frozen=True)
class Policy:
    mode: str
    max_workers: int


def parse_policy(value=None, max_workers=None):
    value = value if value is not None else os.environ.get("VESTIGRAPH_COMPRESS_WORKERS") or "auto"
    limit = max_workers if max_workers is not None else os.environ.get("VESTIGRAPH_COMPRESS_MAX_WORKERS")
    if value in ("auto", "quiet"):
        cap = _integer(limit if limit is not None else 4, "compress max workers", 1, MAX_WORKERS)
        return Policy(value, 1 if value == "quiet" else cap)
    fixed = _integer(value, "compress workers", 1, MAX_WORKERS)
    if limit is not None:
        fixed = min(fixed, _integer(limit, "compress max workers", 1, MAX_WORKERS))
    return Policy("fixed", fixed)


def effective_cpus():
    counter = getattr(os, "process_cpu_count", os.cpu_count)
    return max(1, counter() or 1)


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise SchedulingCancelled("Save cancelled while waiting for compression resources.")


def _bucket(profile, size, count):
    mean = size / max(1, count)
    return ("edit" if profile == "edit" else "initial",
            0 if mean < 4096 else 1 if mean < 65536 else 2,
            0 if size < MIB else 1 if size < 4 * MIB else 2)


class CostModel:
    """Caller holds lock. At most 18 buckets x 8 counts; separate batch-size overhead."""

    def __init__(self):
        self.samples = {}
        self.steps = {}

    def choose(self, key, size, limit):
        candidates = sorted({1, *(w for w in (2, 4, 8) if w <= limit), limit})
        samples = self.samples.get(key, {})
        for workers in candidates:
            if workers not in samples:
                return workers, "calibrating", None
        self.steps[key] = self.steps.get(key, 0) + 1
        if self.steps[key] % 32 == 0:
            workers = candidates[(self.steps[key] // 32 - 1) % len(candidates)]
            return workers, "refreshing_measurement", samples[workers]["ms_per_mib"] * size / MIB
        fastest = min(samples[w]["ms_per_mib"] for w in candidates)
        workers = min(w for w in candidates if samples[w]["ms_per_mib"] <= fastest * 1.10)
        return workers, "measured_cost", samples[workers]["ms_per_mib"] * size / MIB

    def observe(self, key, workers, size, milliseconds):
        if size <= 0 or not math.isfinite(milliseconds) or milliseconds <= 0:
            return
        rate = milliseconds * MIB / size
        samples = self.samples.setdefault(key, {})
        previous = samples.get(workers)
        weight = 1 - 0.7 ** (size / MIB)  # one MiB contributes 30%; tiny tails cannot outweigh full batches
        samples[workers] = {"ms_per_mib": rate if previous is None else (1-weight) * previous["ms_per_mib"] + weight * rate,
                            "samples": 1 if previous is None else previous["samples"] + 1}


@dataclass
class Selection:
    workers: int
    requested_workers: int
    reason: str
    predicted_ms: float | None
    wait_ms: float
    reserved_bytes: int
    key: tuple
    learn: bool
    contention_epoch: int = 0


class Scheduler:
    """One budget for all repositories/windows in this Python service process.

    CPU tokens include inline compression. Memory reservation covers batch input
    and retained outputs, not total RSS or native zlib scratch. Separate service
    processes do not share this budget; no claim of machine-global arbitration.
    """

    def __init__(self, cpu_budget=None, memory_bytes=64 * MIB):
        self.cpu_budget = cpu_budget if cpu_budget is not None else min(MAX_WORKERS, max(1, effective_cpus() - 2))
        self.cpu_budget = _integer(self.cpu_budget, "compress CPU budget", 1, MAX_WORKERS)
        if not isinstance(memory_bytes, int) or isinstance(memory_bytes, bool) or memory_bytes <= 0:
            raise PolicyError("compress memory budget must be positive bytes")
        self.memory_bytes = memory_bytes
        self.condition = threading.Condition()
        self.active_workers = self.active_bytes = self.active_batches = 0
        self.peak_workers = self.peak_bytes = 0
        self.model = CostModel()
        self.contention_epoch = 0
        self.skipped_model_samples = 0

    @contextmanager
    def reserve(self, policy, size, count, *, profile="initial", parallel=True, cancel=None):
        key = _bucket(profile, size, count)
        reserved = size * 2
        started = time.perf_counter()
        with self.condition:
            while True:
                _check_cancel(cancel)
                cpu_free = self.cpu_budget - self.active_workers
                # A single oversized batch may proceed alone; never stack it with another.
                memory_ok = self.active_bytes + reserved <= self.memory_bytes or self.active_batches == 0
                if cpu_free > 0 and memory_ok:
                    break
                self.condition.wait(0.05)
            limit = min(policy.max_workers, self.cpu_budget, max(1, count))
            learn = parallel and policy.mode == "auto"
            if not parallel:
                desired, reason, predicted = 1, "small_batch", None
            elif policy.mode == "auto":
                desired, reason, predicted = self.model.choose(key, size, limit)
            else:
                desired, reason, predicted = limit, policy.mode, None
            workers = min(desired, cpu_free)
            if workers != desired:
                reason, predicted = "shared_budget_limited", None
            if self.active_batches:
                self.contention_epoch += 1
            learn = learn and workers == desired and self.active_batches == 0
            self.active_workers += workers
            self.active_bytes += reserved
            self.active_batches += 1
            self.peak_workers = max(self.peak_workers, self.active_workers)
            self.peak_bytes = max(self.peak_bytes, self.active_bytes)
            selection = Selection(workers, desired, reason, predicted, (time.perf_counter() - started) * 1000,
                                  reserved, key, learn, self.contention_epoch)
        try:
            yield selection
        finally:
            with self.condition:
                self.active_workers -= workers
                self.active_bytes -= reserved
                self.active_batches -= 1
                self.condition.notify_all()

    def observe(self, selection, size, milliseconds):
        if selection.learn:
            with self.condition:
                if selection.contention_epoch == self.contention_epoch:
                    self.model.observe(selection.key, selection.workers, size, milliseconds)
                else:
                    self.skipped_model_samples += 1

    def status(self):
        with self.condition:
            return {"model": MODEL_VERSION, "scope": "service_process", "persistence": "memory_only",
                    "cpu_budget": self.cpu_budget, "memory_reservation_bytes": self.memory_bytes,
                    "active_workers": self.active_workers, "active_reserved_bytes": self.active_bytes,
                    "peak_workers": self.peak_workers, "peak_reserved_bytes": self.peak_bytes,
                    "trained_buckets": len(self.model.samples),
                    "skipped_contended_samples": self.skipped_model_samples,
                    "observations": sum(s["samples"] for bucket in self.model.samples.values() for s in bucket.values()),
                    "costs": [{"profile": key[0], "size_bucket": key[1],
                               "batch_size_bucket": key[2] if len(key) > 2 else None,
                               "workers": {str(w): dict(sample) for w, sample in sorted(samples.items())}}
                              for key, samples in sorted(self.model.samples.items())],
                    "gpu": {"implemented": False, "calibrated": False, "eligible": False,
                            "reason": "GPU compression backend and transfer-inclusive calibration are not implemented"}}


_scheduler = None
_init_lock = threading.Lock()


def get_scheduler():
    global _scheduler
    with _init_lock:
        if _scheduler is None:
            cpu = os.environ.get("VESTIGRAPH_COMPRESS_CPU_BUDGET")
            memory = _integer(os.environ.get("VESTIGRAPH_COMPRESS_MEMORY_MIB") or 64,
                              "compress memory MiB", 16, 1024) * MIB
            _scheduler = Scheduler(cpu_budget=cpu, memory_bytes=memory)
        return _scheduler


def status():
    try:
        policy = parse_policy()
        return dict(get_scheduler().status(), mode=policy.mode, max_workers=policy.max_workers)
    except PolicyError as exc:
        return {"model": MODEL_VERSION, "mode": "invalid", "error": str(exc),
                "gpu": {"implemented": False, "calibrated": False, "eligible": False}}
