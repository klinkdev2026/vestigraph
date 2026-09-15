//! Strict streaming GDS2 record scan, a byte-for-byte port of `vestigraph/storage/gds_scan.py`
//! (profile gds-record-cdc-v1). Pure Rust, no Python types: the PyO3 layer in lib.rs only
//! moves events across the boundary.
//!
//! Contract (must stay identical to the Python reference, see SPEC_PERFORMANCE_READ_ACCESS §3.3):
//! framing/state machine, reversible timestamp zeroing (BGNLIB/BGNSTR with length 28 and
//! datatype 2 only), record-aligned CDC (min/max/mask, records >= 12 bytes, crc32 of the
//! normalized record), per-chunk/segment/normalized/raw SHA-256, bounded reference summaries in
//! first-seen order, every SNAME reaching the global reference batch, error reasons + offsets.

use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

pub const HEADER: u8 = 0x00;
pub const BGNLIB: u8 = 0x01;
pub const UNITS: u8 = 0x03;
pub const ENDLIB: u8 = 0x04;
pub const BGNSTR: u8 = 0x05;
pub const STRNAME: u8 = 0x06;
pub const ENDSTR: u8 = 0x07;
pub const SNAME: u8 = 0x12;
pub const CDC_MIN_RECORD: usize = 12;
const ZERO24: [u8; 24] = [0u8; 24];
pub const MAX_BATCH_EVENTS: usize = 1024;
pub const MAX_BATCH_PAYLOAD_BYTES: usize = 8 * 1024 * 1024;

#[derive(Clone, Copy, Debug)]
pub struct Params {
    pub cdc_min: usize,
    pub cdc_max: usize,
    pub cdc_mask: u32,
    pub refs_max_distinct: usize,
    pub refs_max_bytes: usize,
    pub target_batch: usize,
}

