// vestigraph_scan_core -- bounded Rust backend for Vestigraph's strict GDS record scan.
//
// The scan itself lives in `scan.rs` (pure Rust, unit-tested, a byte-for-byte port of
// `vestigraph/vesti_formats/vesti_format_gds/scan.py`). This file is only the PyO3 session layer described in
// docs/SPEC_PERFORMANCE_READ_ACCESS.md P1 §3.2: bounded input blocks in, bounded event batches
// out, the GIL released around every compute, input copied into Rust-owned memory before the
// release, and a lock-free cancel flag checked between records.
//
// PyO3 0.27.2 notes: `Python::detach` is the renamed `allow_threads`; abi3 forbids
// `#[pyclass(extends = PyException)]`, so the two exception types come from `create_exception!`
// and `GdsStructureError` carries `.args == (reason, offset)` plus `.reason` / `.offset` attrs.
#![allow(clippy::too_many_arguments)]

mod scan;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use pyo3::exceptions::{PyException, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use pyo3::{create_exception, wrap_pyfunction};

use scan::{Event, Kind, Params, ScanError, ScanErrorKind, ScanResult};

pub const BACKEND_ID: &str = "scan_core/0.1.2";

create_exception!(
    vestigraph_scan_core,
    GdsStructureError,
    PyException,
    "Strict GDS2 framing violation with the same reason text and byte offset as \
     vestigraph.vesti_formats.vesti_format_gds.scan.GdsStructureError. `.args == (reason, offset)`; \
     `.reason` / `.offset` are also set as attributes."
);

create_exception!(
    vestigraph_scan_core,
    ScanCancelled,
    PyException,
    "Raised by Scanner.feed()/finish() after Scanner.cancel(); checked between records \
     inside a feed, not only at call boundaries."
);

fn gds_structure_error(py: Python<'_>, reason: &str, offset: u64) -> PyErr {
    let err = PyErr::new::<GdsStructureError, _>((reason.to_string(), offset));
    let value = err.value(py);
    let _ = value.setattr("reason", reason);
    let _ = value.setattr("offset", offset);
    err
}

fn map_error(py: Python<'_>, err: ScanError) -> PyErr {
    match err.kind {
        ScanErrorKind::Cancelled => ScanCancelled::new_err("scan cancelled during feed()"),
        ScanErrorKind::Runtime => PyRuntimeError::new_err(err.reason),
        ScanErrorKind::Structure => gds_structure_error(py, &err.reason, err.offset),
    }
}

/// The error to raise, carrying the events queued before the failure as `pending_events`
/// (a list of the usual event dicts). The adapter replays them onto the sink before it
/// re-raises, so a failing scan delivers exactly the prefix the Python reference delivers.
fn error_with_pending(py: Python<'_>, err: ScanError, pending: Vec<Event>) -> PyResult<PyErr> {
    let exc = map_error(py, err);
    let list = PyList::empty(py);
    for ev in pending {
        list.append(event_to_py(py, ev)?)?;
    }
    exc.value(py).setattr("pending_events", list)?;
    Ok(exc)
}

fn hex(digest: &[u8]) -> String {
    let mut s = String::with_capacity(digest.len() * 2);
    for b in digest {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

/// One `Event` -> the dict shape documented on `Scanner.feed`.
fn event_to_py<'py>(py: Python<'py>, ev: Event) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    match ev {
        Event::Chunk {
            digest,
            data,
            name,
            offset,
        } => {
            d.set_item("t", "chunk")?;
            d.set_item("digest", hex(&digest))?;
            d.set_item("data", PyBytes::new(py, &data))?;
            match name {
                Some(n) => d.set_item("name", PyBytes::new(py, &n))?,
                None => d.set_item("name", py.None())?,
            }
            match offset {
                Some(o) => d.set_item("offset", o)?,
                None => d.set_item("offset", py.None())?,
            }
        }
        Event::Refs(targets) => {
            d.set_item("t", "refs")?;
            let list = PyList::empty(py);
            for t in targets {
                list.append(PyBytes::new(py, &t))?;
            }
            d.set_item("targets", list)?;
        }
        Event::Segment {
            entry,
            chunks,
            timestamp,
        } => {
            d.set_item("t", "segment")?;
            let e = PyDict::new(py);
            e.set_item("kind", entry.kind.as_str())?;
            e.set_item("size", entry.size)?;
            e.set_item("hash", hex(&entry.hash))?;
            if let Some(stamp) = entry.stamp {
                e.set_item("stamp", stamp)?;
            }
            if entry.kind == Kind::LibHead {
                if let Some(units) = entry.units {
                    e.set_item("units", hex(&units))?;
                }
            }
            if entry.kind == Kind::Cell {
                // Python: entry["name"] is the raw STRNAME payload; refs is present when any
                // reference records were seen, even if bounded trimming leaves an empty map;
                // refs_truncated only when True; ref_records always.
                match entry.name {
                    Some(n) => e.set_item("name", PyBytes::new(py, &n))?,
                    None => e.set_item("name", py.None())?,
                }
                e.set_item("ref_records", entry.ref_records)?;
                if entry.ref_records > 0 {
                    let refs = PyDict::new(py);
                    for (target, count) in entry.refs {
                        refs.set_item(PyBytes::new(py, &target), count)?;
                    }
                    e.set_item("refs", refs)?;
                }
                if entry.refs_truncated {
                    e.set_item("refs_truncated", true)?;
                }
            }
            d.set_item("entry", e)?;
            let list = PyList::empty(py);
            for c in chunks {
                list.append(hex(&c))?;
            }
            d.set_item("chunks", list)?;
            match timestamp {
                Some(ts) => d.set_item("timestamp", PyBytes::new(py, &ts))?,
                None => d.set_item("timestamp", py.None())?,
            }
        }
    }
    Ok(d)
}

struct ScanState {
    core: scan::Scanner,
    finished: bool,
}

#[pyclass(frozen, module = "vestigraph_scan_core")]
pub struct Scanner {
    state: Mutex<ScanState>,
    cancelled: AtomicBool,
    cancel_flag: std::sync::Arc<AtomicBool>,
}

use scan::{MAX_BATCH_EVENTS, MAX_BATCH_PAYLOAD_BYTES};
const MAX_INPUT_BYTES: usize = 4 * 1024 * 1024;

#[pymethods]
impl Scanner {
    #[new]
    #[pyo3(signature = (cdc_min, cdc_max, cdc_mask, refs_max_distinct, refs_max_bytes, target_batch))]
    fn new(
        cdc_min: u64,
        cdc_max: u64,
        cdc_mask: u32,
        refs_max_distinct: usize,
        refs_max_bytes: usize,
        target_batch: usize,
    ) -> PyResult<Self> {
        let core = scan::Scanner::new(Params {
            cdc_min: cdc_min as usize,
            cdc_max: (cdc_max as usize).max(1),
            cdc_mask,
            refs_max_distinct,
            refs_max_bytes,
            target_batch: target_batch.max(1),
        });
        let cancel_flag = core.cancel_flag();
        Ok(Scanner {
            state: Mutex::new(ScanState {
                core,
                finished: false,
            }),
            cancelled: AtomicBool::new(false),
            cancel_flag,
        })
    }

    /// Consume at most 4 MiB and pause parsing at the output budget. Call feed(b"") while
    /// needs_drain() before feeding more input or finishing. Batches contain at most 1024
    /// events / 8 MiB chunk+refs payload, except one indivisible oversized event may be
    /// returned alone. Segment metadata is not included in the payload budget.
    ///
    /// Event dicts: {"t":"chunk","digest":str,"data":bytes,"name":bytes|None,"offset":int|None},
    /// {"t":"refs","targets":list[bytes]}, {"t":"segment","entry":dict,"chunks":list[str],
    /// "timestamp":bytes|None} -- the same three sink calls as the Python reference, in order.
    ///
    /// The input is copied into Rust-owned memory BEFORE the GIL is released (`Python::detach`),
    /// so no Python buffer is read while other threads run. `cancel()` from another thread is
    /// honoured between records and raises ScanCancelled.
    fn feed(&self, py: Python<'_>, data: &[u8]) -> PyResult<Vec<Py<PyDict>>> {
        if self.cancelled.load(Ordering::SeqCst) {
            return Err(ScanCancelled::new_err("scan cancelled before feed() ran"));
        }
        if data.len() > MAX_INPUT_BYTES {
            return Err(PyValueError::new_err(
                "input block exceeds MAX_INPUT_BYTES (4 MiB)",
            ));
        }
        let owned: Vec<u8> = data.to_vec();
        let state = &self.state;
        let outcome: Result<Vec<Event>, (ScanError, Vec<Event>)> = py.detach(move || {
            let mut guard = state.lock().expect("Scanner state mutex poisoned");
            if guard.finished {
                return Err((ScanError::runtime("feed() called after finish()", 0), Vec::new()));
            }
            match guard.core.feed_owned(owned) {
                Ok(()) => Ok(guard
                    .core
                    .take_events(MAX_BATCH_EVENTS, MAX_BATCH_PAYLOAD_BYTES)),
                // The events queued before the failing record are part of the transcript the
                // Python reference produces (it delivers them as it goes); hand them over with
                // the error instead of dropping them with the session.
                Err(e) => Err((e, guard.core.take_events(usize::MAX, usize::MAX))),
            }
        });
        let events = match outcome {
            Ok(events) => events,
            Err((e, pending)) => return Err(error_with_pending(py, e, pending)?),
        };
        let mut out = Vec::with_capacity(events.len());
        for ev in events {
            out.push(event_to_py(py, ev)?.unbind());
        }
        Ok(out)
    }