impl Default for Params {
    fn default() -> Self {
        Params {
            cdc_min: 64 * 1024,
            cdc_max: 1024 * 1024,
            cdc_mask: 0x0FFF,
            refs_max_distinct: 1024,
            refs_max_bytes: 32 * 1024,
            target_batch: 1024,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScanErrorKind {
    Structure,
    Cancelled,
    Runtime,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ScanError {
    pub kind: ScanErrorKind,
    pub reason: String,
    pub offset: u64,
}

impl ScanError {
    fn structure(reason: impl Into<String>, offset: u64) -> Self {
        ScanError {
            kind: ScanErrorKind::Structure,
            reason: reason.into(),
            offset,
        }
    }

    pub fn runtime(reason: impl Into<String>, offset: u64) -> Self {
        ScanError {
            kind: ScanErrorKind::Runtime,
            reason: reason.into(),
            offset,
        }
    }

    fn cancelled(offset: u64) -> Self {
        ScanError {
            kind: ScanErrorKind::Cancelled,
            reason: "cancelled".to_string(),
            offset,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum Kind {
    LibHead,
    Cell,
    LibTail,
}

impl Kind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Kind::LibHead => "lib_head",
            Kind::Cell => "cell",
            Kind::LibTail => "lib_tail",
        }
    }
}

#[derive(Debug, Clone)]
pub struct SegmentEntry {
    pub kind: Kind,
    pub size: u64,
    pub hash: [u8; 32],
    pub stamp: Option<u64>,
    pub units: Option<Vec<u8>>,
    pub name: Option<Vec<u8>>,
    /// bounded reference summary in first-seen order (target payload bytes, count)
    pub refs: Vec<(Vec<u8>, u64)>,
    pub refs_truncated: bool,
    pub ref_records: u64,
}

#[derive(Debug, Clone)]
pub enum Event {
    Chunk {
        digest: [u8; 32],
        data: Vec<u8>,
        name: Option<Vec<u8>>,
        offset: Option<u64>,
    },
    Refs(Vec<Vec<u8>>),
    Segment {
        entry: SegmentEntry,
        chunks: Vec<[u8; 32]>,
        timestamp: Option<[u8; 24]>,
    },
}

impl Event {
    pub fn payload_len(&self) -> usize {
        match self {
            Event::Chunk { data, .. } => data.len(),
            Event::Refs(t) => t.iter().map(|x| x.len()).sum(),
            Event::Segment { .. } => 0,
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct ScanResult {
    pub raw_sha256: [u8; 32],
    pub normalized_sha256: [u8; 32],
    pub size: u64,
    pub segments: u64,
    pub cells: u64,
    pub stamps: u64,
    pub chunk_refs: u64,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum State {
    Start,
    Head,
    Cell,
    Between,
}

/// Per-segment accumulation state (mirrors the seg_* locals of the Python scanner).
struct Segment {
    kind: Kind,
    hash: Sha256,
    size: u64,
    chunks: Vec<[u8; 32]>,
    abs_start: u64,
    name: Option<Vec<u8>>,
    stamp: Option<u64>,
    timestamp: Option<[u8; 24]>,
    refs: Vec<(Vec<u8>, u64)>,
    refs_index: HashMap<Vec<u8>, usize>,
    refs_truncated: bool,
    ref_count: u64,
    units: Option<Vec<u8>>,
    targets: Vec<Vec<u8>>,
    targets_bytes: usize,
}

impl Segment {
    fn new(kind: Kind, abs_start: u64) -> Self {
        Segment {
            kind,
            hash: Sha256::new(),
            size: 0,
            chunks: Vec::new(),
            abs_start,
            name: None,
            stamp: None,
            timestamp: None,
            refs: Vec::new(),
            refs_index: HashMap::new(),
            refs_truncated: false,
            ref_count: 0,
            units: None,
            targets: Vec::new(),
            targets_bytes: 0,
        }
    }
}

pub struct Scanner {
    p: Params,
    raw: Sha256,
    normalized: Sha256,
    result: ScanResult,
    // chunk accumulation
    parts: Vec<u8>, // normalized bytes of the open chunk (owned copy; Python joins views)
    seg_len: usize, // bytes since the last cut within the segment
    seg: Segment,
    // parse state
    state: State,
    expect_strname: bool,
    units_seen: bool,
    tail: Vec<u8>,
    // Retain one owned allocation plus a cursor; do not copy suffixes on each drain.
    pending_input: Option<(Vec<u8>, usize)>,
    abs_base: u64,
    trailer: bool,
    ended: bool,
    finished: bool,
    // output
    events: VecDeque<Event>,
    pending_bytes: usize,
    cancel: Arc<AtomicBool>,
    pub records_seen: u64,
}

impl Scanner {
    pub fn new(p: Params) -> Self {
        let p = Params {
            cdc_min: p.cdc_min,
            cdc_max: p.cdc_max.max(1),
            cdc_mask: p.cdc_mask,
            refs_max_distinct: p.refs_max_distinct,
            refs_max_bytes: p.refs_max_bytes,
            target_batch: p.target_batch.max(1),
        };
        Scanner {
            p,
            raw: Sha256::new(),
            normalized: Sha256::new(),
            result: ScanResult::default(),
            parts: Vec::new(),
            seg_len: 0,
            seg: Segment::new(Kind::LibHead, 0),
            state: State::Start,
            expect_strname: false,
            units_seen: false,
            tail: Vec::new(),
            abs_base: 0,
            pending_input: None,
            trailer: false,
            ended: false,
            finished: false,
            events: VecDeque::new(),
            pending_bytes: 0,
            cancel: Arc::new(AtomicBool::new(false)),
            records_seen: 0,
        }
    }

    pub fn cancel_flag(&self) -> Arc<AtomicBool> {
        self.cancel.clone()
    }

    pub fn pending_events(&self) -> usize {
        self.events.len()
    }
    pub fn pending_bytes(&self) -> usize {
        self.pending_bytes
    }
    pub fn needs_drain(&self) -> bool {
        self.pending_input.is_some() || !self.events.is_empty()
    }
    pub fn buffered_input_bytes(&self) -> usize {
        self.pending_input
            .as_ref()
            .map_or(self.tail.len(), |(data, start)| data.len() - start)
    }

    fn output_full(&self) -> bool {
        // A record may emit chunk + refs + segment atomically. Payload can exceed
        // its soft budget by one record's events, never by an entire feed.
        self.events.len() >= MAX_BATCH_EVENTS - 3 || self.pending_bytes >= MAX_BATCH_PAYLOAD_BYTES
    }

    /// Drain up to `max_events` events / `max_bytes` payload (at least one event if any).
    pub fn take_events(&mut self, max_events: usize, max_bytes: usize) -> Vec<Event> {
        let mut out = Vec::new();
        let mut bytes = 0usize;
        while let Some(front) = self.events.front() {
            let len = front.payload_len();
            if !out.is_empty() && (out.len() >= max_events || bytes + len > max_bytes) {
                break;
            }
            let ev = self.events.pop_front().unwrap();
            self.pending_bytes -= len;
            bytes += len;
            out.push(ev);
        }
        out
    }

    fn push(&mut self, ev: Event) {
        self.pending_bytes += ev.payload_len();
        self.events.push_back(ev);
    }

    fn emit_chunk(&mut self) {
        if self.parts.is_empty() {
            self.seg_len = 0;
            return;
        }
        let data = std::mem::take(&mut self.parts);
        self.seg_len = 0;
        let digest: [u8; 32] = Sha256::digest(&data).into();
        self.seg.hash.update(&data);
        self.normalized.update(&data);
        self.seg.chunks.push(digest);
        self.result.chunk_refs += 1;
        let (name, offset) = if self.seg.kind == Kind::Cell && self.seg.name.is_some() {
            (
                self.seg.name.clone(),
                Some(self.seg.size - data.len() as u64),
            )
        } else {
            (None, None)
        };
        self.push(Event::Chunk {
            digest,
            data,
            name,
            offset,
        });
    }

    fn begin_segment(&mut self, kind: Kind, abs_start: u64) {
        self.seg = Segment::new(kind, abs_start);
        self.seg_len = 0;
        self.parts.clear();
    }

    fn flush_targets(&mut self) {
        if !self.seg.targets.is_empty() {
            let t = std::mem::take(&mut self.seg.targets);
            self.seg.targets_bytes = 0;
            self.push(Event::Refs(t));
        }
    }

    fn end_segment(&mut self) {
        self.emit_chunk();
        self.flush_targets();
        let seg = std::mem::replace(&mut self.seg, Segment::new(Kind::LibTail, 0));
        let hash: [u8; 32] = seg.hash.finalize().into();
        if seg.stamp.is_some() {
            self.result.stamps += 1;
        }
        // bounded refs: the Python reference keeps at most refs_max_distinct keys (already bounded
        // while scanning) and then trims by an approximate encoded size in insertion order.
        let mut refs = seg.refs;
        let mut truncated = seg.refs_truncated;
        if !refs.is_empty() {
            let approx: usize = refs.iter().map(|(k, _)| k.len() * 4 / 3 + 12).sum();
            if approx > self.p.refs_max_bytes {
                let mut kept = Vec::new();
                let mut total = 0usize;
                for (k, c) in refs.into_iter() {
                    let item = k.len() * 4 / 3 + 12;
                    if total + item > self.p.refs_max_bytes {
                        truncated = true;
                        break;
                    }
                    total += item;
                    kept.push((k, c));
                }
                refs = kept;
            }
        }
        let entry = SegmentEntry {
            kind: seg.kind,
            size: seg.size,
            hash,
            stamp: seg.stamp,
            units: seg.units,
            name: seg.name,
            refs,
            refs_truncated: truncated,
            ref_records: seg.ref_count,
        };
        self.result.segments += 1;
        self.push(Event::Segment {
            entry,
            chunks: seg.chunks,
            timestamp: seg.timestamp,
        });
    }

    fn err(&self, reason: &str, offset: u64) -> ScanError {
        ScanError::structure(reason, offset)
    }

    /// Test convenience; FFI transfers its already-owned block via feed_owned.
    #[cfg(test)]
    pub fn feed(&mut self, input: &[u8]) -> Result<(), ScanError> {
        self.feed_owned(input.to_vec())
    }

    /// Pause parsing at the output budget; drain before accepting another input.
    pub fn feed_owned(&mut self, input: Vec<u8>) -> Result<(), ScanError> {
        if self.finished {
            return Err(ScanError::runtime(
                "scanner already finished",
                self.abs_base,
            ));
        }
        if !input.is_empty() && self.needs_drain() {
            return Err(ScanError::runtime(
                "drain pending input and events before feed",
                self.abs_base,
            ));
        }
        let (data, start) = if input.is_empty() {
            match self.pending_input.take() {
                Some(pending) => pending,
                None => return Ok(()),
            }
        } else {
            self.raw.update(&input);
            self.result.size += input.len() as u64;
            if self.tail.is_empty() {
                (input, 0)
            } else {
                let mut d = std::mem::take(&mut self.tail);
                self.abs_base -= d.len() as u64;
                d.extend_from_slice(&input);
                (d, 0)
            }
        };
        let data_ref = &data[start..];
        let n = data_ref.len();
        let mut pos = 0usize;
        let mut piece_start = 0usize;
        let mut paused = false;
        if !self.trailer {
            while pos + 4 <= n {
                if self.output_full() {
                    paused = true;
                    break;
                }
                self.records_seen += 1;
                if self.records_seen % 4096 == 0 && self.cancel.load(Ordering::Relaxed) {
                    return Err(ScanError::cancelled(self.abs_base + pos as u64));
                }
                let length = ((data_ref[pos] as usize) << 8) | data_ref[pos + 1] as usize;
                if length < 4 || length & 1 == 1 {
                    return Err(self.err(
                        &format!("bad record length {}", length),
                        self.abs_base + pos as u64,
                    ));
                }
                let end = pos + length;
                if end > n {
                    break;
                }
                let rtype = data_ref[pos + 2];
                let abs_pos = self.abs_base + pos as u64;
                match rtype {
                    BGNSTR => {
                        if self.state == State::Start || self.state == State::Cell {
                            return Err(self.err("BGNSTR in an illegal position", abs_pos));
                        }
                        if !self.units_seen {
                            return Err(self.err("BGNSTR before UNITS", abs_pos));
                        }
                        if self.state == State::Head {
                            if pos > piece_start {
                                self.parts.extend_from_slice(&data_ref[piece_start..pos]);
                                self.seg.size += (pos - piece_start) as u64;
                            }
                            self.end_segment();
                        }
                        self.begin_segment(Kind::Cell, abs_pos);
                        self.result.cells += 1;
                        piece_start = pos;
                        if length == 28 && data_ref[pos + 3] == 2 {
                            self.parts.extend_from_slice(&data_ref[pos..pos + 4]);
                            self.parts.extend_from_slice(&ZERO24);
                            let mut ts = [0u8; 24];
                            ts.copy_from_slice(&data_ref[pos + 4..end]);
                            self.seg.timestamp = Some(ts);
                            self.seg.stamp = Some(4);
                            self.seg.size += 28;
                            self.seg_len += 28;
                            piece_start = end;
                        } else {
                            self.seg_len += length;
                        }
                        self.state = State::Cell;
                        self.expect_strname = true;
                        pos = end;
                        continue;
                    }
                    STRNAME => {
                        if !self.expect_strname {
                            return Err(self.err("STRNAME not directly after BGNSTR", abs_pos));
                        }
                        self.expect_strname = false;
                        self.seg.name = Some(data_ref[pos + 4..end].to_vec());
                    }
                    ENDSTR => {
                        if self.state != State::Cell || self.expect_strname {
                            return Err(self.err("ENDSTR outside a cell", abs_pos));
                        }
                        self.parts.extend_from_slice(&data_ref[piece_start..end]);
                        self.seg.size += (end - piece_start) as u64;
                        self.seg_len += length;
                        self.end_segment();
                        self.begin_segment(Kind::LibTail, self.abs_base + end as u64);
                        piece_start = end;
                        self.state = State::Between;
                        pos = end;
                        continue;
                    }
                    SNAME => {
                        if self.state != State::Cell {
                            return Err(self.err("SNAME outside a cell", abs_pos));
                        }
                        let target = data_ref[pos + 4..end].to_vec();
                        if let Some(&i) = self.seg.refs_index.get(&target) {
                            self.seg.refs[i].1 += 1;
                        } else if self.seg.refs.len() < self.p.refs_max_distinct {
                            self.seg
                                .refs_index
                                .insert(target.clone(), self.seg.refs.len());
                            self.seg.refs.push((target.clone(), 1));
                        } else {
                            self.seg.refs_truncated = true;
                        }
                        self.seg.ref_count += 1;
                        if !self.seg.targets.is_empty()
                            && (self.seg.targets.len() >= self.p.target_batch
                                || self.seg.targets_bytes + target.len() > MAX_BATCH_PAYLOAD_BYTES)
                        {
                            self.flush_targets();
                        }
                        self.seg.targets_bytes += target.len();
                        self.seg.targets.push(target);
                        if self.seg.targets.len() >= self.p.target_batch
                            || self.seg.targets_bytes >= MAX_BATCH_PAYLOAD_BYTES
                        {
                            self.flush_targets();
                        }
                    }
                    HEADER => {
                        if self.state != State::Start {
                            return Err(self.err("HEADER not first", abs_pos));
                        }
                        self.state = State::Head;
                    }
                    BGNLIB => {
                        if self.state != State::Head || abs_pos != 6 || self.seg.stamp.is_some() {
                            return Err(self.err("BGNLIB not directly after HEADER", abs_pos));
                        }
                        if length == 28 && data_ref[pos + 3] == 2 {
                            self.parts
                                .extend_from_slice(&data_ref[piece_start..pos + 4]);
                            self.parts.extend_from_slice(&ZERO24);
                            self.seg.size += (pos + 4 - piece_start) as u64 + 24;
                            self.seg_len += length;
                            let mut ts = [0u8; 24];
                            ts.copy_from_slice(&data_ref[pos + 4..end]);
                            self.seg.timestamp = Some(ts);
                            self.seg.stamp = Some(abs_pos - self.seg.abs_start + 4);
                            piece_start = end;
                            pos = end;
                            continue;
                        }
                    }
                    UNITS => {
                        if self.state != State::Head {
                            return Err(self.err("UNITS outside the library header", abs_pos));
                        }
                        self.units_seen = true;
                        self.seg.units = Some(data_ref[pos + 4..end].to_vec());
                    }
                    ENDLIB => {
                        if self.state == State::Cell || self.state == State::Start {
                            return Err(self.err("ENDLIB inside a cell or before HEADER", abs_pos));
                        }
                        if self.state == State::Head {
                            if pos > piece_start {
                                self.parts.extend_from_slice(&data_ref[piece_start..pos]);
                                self.seg.size += (pos - piece_start) as u64;
                            }
                            self.end_segment();
                            self.begin_segment(Kind::LibTail, abs_pos);
                            piece_start = pos;
                        }
                        self.parts.extend_from_slice(&data_ref[piece_start..end]);
                        self.seg.size += (end - piece_start) as u64;
                        self.seg_len += length;
                        piece_start = end;
                        pos = end;
                        self.trailer = true;
                        self.ended = true;
                        break;
                    }
                    _ => {
                        if self.state == State::Start {
                            return Err(self.err("first record is not HEADER", abs_pos));
                        }
                        if self.state == State::Between {
                            return Err(self.err("record between ENDSTR and BGNSTR", abs_pos));
                        }
                        if self.expect_strname {
                            return Err(self.err("STRNAME not directly after BGNSTR", abs_pos));
                        }
                    }
                }
                // plain record: chunk accounting (normalized == raw here; timestamp records `continue`d above)
                self.seg_len += length;
                if self.seg_len >= self.p.cdc_min
                    && ((length >= CDC_MIN_RECORD
                        && crc32fast::hash(&data_ref[pos..end]) & self.p.cdc_mask == 0)
                        || self.seg_len >= self.p.cdc_max)
                {
                    self.parts.extend_from_slice(&data_ref[piece_start..end]);
                    self.seg.size += (end - piece_start) as u64;
                    piece_start = end;
                    self.emit_chunk();
                }
                pos = end;
            }
        }
        if self.trailer {
            while pos < n {
                if self.output_full() {
                    paused = true;
                    break;
                }
                if self.cancel.load(Ordering::Relaxed) {
                    return Err(ScanError::cancelled(self.abs_base + pos as u64));
                }
                if self.seg_len >= self.p.cdc_max {
                    self.emit_chunk();
                }
                let take = std::cmp::min(n - pos, self.p.cdc_max - self.seg_len);
                debug_assert!(take > 0);
                self.parts.extend_from_slice(&data_ref[pos..pos + take]);
                self.seg.size += take as u64;
                self.seg_len += take;
                pos += take;
                if self.seg_len >= self.p.cdc_max {
                    self.emit_chunk();
                }
            }
            self.tail.clear();
        } else {
            if pos > piece_start {
                self.parts.extend_from_slice(&data_ref[piece_start..pos]);
                self.seg.size += (pos - piece_start) as u64;
            }
            if !paused {
                self.tail = if pos < n {
                    data_ref[pos..].to_vec()
                } else {
                    Vec::new()
                };
            }
        }
        if paused {
            self.abs_base += pos as u64;
            self.pending_input = Some((data, start + pos));
        } else {
            self.abs_base += n as u64;
        }
        Ok(())
    }

    /// End of input: the last segment is closed and the result is available.
    pub fn finish(&mut self) -> Result<ScanResult, ScanError> {
        if self.finished {
            return Err(ScanError::runtime(
                "scanner already finished",
                self.abs_base,
            ));
        }
        if self.needs_drain() {
            return Err(ScanError::runtime(
                "drain pending input and events before finish",
                self.abs_base,
            ));
        }
        if !self.ended {
            if self.result.size == 0 {
                return Err(self.err("empty file", 0));
            }
            return Err(self.err(
                "truncated record or missing ENDLIB",
                self.abs_base - self.tail.len() as u64,
            ));
        }
        self.end_segment();
        self.finished = true;
        let mut r = self.result.clone();
        r.raw_sha256 = std::mem::take(&mut self.raw).finalize().into();
        r.normalized_sha256 = std::mem::take(&mut self.normalized).finalize().into();
        self.result = r.clone();
        Ok(r)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rec(rtype: u8, dtype: u8, payload: &[u8]) -> Vec<u8> {
        let len = 4 + payload.len();
        let mut v = vec![(len >> 8) as u8, len as u8, rtype, dtype];
        v.extend_from_slice(payload);
        v
    }

    fn library(cells: &[Vec<u8>]) -> Vec<u8> {
        let mut v = rec(HEADER, 2, &[2, 88]);
        v.extend(rec(BGNLIB, 2, &[1u8; 24]));
        v.extend(rec(0x02, 6, b"LIB\0"));
        v.extend(rec(UNITS, 5, &[0u8; 16]));
        for c in cells {
            v.extend_from_slice(c);
        }
        v.extend(rec(ENDLIB, 0, &[]));
        v
    }

    fn cell(name: &[u8], body: &[u8]) -> Vec<u8> {
        let mut v = rec(BGNSTR, 2, &[2u8; 24]);
        v.extend(rec(STRNAME, 6, name));
        v.extend_from_slice(body);
        v.extend(rec(ENDSTR, 0, &[]));
        v
    }

    fn run(data: &[u8], slice: usize) -> (Vec<Event>, ScanResult) {
        let mut s = Scanner::new(Params::default());
        let mut events = Vec::new();
        for c in data.chunks(slice) {
            s.feed(c).unwrap();
            events.extend(s.take_events(usize::MAX, usize::MAX));
            while s.needs_drain() {
                s.feed(&[]).unwrap();
                events.extend(s.take_events(usize::MAX, usize::MAX));
            }
        }
        let r = s.finish().unwrap();
        events.extend(s.take_events(usize::MAX, usize::MAX));
        (events, r)
    }

    #[test]
    fn segments_and_timestamps_are_stable_across_slicing() {
        let c1 = cell(b"A\0", &rec(SNAME, 6, b"B\0"));
        let c2 = cell(b"B\0", &[]);
        let data = library(&[c1, c2]);
        let (e1, r1) = run(&data, 4096);
        let (e2, r2) = run(&data, 3);
        assert_eq!(r1.raw_sha256, r2.raw_sha256);
        assert_eq!(r1.normalized_sha256, r2.normalized_sha256);
        assert_eq!(r1.segments, 4);
        assert_eq!(r1.stamps, 3);
        assert_eq!(e1.len(), e2.len());
        let segs: Vec<_> = e1
            .iter()
            .filter_map(|e| {
                if let Event::Segment { entry, .. } = e {
                    Some(entry)
                } else {
                    None
                }
            })
            .collect();
        assert_eq!(segs[0].kind, Kind::LibHead);
        assert_eq!(segs[0].stamp, Some(10));
        assert_eq!(segs[1].name.as_deref(), Some(&b"A\0"[..]));
        assert_eq!(segs[1].refs, vec![(b"B\0".to_vec(), 1)]);
        assert_eq!(segs[1].ref_records, 1);
        assert_eq!(segs[3].kind, Kind::LibTail);
        assert_eq!(segs[3].size, 4);
    }

    #[test]
    fn refs_events_respect_payload_budget_before_count_limit() {
        let payload = vec![b'R'; 65_530];
        let mut body = Vec::new();
        for _ in 0..129 {
            body.extend(rec(SNAME, 6, &payload));
        }
        let data = library(&[cell(b"A\0", &body)]);
        let mut s = Scanner::new(Params {
            target_batch: usize::MAX,
            cdc_max: 64 * 1024 * 1024,
            ..Params::default()
        });
        let mut refs_payloads = Vec::new();

        s.feed(&data).unwrap();
        loop {
            for ev in s.take_events(usize::MAX, usize::MAX) {
                if let Event::Refs(targets) = ev {
                    refs_payloads.push(targets.iter().map(|t| t.len()).sum::<usize>());
                }
            }
            if !s.needs_drain() {
                break;
            }
            s.feed(&[]).unwrap();
        }
        s.finish().unwrap();
        for ev in s.take_events(usize::MAX, usize::MAX) {
            if let Event::Refs(targets) = ev {
                refs_payloads.push(targets.iter().map(|t| t.len()).sum::<usize>());
            }
        }

        assert_eq!(refs_payloads.len(), 2);
        assert!(refs_payloads
            .iter()
            .all(|bytes| *bytes <= MAX_BATCH_PAYLOAD_BYTES));
        assert_eq!(refs_payloads.iter().sum::<usize>(), 129 * payload.len());
    }

    #[test]
    fn structure_errors_carry_offsets() {
        let mut s = Scanner::new(Params::default());
        let mut bad = rec(HEADER, 2, &[2, 88]);
        bad.extend(rec(BGNLIB, 2, &[1u8; 24]));
        bad.extend(rec(UNITS, 5, &[0u8; 16]));
        bad.extend(rec(BGNSTR, 2, &[2u8; 24]));
        bad.extend(rec(0x0D, 2, &[0, 1])); // LAYER where STRNAME must be
        let err = s.feed(&bad).unwrap_err();
        assert_eq!(err.reason, "STRNAME not directly after BGNSTR");
        assert_eq!(err.offset, 6 + 28 + 20 + 28);
    }

    #[test]
    fn zero_cdc_max_is_clamped_before_trailer_chunking() {
        let mut params = Params::default();
        params.cdc_max = 0;
        let mut s = Scanner::new(params);
        let mut data = library(&[]);
        data.extend_from_slice(b"trailer");
        s.feed(&data).unwrap();
        let mut events = s.take_events(usize::MAX, usize::MAX);
        while s.needs_drain() {
            s.feed(&[]).unwrap();
            events.extend(s.take_events(usize::MAX, usize::MAX));
        }
        let result = s.finish().unwrap();
        events.extend(s.take_events(usize::MAX, usize::MAX));
        let mut normalized = Vec::new();
        for ev in events {
            if let Event::Chunk { data, .. } = ev {
                normalized.extend_from_slice(&data);
            }
        }
        assert_eq!(result.size, data.len() as u64);
        assert!(normalized.ends_with(b"trailer"));
    }
}