    /// End of input. Returns the ScanResult fields (raw_sha256, normalized_sha256, size,
    /// segments, cells, stamps, chunk_refs) plus "events": the final batch (the closing
    /// segment's chunks/refs/segment events), which the adapter must replay onto the sink
    /// BEFORE reporting the result -- exactly the order the Python reference produces them.
    /// Raises GdsStructureError for end-of-stream violations (truncation, missing ENDLIB).
    fn finish(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        if self.cancelled.load(Ordering::SeqCst) {
            return Err(ScanCancelled::new_err("scan cancelled before finish() ran"));
        }
        let state = &self.state;
        let outcome: Result<(ScanResult, Vec<Event>), (ScanError, Vec<Event>)> = py.detach(move || {
            let mut guard = state.lock().expect("Scanner state mutex poisoned");
            if guard.finished {
                return Err((ScanError::runtime("finish() called more than once", 0), Vec::new()));
            }
            match guard.core.finish() {
                Ok(result) => {
                    guard.finished = true;
                    let events = guard.core.take_events(usize::MAX, usize::MAX);
                    Ok((result, events))
                }
                Err(e) => Err((e, guard.core.take_events(usize::MAX, usize::MAX))),
            }
        });
        let (result, events) = match outcome {
            Ok(pair) => pair,
            Err((e, pending)) => return Err(error_with_pending(py, e, pending)?),
        };
        let d = PyDict::new(py);
        d.set_item("raw_sha256", hex(&result.raw_sha256))?;
        d.set_item("normalized_sha256", hex(&result.normalized_sha256))?;
        d.set_item("size", result.size)?;
        d.set_item("segments", result.segments)?;
        d.set_item("cells", result.cells)?;
        d.set_item("stamps", result.stamps)?;
        d.set_item("chunk_refs", result.chunk_refs)?;
        let list = PyList::empty(py);
        for ev in events {
            list.append(event_to_py(py, ev)?)?;
        }
        d.set_item("events", list)?;
        Ok(d.unbind())
    }

    /// Request cancellation from any thread; idempotent. A feed in flight stops at its next
    /// between-records check (every 4096 records) and raises ScanCancelled.
    fn cancel(&self) {
        self.cancelled.store(true, Ordering::SeqCst);
        self.cancel_flag.store(true, Ordering::SeqCst);
    }

    /// True until retained input has been parsed and queued events returned.
    fn needs_drain(&self) -> bool {
        self.state
            .lock()
            .expect("Scanner state mutex poisoned")
            .core
            .needs_drain()
    }

    fn buffered_input_bytes(&self) -> usize {
        self.state
            .lock()
            .expect("Scanner state mutex poisoned")
            .core
            .buffered_input_bytes()
    }

    /// Diagnostics: records framed so far and events still queued.
    fn stats(&self) -> PyResult<(u64, usize, usize)> {
        let guard = self.state.lock().expect("Scanner state mutex poisoned");
        Ok((
            guard.core.records_seen,
            guard.core.pending_events(),
            guard.core.pending_bytes(),
        ))
    }
}

#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pymodule]
fn vestigraph_scan_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Scanner>()?;
    m.add("GdsStructureError", m.py().get_type::<GdsStructureError>())?;
    m.add("ScanCancelled", m.py().get_type::<ScanCancelled>())?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add("BACKEND_ID", BACKEND_ID)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("MAX_BATCH_EVENTS", MAX_BATCH_EVENTS)?;
    m.add("MAX_BATCH_PAYLOAD_BYTES", MAX_BATCH_PAYLOAD_BYTES)?;
    m.add("MAX_INPUT_BYTES", MAX_INPUT_BYTES)?;
    m.add("BOUNDED_INPUT_API", 1)?;
    Ok(())
}
