// Vestigraph web panel. One page, ES modules, no build step.
// Rules: every string through i18n.t(); every list request carries a
// generation and stale responses are dropped; the browser never controls the
// recorder's lifetime — it only sends explicit user actions.
//
// Navigation is hash-routed and entirely client-side, independent per browser
// tab (nothing but the URL hash carries "where am I"):
//   #                          -> windows: every online KLayout window (by port)
//   #session=<id>              -> window: the layouts open in one window
//   #docs                      -> docs: every layout that has history (any project)
//   #doc=<id>[&session=<sid>]  -> doc: the saved-version timeline for one layout
// `&version=<cid>` / `&activity=<sid>` extend the doc route (kept from before).
import { Api, ApiError } from "./api.js";
import { setupSkills } from "./skills.js";
import { setupHistoryImport } from "./history_import.js";
import * as i18n from "./i18n.js";
import { draw, fitViewport } from "./preview.js";

const t = i18n.t;
const api = new Api();
const $ = (id) => document.getElementById(id);

const state = {
  signedIn: false,
  projects: [],                  // every project this service knows about
  route: "windows",              // "windows" | "window" | "docs" | "doc"
  sessionId: null,                // window route: which window; doc route: session context (breadcrumb), may be null
  sessions: [],                   // last GET /sessions response items (online and offline)
  sessionIndex: new Map(),        // session_id -> session item
  documentIndex: new Map(),       // document id -> {doc, projectId}
  documentsByProject: new Map(),  // project id -> [doc, ...]
  storageProjectId: null,
  storageStatus: null,
  documentId: null,
  docProjectId: null,             // resolved project id owning the document currently viewed
  projectStatus: null,
  checkpoints: [],
  checkpointsCursor: null,
  segments: [],
  segmentsCursor: null,
  events: [],
  selectedCheckpoint: null,
  segmentFilter: null,
  jobs: new Map(),                // job id -> {kind, status, label, result, error, checkpointId}
  detail: null,
  annotations: [],
  generation: 0,                  // bumped on every route change
  lastListSignature: "",
  listScopeKey: null,
  listPollDelay: 3000,
  playbackDiffDeferred: new Set(),
  hidden: false,
  // Playback: previews keyed by checkpoint id; one fixed viewport per document.
  previews: new Map(),            // checkpoint id -> {status, data, error, jobId, requestId}
  diffs: new Map(),               // "from>to" -> {status, data, error, requestId}
  recordedChanges: new Map(),     // checkpoint id -> {status, root, items, nextCursor, error}
  viewport: null,
  viewportKind: null,
  playing: false,
  playTimer: null,
  // Write actions in flight, keyed by action (see beginPending/endPending/isPending). Consulted
  // by every render function that sets a write button's `disabled` so a periodic poll re-render
  // can never re-enable a button while its request is still in flight.
  pendingActions: new Set(),
};

function isPending(key) { return state.pendingActions.has(key); }
function beginPending(key) { state.pendingActions.add(key); }
function endPending(key) { state.pendingActions.delete(key); }

// ---------------------------------------------------------------- boot ----
async function boot() {
  await i18n.load(i18n.initialLanguage());
  i18n.applyTo(document);
  wireEvents();
  const token = window.__vestigraphBootstrap;
  delete window.__vestigraphBootstrap;
  try {
    if (token) await api.bootstrap(token);
    else await api.session();
    state.signedIn = true;
  } catch (err) {
    state.signedIn = false;
    showLogin(err);
    return;
  }
  showApp();
  await refreshProjects();
  applyRouteFromHash();
  startPolling();
}

function showLogin(err) {
  $("login").hidden = false;
  $("main").hidden = true;
  $("state-pill").hidden = true;
  $("login-detail").textContent = err instanceof ApiError && err.code !== "UNAUTHENTICATED" ? describeError(err).text : "";
}

function showApp() {
  $("login").hidden = true;
  $("main").hidden = false;
}

// -------------------------------------------------------------- routing ----
function parseHash() {
  return new URLSearchParams((location.hash || "").replace(/^#/, ""));
}

function applyRouteFromHash() {
  const params = parseHash();
  if (params.has("doc")) {
    navigateDoc(params.get("doc"), params.get("session") || null,
                { checkpoint: params.get("version"), activity: params.get("activity") });
  } else if (params.has("session")) {
    navigateWindow(params.get("session"));
  } else if (params.has("docs")) {
    navigateDocs();
  } else {
    navigateWindows();
  }
}

function persistSelection() {
  const params = new URLSearchParams();
  if (state.route === "window") {
    params.set("session", state.sessionId);
  } else if (state.route === "docs") {
    params.set("docs", "1");
  } else if (state.route === "doc") {
    params.set("doc", state.documentId);
    if (state.sessionId) params.set("session", state.sessionId);
    if (state.selectedCheckpoint) params.set("version", state.selectedCheckpoint);
    if (state.segmentFilter) params.set("activity", state.segmentFilter);
  }
  const hash = params.toString();
  history.replaceState(null, "", location.pathname + (hash ? "#" + hash : ""));
}

function showView(name) {
  $("view-windows").hidden = name !== "windows";
  $("view-window").hidden = name !== "window";
  $("view-docs").hidden = name !== "docs";
  $("view-doc").hidden = name !== "doc";
  $("state-pill").hidden = name !== "doc";
}

function navigateWindows() {
  abortAll();
  state.generation += 1;
  state.route = "windows";
  state.sessionId = null;
  state.documentId = null;
  state.docProjectId = null;
  state.selectedCheckpoint = null;
  state.segmentFilter = null;
  clearDocumentViews();
  persistSelection();
  showView("windows");
  const gen = state.generation;
  renderWindowsView();
  refreshProjects().then(() => { if (gen === state.generation) { renderWindowsView(); loadStorageStatus(gen); } });
  loadSessions(gen, { announce: true }).then(() => { if (gen === state.generation) renderWindowsView(); });
}

function navigateWindow(sessionId) {
  abortAll();
  state.generation += 1;
  state.route = "window";
  state.sessionId = sessionId;
  state.documentId = null;
  state.docProjectId = null;
  state.selectedCheckpoint = null;
  state.segmentFilter = null;
  clearDocumentViews();
  persistSelection();
  showView("window");
  const gen = state.generation;
  renderWindowView();
  loadSessions(gen, { announce: true }).then(() => { if (gen === state.generation) renderWindowView(); });
}

function navigateDocs() {
  abortAll();
  state.generation += 1;
  state.route = "docs";
  state.sessionId = null;
  state.documentId = null;
  state.docProjectId = null;
  state.selectedCheckpoint = null;
  state.segmentFilter = null;
  clearDocumentViews();
  persistSelection();
  showView("docs");
  const gen = state.generation;
  (async () => {
    await refreshProjects();
    await loadAllDocuments(gen);
    if (gen === state.generation) renderDocsView();
  })();
}

function navigateDoc(documentId, sessionId = null, { checkpoint = null, activity = null } = {}) {
  abortAll();
  state.generation += 1;
  state.route = "doc";
  state.documentId = documentId;
  state.sessionId = sessionId;
  state.docProjectId = null;
  state.selectedCheckpoint = checkpoint;
  state.segmentFilter = activity;
  clearDocumentViews();
  persistSelection();
  showView("doc");
  renderDocBreadcrumb();
  const gen = state.generation;
  (async () => {
    await refreshProjects();
    await loadAllDocuments(gen);
    if (gen !== state.generation) return;
    const entry = state.documentIndex.get(documentId);
    state.docProjectId = entry ? entry.projectId : null;
    renderDocBreadcrumb();
    await pollStatus();
    await pollLists();
  })();
}

// ------------------------------------------------------------- polling ----
let statusTimer = null, listTimer = null;
const controllers = new Set();

function startPolling() {
  const tick = async () => { await pollStatus(); statusTimer = setTimeout(tick, state.hidden ? 5000 : 1000); };
  const lists = async () => { await pollLists(); listTimer = setTimeout(lists, state.hidden ? Math.max(10000, state.listPollDelay) : state.listPollDelay); };
  tick(); lists();
  document.addEventListener("visibilitychange", () => { state.hidden = document.hidden; });
}

function abortAll() {
  for (const c of controllers) c.abort();
  controllers.clear();
}

function guarded(generation, fn) {
  const controller = new AbortController();
  controllers.add(controller);
  return fn(controller.signal).finally(() => controllers.delete(controller))
    .then((value) => (generation === state.generation ? value : undefined));
}

async function pollStatus() {
  if (!state.signedIn) return;
  const gen = state.generation;
  try {
    await guarded(gen, (signal) => api.status(signal));
    setConnection(true);
  } catch (err) {
    if (err && err.name === "AbortError") return;
    if (err instanceof ApiError && err.status === 401) { state.signedIn = false; showLogin(err); return; }
    setConnection(false, err);
    return;
  }
  if (state.route === "doc" && state.docProjectId) {
    try {
      const status = await guarded(gen, (s) => api.projectStatus(state.docProjectId, s));
      if (status !== undefined) { state.projectStatus = status; renderRecording(); }
    } catch (err) {
      if (err && err.name === "AbortError") return;
      if (err instanceof ApiError && err.status === 401) { state.signedIn = false; showLogin(err); return; }
    }
  }
  await pollJobs();
}

let listsInFlight = null;
async function pollLists() {
  if (listsInFlight && listsInFlight.generation === state.generation) return listsInFlight.promise;
  const task = { generation: state.generation };
  task.promise = pollListsOnce().finally(() => { if (listsInFlight === task) listsInFlight = null; });
  listsInFlight = task;
  return task.promise;
}

async function pollListsOnce() {
  if (!state.signedIn) return;
  const gen = state.generation;
  await loadSessions(gen);
  if (gen !== state.generation) return;
  if (state.route === "windows") {
    renderWindowsView();
    await loadStorageStatus(gen);
    return;
  }
  if (state.route === "window") { renderWindowView(); return; }
  if (state.route === "docs") {
    await loadAllDocuments(gen);
    if (gen === state.generation) renderDocsView();
    return;
  }
  if (state.route !== "doc" || !state.documentId) return;
  try {
    const filter = state.segmentFilter;
    const scopeKey = JSON.stringify([state.documentId, filter]);
    const [freshCheckpoints, freshSegments, events] = await Promise.all([
      fetchCheckpointsWindow(gen),
      fetchSegmentsWindow(gen),
      guarded(gen, (s) => api.events(state.documentId, { limit: 30, segmentId: filter }, s)),
    ]);
    if (!freshCheckpoints || !freshSegments || !events || filter !== state.segmentFilter) return;
    const reset = state.listScopeKey !== scopeKey || state.historyRevision !== (freshCheckpoints.history_revision || 0);
    if (reset) { state.detail = null; state.recordedChanges.clear(); state.diffs.clear(); }
    state.historyRevision = freshCheckpoints.history_revision || 0;
    const checkpoints = mergeFreshWindow(state.checkpoints, state.checkpointsCursor, freshCheckpoints, reset);
    const segments = mergeFreshWindow(state.segments, state.segmentsCursor, freshSegments);
    state.listScopeKey = scopeKey;
    state.listPollDelay = 3000;
    const signature = JSON.stringify([checkpoints.items, segments.items, events.items]);
    if (signature !== state.lastListSignature) {
      state.lastListSignature = signature;
      state.checkpoints = checkpoints.items;
      state.checkpointsCursor = checkpoints.next_cursor;
      state.segments = segments.items;
      state.segmentsCursor = segments.next_cursor;
      state.events = events.items;
      renderTimeline();
      renderActivities();
      renderEvents();
    }
    if (state.selectedCheckpoint && !state.detail) await loadDetail();
    if (state.selectedCheckpoint) {
      renderPreview(); loadPreview(state.selectedCheckpoint); loadChanges(state.selectedCheckpoint);
      renderRecordedChanges(); loadRecordedChanges(state.selectedCheckpoint);
    }
  } catch (err) {
    state.listPollDelay = Math.min(30000, state.listPollDelay * 2);
    if (err && err.name === "AbortError") return;
    if (err instanceof ApiError && err.code === "CURSOR_SCOPE_MISMATCH") return;
    if (err instanceof ApiError && err.status === 404) { navigateDocs(); return; }
    toastError(err);
  }
}

/** Cursors retain their own snapshot upper bound. Only fetch the fresh first page
 * during polling; retain the old tail cursor when an overlap proves continuity.
 * No overlap means a gap (or a changed filter), so reset rather than invent a chain. */
function mergeFreshWindow(oldItems, oldCursor, fresh, reset = false) {
  const ids = new Set(fresh.items.map((item) => item.id));
  if (reset || !oldItems.some((item) => ids.has(item.id))) return fresh;
  return { items: fresh.items.concat(oldItems.filter((item) => !ids.has(item.id))),
           next_cursor: oldCursor };
}

async function fetchCheckpointsWindow(gen) {
  return guarded(gen, (s) => api.checkpoints(state.documentId, { limit: 50, segmentId: state.segmentFilter }, s));
}

async function fetchSegmentsWindow(gen) {
  return guarded(gen, (s) => api.segments(state.documentId, { limit: 50 }, s));
}

async function pollJobs() {
  for (const [id, job] of state.jobs) {
    if (!["queued", "running"].includes(job.status)) continue;
    try {
      const fresh = await api.job(id);
      job.status = fresh.status; job.result = fresh.result; job.error = fresh.error;
      if (!["queued", "running"].includes(fresh.status)) await settleTrackedJob(job, fresh);
      renderJobs();
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        // The service's in-memory job table no longer knows this job (e.g. it restarted) --
        // there is nothing left to poll for, so surface it once as lost instead of retrying
        // this id forever.
        job.status = "lost";
        job.error = { code: "JOB_LOST", message: t("errors.job_lost") };
        toastJob(job);
        renderJobs();
        continue;
      }
      if (err instanceof ApiError && err.status !== 404) toastError(err);
    }
  }
  pruneJobs();
}

/** Keep state.jobs from growing without bound. A settled job (anything no longer queued/running,
 *  including our own synthetic "lost") is dropped once it has been visible for a while, or --
 *  regardless of age -- once the tracked-job count exceeds the cap; jobs still in flight are
 *  never pruned. renderJobs() only ever shows the most recent few, so this never removes
 *  anything the panel currently needs. */
const SETTLED_JOB_TTL_MS = 5 * 60 * 1000;
const MAX_TRACKED_JOBS = 50;

function isSettledJobStatus(status) { return !["queued", "running"].includes(status); }

function pruneJobs() {
  const now = Date.now();
  for (const job of state.jobs.values()) {
    if (isSettledJobStatus(job.status) && job.settledAt === undefined) job.settledAt = now;
  }
  for (const [id, job] of state.jobs) {
    if (job.settledAt !== undefined && now - job.settledAt > SETTLED_JOB_TTL_MS) state.jobs.delete(id);
  }
  if (state.jobs.size > MAX_TRACKED_JOBS) {
    for (const [id, job] of state.jobs) {
      if (state.jobs.size <= MAX_TRACKED_JOBS) break;
      if (isSettledJobStatus(job.status)) state.jobs.delete(id);
    }
  }
}

function setConnection(ok, err) {
  const el = $("connection");
  if (ok) { el.textContent = ""; el.className = "connection ok"; return; }
  el.textContent = t("state.service_unreachable");
  el.className = "connection bad";
  if (err && !(err instanceof ApiError && err.code === "NETWORK")) toastError(err);
}

// ------------------------------------------------------------ projects ----
async function refreshProjects() {
  try {
    const data = await api.projects();
    state.projects = data.items;
  } catch (err) { toastError(err); return; }
  ensureStorageProject();
}

function ensureStorageProject() {
  if (state.storageProjectId && state.projects.some((p) => p.id === state.storageProjectId)) return;
  const catchAll = state.projects.find((p) => p.catch_all);
  state.storageProjectId = catchAll ? catchAll.id : (state.projects[0] ? state.projects[0].id : null);
}

async function loadAllDocuments(gen) {
  const lists = await Promise.all(state.projects.map((p) =>
    api.documents(p.id).then((d) => ({ pid: p.id, items: d.items })).catch(() => ({ pid: p.id, items: [] }))
  ));
  if (gen !== undefined && gen !== state.generation) return;
  const index = new Map();
  const byProject = new Map();
  for (const { pid, items } of lists) {
    byProject.set(pid, items);
    for (const doc of items) index.set(doc.id, { doc, projectId: pid });
  }
  state.documentIndex = index;
  state.documentsByProject = byProject;
}

function clearDocumentViews() {
  state.checkpoints = []; state.segments = []; state.events = []; state.detail = null; state.annotations = [];
  state.lastListSignature = "";
  state.listScopeKey = null;
  state.listPollDelay = 3000;
  state.playbackDiffDeferred.clear();
  stopPlayback();
  state.previews = new Map();
  state.diffs = new Map();
  state.recordedChanges = new Map();
  state.viewport = null;
  renderTimeline(); renderActivities(); renderEvents(); renderDetail(); renderRecordedChanges();
}

// -------------------------------------------------------------- sessions --
async function loadSessions(gen, { announce = false } = {}) {
  try {
    const data = await guarded(gen, (s) => api.sessions(s));
    if (data === undefined) return;
    state.sessions = data.items;
    state.sessionIndex = new Map(data.items.map((s) => [s.session_id, s]));
  } catch (err) {
    if (err && err.name === "AbortError") return;
    state.sessions = [];
    state.sessionIndex = new Map();
    if (announce) toastError(err);
  }
}

function primaryRecording(session) {
  return (session.recording && session.recording[0]) || null;
}

// -------------------------------------------------------- windows (home) --
function renderWindowsView() {
  $("windows-no-projects").hidden = state.projects.length > 0;
  if (!state.projects.length) {
    $("windows-empty").hidden = true;
    $("windows-list").innerHTML = "";
    return;
  }
  const online = state.sessions.filter((s) => s.online);
  $("windows-empty").hidden = online.length > 0;
  const list = $("windows-list");
  list.innerHTML = "";
  for (const session of online) list.appendChild(buildWindowCard(session));
  renderStorage();
}

function buildWindowCard(session) {
  const rec = primaryRecording(session);
  const card = document.createElement("div");
  card.className = "card window-card";
  const title = document.createElement("h3");
  title.textContent = session.display_name || t("windows.card_title", { port: session.port });
  const sub = document.createElement("p");
  sub.className = "muted";
  sub.textContent = session.session_id;
  const badge = document.createElement("span");
  badge.className = "pill";
  if (rec) { badge.textContent = t(`state.${rec.state}`); badge.dataset.state = rec.state; }
  else { badge.textContent = t("windows.not_recorded"); badge.dataset.state = "idle"; }
  const list = document.createElement("ul");
  list.className = "list compact";
  renderDocList(list, rec ? rec.documents : []);
  const actions = document.createElement("div");
  actions.className = "actions";
  const enter = document.createElement("button");
  enter.type = "button";
  enter.textContent = t("windows.enter");
  enter.addEventListener("click", () => navigateWindow(session.session_id));
  const pauseBtn = document.createElement("button");
  pauseBtn.type = "button";
  pauseBtn.textContent = t("actions.pause_window");
  const resumeBtn = document.createElement("button");
  resumeBtn.type = "button";
  resumeBtn.textContent = t("actions.resume_window");
  const canControl = Boolean(api.capabilities.autocapture) && Boolean(rec);
  const paused = Boolean(rec) && rec.state === "paused";
  pauseBtn.hidden = paused;
  resumeBtn.hidden = !paused;
  pauseBtn.disabled = !canControl || isPending(`pause:${session.session_id}`);
  resumeBtn.disabled = !canControl || isPending(`resume:${session.session_id}`);
  if (rec) {
    pauseBtn.addEventListener("click", () => doWindowPause(rec.project_id, session.session_id));
    resumeBtn.addEventListener("click", () => doWindowResume(rec.project_id, session.session_id));
  }
  actions.append(enter, pauseBtn, resumeBtn);
  card.append(title, sub, badge, list, actions);
  return card;
}

function renderDocList(container, docs, { clickable = false, sessionId = null } = {}) {
  container.innerHTML = "";
  if (!docs.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = t("windows.no_layouts");
    container.appendChild(li);
    return;
  }
  for (const doc of docs) {
    const li = document.createElement("li");
    const name = doc.filename ? baseName(doc.filename) : t("windows.unsaved");
    const marker = doc.recording ? " · " + t("windows.recording_marker")
                 : (doc.filename && !doc.document_id ? " · " + t("windows.not_yet_recorded") : "");
    li.textContent = name + marker;
    if (doc.filename) li.title = doc.filename;
    if (clickable && doc.document_id) {
      li.className = "item";
      li.tabIndex = 0;
      li.setAttribute("role", "button");
      const go = () => navigateDoc(doc.document_id, sessionId);
      li.addEventListener("click", go);
      li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    }
    container.appendChild(li);
  }
}

async function doWindowPause(projectId, sessionId) {
  if (!projectId) return;
  const requestId = crypto.randomUUID();
  beginPending(`pause:${sessionId}`);
  rerenderCurrentWindowViews();
  try {
    trackJob("pause", await api.pause(projectId, "user", requestId, sessionId));
  } catch (err) { toastError(err); }
  finally {
    // A window that just went offline (SESSION_OFFLINE) rejects this synchronously, not as a
    // job -- refresh the sessions list on the error path too, so a now-stale, no-longer
    // controllable card doesn't linger.
    endPending(`pause:${sessionId}`);
    await refreshSessionsAndRerender();
  }
}

async function doWindowResume(projectId, sessionId) {
  if (!projectId) return;
  const requestId = crypto.randomUUID();
  beginPending(`resume:${sessionId}`);
  rerenderCurrentWindowViews();
  try {
    trackJob("resume", await api.resume(projectId, requestId, sessionId));
  } catch (err) { toastError(err); }
  finally {
    endPending(`resume:${sessionId}`);
    await refreshSessionsAndRerender();
  }
}

function rerenderCurrentWindowViews() {
  if (state.route === "windows") renderWindowsView();
  if (state.route === "window") renderWindowView();
}

async function refreshSessionsAndRerender() {
  await loadSessions(state.generation);
  rerenderCurrentWindowViews();
}

// -------------------------------------------------------------- window ----
function renderWindowView() {
  const sid = state.sessionId;
  const session = state.sessionIndex.get(sid);
  const online = Boolean(session && session.online);
  $("window-gone").hidden = online;
  if (!online) {
    $("window-title").textContent = sid || "";
    $("window-badge").hidden = true;
    $("window-reason").textContent = "";
    $("window-pause").hidden = true;
    $("window-resume").hidden = true;
    $("window-documents").innerHTML = "";
    return;
  }
  $("window-title").textContent = session.display_name || t("windows.card_title", { port: session.port });
  const rec = primaryRecording(session);
  const badge = $("window-badge");
  if (rec) {
    badge.hidden = false;
    badge.textContent = t(`state.${rec.state}`);
    badge.dataset.state = rec.state;
    const reasonKey = `reason.${rec.reason || ""}`;
    $("window-reason").textContent = i18n.has(reasonKey) ? t(reasonKey) : (rec.reason || "");
  } else {
    badge.hidden = true;
    $("window-reason").textContent = t("windows.not_recorded");
  }
  const canControl = Boolean(api.capabilities.autocapture) && Boolean(rec);
  const paused = Boolean(rec) && rec.state === "paused";
  $("window-pause").hidden = paused;
  $("window-resume").hidden = !paused;
  $("window-pause").disabled = !canControl || isPending(`pause:${sid}`);
  $("window-resume").disabled = !canControl || isPending(`resume:${sid}`);
  renderDocList($("window-documents"), rec ? rec.documents : [], { clickable: true, sessionId: sid });
}

// -------------------------------------------------------------- all docs --
function renderDocsView() {
  const list = $("docs-list");
  list.innerHTML = "";
  const all = [];
  for (const docs of state.documentsByProject.values()) all.push(...docs);
  $("docs-empty").hidden = all.length > 0;
  for (const doc of all) {
    const li = document.createElement("li");
    li.className = "item";
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    const title = document.createElement("div");
    title.className = "item-title";
    title.textContent = doc.name + (doc.read_only ? " · " + t("document.read_only") : "");
    li.appendChild(title);
    const go = () => navigateDoc(doc.id);
    li.addEventListener("click", go);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    list.appendChild(li);
  }
}

// -------------------------------------------------------------- storage ---
async function loadStorageStatus(gen) {
  if (!state.storageProjectId) { state.storageStatus = null; renderStorage(); return; }
  try {
    const status = await guarded(gen, (s) => api.projectStatus(state.storageProjectId, s));
    if (status === undefined) return;
    state.storageStatus = status;
  } catch (err) {
    if (err && err.name === "AbortError") return;
    state.storageStatus = null;
  }
  renderStorage();
}

function renderStorage() {
  const status = state.storageStatus;
  const canControl = Boolean(api.capabilities.autocapture) && Boolean(state.storageProjectId);
  $("storage-current").textContent = status && status.history_root ? t("storage.current", { path: status.history_root }) : "";
  $("storage-change").disabled = !canControl || isPending("relocate");
  $("storage-browse").disabled = !canControl || isPending("relocate");
}

// --------------------------------------------------------- breadcrumb ----
function renderDocBreadcrumb() {
  const nav = $("doc-breadcrumb");
  nav.innerHTML = "";
  const crumbs = [{ text: t("breadcrumb.all_windows"), action: navigateWindows }];
  if (state.sessionId) {
    const session = state.sessionIndex.get(state.sessionId);
    const port = session ? session.port : "?";
    const sid = state.sessionId;
    crumbs.push({ text: (session && session.display_name) || t("windows.card_title", { port }), action: () => navigateWindow(sid) });
  } else {
    crumbs.push({ text: t("docs.title"), action: navigateDocs });
  }
  const entry = state.documentIndex.get(state.documentId);
  const name = entry ? entry.doc.name
    : (state.detail ? (state.detail.document_filename || state.detail.filename) : "…");
  crumbs.push({ text: name, action: null });
  crumbs.forEach((crumb, i) => {
    const el = document.createElement(crumb.action ? "button" : "span");
    if (crumb.action) { el.type = "button"; el.className = "link"; el.addEventListener("click", crumb.action); }
    else el.className = "crumb-current";
    el.textContent = crumb.text;
    nav.appendChild(el);
    if (i < crumbs.length - 1) {
      const sep = document.createElement("span");
      sep.className = "muted crumb-sep";
      sep.textContent = " › ";
      nav.appendChild(sep);
    }
  });
}

// ------------------------------------------------------------ recording ----
function renderRecording() {
  const status = state.projectStatus;
  const project = state.projects.find((p) => p.id === state.docProjectId);
  if (!status) return;
  const code = status.state || "blocked";
  const documentQueue = state.documentIndex.get(state.documentId)?.doc.capture_queue;
  const ownQueue = status.document_id === state.documentId ? status.capture_queue : documentQueue;
  const queue = ownQueue && ownQueue.enabled ? ownQueue : null;
  const queueFull = Boolean(queue && (
    queue.full || (status.queue && status.queue.full) ||
    (typeof queue.pending_count === "number" && typeof queue.capacity_count === "number" &&
      queue.capacity_count > 0 && queue.pending_count >= queue.capacity_count) ||
    (typeof queue.pending_bytes === "number" && typeof queue.capacity_bytes === "number" &&
      queue.capacity_bytes > 0 && queue.pending_bytes >= queue.capacity_bytes)
  ));
  const activeOrganizing = Boolean(queue && !status.capturing && (queue.processing || status.organizing_progress));
  const hasQueuedSnapshots = Boolean(queue && (queue.pending_count || 0) > 0);
  const cancelSafetyGate = code === "baselining" || code === "draining";
  $("state-pill").textContent = t(`state.${code}`);
  $("state-pill").dataset.state = code;
  $("recording-state").textContent = t(`state.${code}`);
  const reasonKey = `reason.${status.reason || ""}`;
  $("recording-detail").textContent = i18n.has(reasonKey) ? t(reasonKey) : (status.reason ? status.reason : "");
  const gap = status.gap;
  const gapEl = $("recording-gap");
  if (gap) {
    gapEl.hidden = false;
    gapEl.textContent = status.last_checkpoint_id
      ? t("recording.gap_with_saved", { time: i18n.formatRelative(status.last_success_at) })
      : t("recording.gap_without_saved");
  } else gapEl.hidden = true;
  const hint = [];
  if (status.last_success_at) hint.push(t("recording.last_saved", { time: i18n.formatRelative(status.last_success_at) }));
  const docEntry = state.documentIndex.get(status.document_id);
  if (docEntry) hint.push(t("recording.current_document", { name: docEntry.doc.name }));
  $("recording-hint").textContent = hint.join(" · ");
  // Legacy save progress is shown only before capture_queue-aware status exists.
  const progressEl = $("recording-progress");
  if (queue && (status.capturing || activeOrganizing || hasQueuedSnapshots)) {
    const op = status.organizing_progress || {};
    const parts = [status.capturing ? t("recording.queue_capturing") : t("recording.queue_organizing")];
    if (typeof op.fraction === "number") parts.push(`${Math.round(op.fraction * 100)}%`);
    if (typeof op.bytes === "number") {
      parts.push(typeof op.total === "number" && op.total > 0
        ? `${i18n.formatBytes(op.bytes)} / ${i18n.formatBytes(op.total)}`
        : i18n.formatBytes(op.bytes));
    }
    progressEl.textContent = parts.filter(Boolean).join(" · ");
    progressEl.hidden = false;
  } else if (code === "saving") {
    const sp = status.save_progress || {};
    const parts = [i18n.has(reasonKey) ? t(reasonKey) : (status.reason || "")];
    if (typeof sp.fraction === "number") parts.push(`${Math.round(sp.fraction * 100)}%`);
    if (typeof sp.bytes === "number") {
      parts.push(typeof sp.total === "number" && sp.total > 0
        ? `${i18n.formatBytes(sp.bytes)} / ${i18n.formatBytes(sp.total)}`
        : i18n.formatBytes(sp.bytes));
    }
    progressEl.textContent = parts.filter(Boolean).join(" · ");
    progressEl.hidden = false;
  } else {
    progressEl.hidden = true;
  }
  const queueEl = $("recording-queue");
  if (queue) {
    const parts = [
      t("recording.queue_pending", {
        count: i18n.formatNumber(queue.pending_count || 0),
        bytes: i18n.formatBytes(queue.pending_bytes || 0),
      }),
    ];
    if (queueFull) parts.push(t("recording.queue_full"));
    if ((queue.blocked_count || 0) > 0) {
      parts.push(t("recording.queue_blocked", { count: i18n.formatNumber(queue.blocked_count || 0) }));
    }
    if ((queue.quarantined_count || 0) > 0) {
      parts.push(t("recording.queue_unconfirmed", { count: i18n.formatNumber(queue.quarantined_count) }));
    }
    if (queue.last_error) parts.push(t("recording.queue_last_error", { error: queue.last_error }));
    queueEl.textContent = parts.join(" · ");
    queueEl.className = (queueFull || (queue.blocked_count || 0) > 0 || queue.quarantined_count > 0) ? "warn" : "muted";
    queueEl.hidden = false;
  } else {
    queueEl.hidden = true;
  }
  const pendingEl = $("recording-pending");
  if (status.pending_since && (status.reason === "changes_pending" || queue)) {
    pendingEl.textContent = t("recording.changes_pending", { time: i18n.formatRelative(status.pending_since) });
    pendingEl.hidden = false;
  } else {
    pendingEl.hidden = true;
  }
  const coalescedEl = $("recording-coalesced");
  const coalesced = status.events_coalesced || 0;
  if (coalesced > 0) {
    coalescedEl.textContent = t("recording.events_coalesced_warning", { count: coalesced });
    coalescedEl.hidden = false;
  } else {
    coalescedEl.hidden = true;
  }
  const paused = code === "paused";
  const disabled = code === "disabled" || code === "blocked";
  const canControl = Boolean(api.capabilities.autocapture) && Boolean(project);
  $("pause").hidden = paused || !canControl;
  $("pause").disabled = isPending("pause:global") || disabled ||
    !["recording", "baselining", "waiting_document", "waiting_session", "reconnecting"].includes(code);
  $("resume").hidden = !paused;
  $("resume").disabled = isPending("resume:global");
  $("cancel-save").textContent = t(queue ? "actions.cancel_organizing" : "actions.cancel_save");
  $("cancel-save").hidden = queue ? (!activeOrganizing || cancelSafetyGate) : code !== "saving";
  $("cancel-save").disabled = isPending("cancel_save") || (queue && cancelSafetyGate);
  if (status.document_id !== state.documentId) $("cancel-save").hidden = true;
  $("retry-capture").hidden = !queue || !(queue.blocked_count > 0) || Boolean(state.documentIndex.get(state.documentId)?.doc.read_only);
  $("retry-capture").disabled = isPending("retry_capture");
  $("milestone").disabled = isPending("milestone") || code !== "recording" || state.documentId !== status.document_id;
  $("milestone-title").disabled = $("milestone").disabled;
  const tech = $("recording-technical");
  tech.innerHTML = "";
  addFact(tech, t("tech.state_code"), `${code} / ${status.reason || ""}`);
  addFact(tech, t("tech.session"), status.session_instance ? `${status.session_instance.session_id} (pid ${status.session_instance.pid ?? "?"})` : "—");
  addFact(tech, t("tech.run_id"), status.capture_run_id || "—");
  addFact(tech, t("tech.last_checkpoint"), status.last_checkpoint_id || "—");
  addFact(tech, t("tech.export_ms"), status.export_ms == null ? "—" : `${status.export_ms} ms / ${i18n.formatBytes(status.layout_bytes)}`);
  addFact(tech, t("tech.policy_version"), String(status.policy_version ?? "—"));
  if (status.diagnostic) addFact(tech, t("tech.diagnostic"), JSON.stringify(status.diagnostic));
  if (gap) addFact(tech, t("tech.gap"), `${gap.code}: ${gap.reason || ""}`);
  const timing = status.timing_ms;
  if (timing && typeof timing === "object") {
    const timingFields = [
      ["scan_ms", "tech.timing_scan"], ["dedupe_ms", "tech.timing_dedupe"],
      ["full_compress_ms", "tech.timing_compress"], ["delta_encode_ms", "tech.timing_delta"],
      ["delta_verify_ms", "tech.timing_verify"], ["changes_ms", "tech.timing_changes"],
      ["pack_fsync_ms", "tech.timing_pack"], ["publish_ms", "tech.timing_publish"],
      ["db_commit_ms", "tech.timing_db"],
    ];
    for (const [field, key] of timingFields) {
      if (typeof timing[field] === "number") addFact(tech, t(key), `${Math.round(timing[field])} ms`);
    }
  }
}

// --------------------------------------------------------------- timeline ----
function renderTimeline() {
  $("history-import-open").disabled = !state.documentId;
  const list = $("checkpoints");
  list.innerHTML = "";
  $("filter").hidden = !state.segmentFilter;
  $("timeline-empty").hidden = state.checkpoints.length > 0;
  $("more-checkpoints").hidden = !state.checkpointsCursor;
  for (const cp of state.checkpoints) {
    const li = document.createElement("li");
    li.className = "item" + (cp.id === state.selectedCheckpoint ? " selected" : "");
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    li.dataset.id = cp.id;
    const title = document.createElement("div");
    title.className = "item-title";
    title.textContent = displayTitle(cp);
    const meta = document.createElement("div");
    meta.className = "item-meta";
    meta.textContent = `${cp.imported ? (cp.historical_at || t("import.unknown_date")) : i18n.formatTime(cp.created_at)} · ${i18n.formatBytes(cp.size)} · ${cp.imported ? t("import.imported") : t("source." + (cp.source || "unknown"))}`;
    li.append(title, meta);
    li.addEventListener("click", () => selectCheckpoint(cp.id));
    li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectCheckpoint(cp.id); } });
    list.appendChild(li);
  }
}

function displayTitle(cp) {
  if (cp.title_revision) return cp.title;
  const titles = {
    "Capture baseline": "checkpoint.baseline",
    "Observed editor changes": "checkpoint.observed",
    "Final observed editor changes": "checkpoint.final",
    "Observed KLayout changes": "checkpoint.observed",
    "Final observed KLayout changes": "checkpoint.final",
  };
  return titles[cp.title] ? t(titles[cp.title]) : cp.title;
}

async function loadMoreCheckpoints() {
  if (!state.checkpointsCursor) return;
  const gen = state.generation;
  const cursor = state.checkpointsCursor, filter = state.segmentFilter;
  try {
    const page = await guarded(gen, (s) => api.checkpoints(state.documentId, { limit: 50, segmentId: filter, cursor }, s));
    if (page === undefined || cursor !== state.checkpointsCursor || filter !== state.segmentFilter) return;
    state.checkpoints = state.checkpoints.concat(page.items);
    state.checkpointsCursor = page.next_cursor;
    renderTimeline();
  } catch (err) { toastError(err); }
}

async function selectCheckpoint(id, { keepPlaying = false } = {}) {
  if (!keepPlaying) { stopPlayback(); state.playbackDiffDeferred.delete(id); }
  else if (state.playing) state.playbackDiffDeferred.add(id);
  state.selectedCheckpoint = id;
  state.detail = null;
  persistSelection();
  renderTimeline();
  renderPreview();
  renderChanges();
  renderRecordedChanges();
  await Promise.all([loadDetail(), loadPreview(id), loadChanges(id), loadRecordedChanges(id)]);
}

// ---------------------------------------------------- recorded changes ----
// ChangeSet v1 (docs/CHANGESET_V1.md): the storage-recorded diff between two saved
// versions, read straight off disk (no KLayout, no live comparison job -- unlike the
// "changes"/"几何比较" block below, which runs a real geometry diff through a job).
/** Keep a per-document cache Map bounded: drop the oldest entries (insertion order) beyond
 *  `limit`, never the one for the selected version. Long histories browsed version by version
 *  otherwise accumulate every preview/diff/change page for the life of the tab. */
function boundCache(map, limit, keep) {
  for (const key of map.keys()) {
    if (map.size <= limit) return;
    if (key === keep) continue;
    map.delete(key);
  }
}
const CACHE_LIMITS = { previews: 48, diffs: 64, recordedChanges: 64 };

function recordedChangesEntry(id) {
  if (!state.recordedChanges.has(id)) {
    boundCache(state.recordedChanges, CACHE_LIMITS.recordedChanges - 1, state.selectedCheckpoint);
    state.recordedChanges.set(id, { status: "idle", root: null, items: [], nextCursor: null, error: null });
  }
  return state.recordedChanges.get(id);
}

async function loadRecordedChanges(id) {
  if (!id || !state.documentId) return;
  const entry = recordedChangesEntry(id);
  if (!api.capabilities.recorded_changes) {
    entry.status = "failed"; entry.error = { code: "SERVICE_UPDATE_REQUIRED", message: t("preview.service_update") };
    renderRecordedChanges(); return;
  }
  if (entry.status === "ready" || entry.status === "loading" || entry.status === "failed") { renderRecordedChanges(); return; }
  const gen = state.generation;
  entry.status = "loading";
  renderRecordedChanges();
  try {
    const page = await guarded(gen, (s) => api.changes(state.documentId, id, { limit: 50 }, s));
    if (page === undefined) return;
    entry.root = page.root || null;
    entry.recordedStatus = page.status;
    entry.items = page.items || [];
    entry.nextCursor = page.next_cursor || null;
    entry.entryCount = page.entry_count;
    entry.status = "ready";
  } catch (err) {
    if (err && err.name === "AbortError") return;
    entry.status = "failed";
    entry.error = err instanceof ApiError ? { code: err.code, message: err.message } : { code: "NETWORK", message: String(err) };
  }
  if (gen === state.generation && id === state.selectedCheckpoint) renderRecordedChanges();
}

async function loadMoreRecordedChanges() {
  const id = state.selectedCheckpoint;
  if (!id) return;
  const entry = recordedChangesEntry(id);
  if (!entry.nextCursor || entry.loadingMore) return;
  entry.loadingMore = true;
  const gen = state.generation;
  try {
    const page = await guarded(gen, (s) => api.changes(state.documentId, id, { limit: 50, cursor: entry.nextCursor }, s));
    if (page === undefined) return;
    entry.items = entry.items.concat(page.items || []);
    entry.nextCursor = page.next_cursor || null;
  } catch (err) {
    if (err && err.name === "AbortError") return;
    toastError(err);
  } finally {
    entry.loadingMore = false;
    if (gen === state.generation && id === state.selectedCheckpoint) renderRecordedChanges();
  }
}

// Kind strings below are ChangeSet v1 entry kinds (docs/CHANGESET_V1.md §3), single-quoted
// on purpose: they are NOT i18n keys, but their dotted shape would otherwise be mistaken
// for one by the app.js i18n-literal scan in tests/test_web_i18n.py.
function recordedKindLabel(kind) {
  const key = {
    'cell.added': "recorded_changes.kind_cell_added",
    'cell.removed': "recorded_changes.kind_cell_removed",
    'cell.changed': "recorded_changes.kind_cell_changed",
    'cell.timestamp_only': "recorded_changes.kind_cell_timestamp_only",
    'cell.reordered': "recorded_changes.kind_cell_reordered",
    'reference.changed': "recorded_changes.kind_reference_changed",
    'file.context_changed': "recorded_changes.kind_file_context_changed",
    'coverage.warning': "recorded_changes.kind_coverage_warning",
  }[kind];
  return key ? t(key) : kind;
}

function recordedEntryLine(e) {
  const label = recordedKindLabel(e.kind);
  if (e.kind === 'reference.changed') {
    return t("recorded_changes.line_reference", { name: e.name || "", target: e.target_name || "", label });
  }
  if (e.kind === 'cell.reordered') {
    return t("recorded_changes.line_reordered", { name: e.name || "", from: e.ordinal_before, to: e.ordinal_after, label });
  }
  if (e.kind === 'coverage.warning') {
    return `${label}: ${e.message || ""}`;
  }
  if (e.kind === 'file.context_changed') {
    return `${label}: ${e.scope || ""}`;
  }
  return e.name ? `${label}: ${e.name}` : label;
}

function recordedSummarySentence(root) {
  if (!root || !root.summary) return "";
  const s = root.summary, n = (v) => i18n.formatNumber(v);
  const parts = [];
  if (s.cells_added) parts.push(t("recorded_changes.summary_added", { count: n(s.cells_added) }));
  if (s.cells_removed) parts.push(t("recorded_changes.summary_removed", { count: n(s.cells_removed) }));
  if (s.cells_changed) parts.push(t("recorded_changes.summary_changed", { count: n(s.cells_changed) }));
  if (s.cells_timestamp_only) parts.push(t("recorded_changes.summary_timestamp_only", { count: n(s.cells_timestamp_only) }));
  if (s.cells_reordered) parts.push(t("recorded_changes.summary_reordered", { count: n(s.cells_reordered) }));
  if (s.units_changed) parts.push(t("recorded_changes.summary_units_changed"));
  return parts.length ? parts.join(t("changes.separator")) : t("recorded_changes.summary_none");
}

function renderRecordedChanges() {
  const hint = $("recorded-changes-storage-delta-hint");
  hint.hidden = api.capabilities.storage_delta !== false;   // shown only once we know it's explicitly off
  const id = state.selectedCheckpoint;
  const statusEl = $("recorded-changes-status"), summaryEl = $("recorded-changes-summary");
  const list = $("recorded-changes-list"), more = $("recorded-changes-more");
  list.innerHTML = ""; more.hidden = true;
  if (!id) { statusEl.textContent = ""; summaryEl.textContent = ""; return; }
  const entry = state.recordedChanges.get(id);
  if (!entry || entry.status === "idle" || entry.status === "loading") {
    delete statusEl.dataset.status;                       // no stale colour from the previous version
    statusEl.textContent = t("recorded_changes.loading");
    summaryEl.textContent = "";
    return;
  }
  if (entry.status === "failed") {
    statusEl.textContent = t("recorded_changes.failed");
    summaryEl.textContent = entry.error && entry.error.message ? entry.error.message : "";
    return;
  }
  const selected = state.checkpoints.find((c) => c.id === id);
  if (selected && Object.hasOwn(selected, "history_parent_id") && selected.history_parent_id !== selected.parent_id) {
    statusEl.textContent = t("import.basis_changed"); summaryEl.textContent = "";
    return;
  }
  const recStatus = entry.recordedStatus || "unavailable";
  statusEl.textContent = t(`recorded_changes.status_${recStatus}`);
  statusEl.dataset.status = recStatus;
  if (recStatus === "unavailable") {
    summaryEl.textContent = t("recorded_changes.unavailable_detail");
    return;
  }
  if (recStatus === "baseline") {
    summaryEl.textContent = t("recorded_changes.baseline_detail");
    return;
  }
  const countText = typeof entry.entryCount === "number"
    ? t("recorded_changes.entry_count", { count: i18n.formatNumber(entry.entryCount) })
    : "";
  const sentence = recordedSummarySentence(entry.root);
  summaryEl.textContent = [countText, sentence].filter(Boolean).join(t("changes.separator"));
  for (const e of entry.items) {
    const li = document.createElement("li");
    li.textContent = recordedEntryLine(e);
    list.appendChild(li);
  }
  more.hidden = !entry.nextCursor;
}

// ---------------------------------------------------------- changes ----
function previousCheckpointId(id) {
  const cp = state.checkpoints.find((c) => c.id === id);
  if (cp && Object.hasOwn(cp, "history_parent_id")) return cp.history_parent_id;
  const index = state.checkpoints.findIndex((c) => c.id === id);
  return index >= 0 && index + 1 < state.checkpoints.length ? state.checkpoints[index + 1].id : null;
}

function diffEntry(fromId, toId) {
  const key = `${fromId}>${toId}`;
  if (!state.diffs.has(key)) {
    boundCache(state.diffs, CACHE_LIMITS.diffs - 1, null);
    state.diffs.set(key, { status: "idle", data: null, error: null, requestId: crypto.randomUUID() });
  }
  return state.diffs.get(key);
}

/** Populate a preview/diff entry from a terminal job (succeeded or not) by fetching its asset.
 *  Shared by the initiating request's own wait AND by the background settling of a job whose
 *  local wait already gave up (see waitForJob / settleTrackedJob) -- so a job that keeps running
 *  after the page stopped watching it still lands in the entry whenever it finally completes. */
async function settleDiffJob(entry, job) {
  if (job.status !== "succeeded") {
    entry.status = "failed";
    entry.error = job.error || { code: "PREVIEW_WORKER_FAILED" };
    return;
  }
  try {
    const response = await fetch(api.assetUrl(job.id), { credentials: "same-origin" });
    if (!response.ok) throw new ApiError(response.status, await response.json().catch(() => null));
    const payload = await response.json();
    entry.data = payload.diff;
    entry.status = "ready";
  } catch (err) {
    entry.status = "failed";
    entry.error = err instanceof ApiError ? { code: err.code, message: err.message } : { code: "NETWORK", message: String(err) };
  }
}

async function loadChanges(id) {
  if (!id || !state.documentId) return;
  const fromId = previousCheckpointId(id);
  if (!fromId) { renderChanges(); return; }
  if (state.playing || state.playbackDiffDeferred.has(id)) { renderChanges(); return; }
  const entry = diffEntry(fromId, id);
  // A once-failed comparison is remembered and never auto-retried on the next poll; only a
  // fresh selection (a brand-new diffEntry) or the checkpoint list changing under it retries.
  if (entry.status === "ready" || entry.status === "loading" || entry.status === "failed") { renderChanges(); return; }
  if (!api.capabilities.preview) { entry.status = "unavailable"; renderChanges(); return; }
  const gen = state.generation;
  entry.status = "loading";
  renderChanges();
  try {
    const accepted = await guarded(gen, () => api.diff(state.documentId, fromId, id, entry.requestId));
    if (accepted === undefined) return;
    trackJob("diff", accepted, { checkpointId: id, fromId, quiet: true });
    const job = await waitForJob(accepted.job_id, gen);
    if (job === undefined) return;
    await settleDiffJob(entry, job);
  } catch (err) {
    if (err && err.name === "AbortError") return;
    entry.status = "failed";
    entry.error = err instanceof ApiError ? { code: err.code, message: err.message } : { code: "NETWORK", message: String(err) };
  }
  if (gen === state.generation && id === state.selectedCheckpoint) { renderChanges(); renderPreview(); }
}

function currentDiff() {
  const id = state.selectedCheckpoint;
  const fromId = id ? previousCheckpointId(id) : null;
  return fromId ? state.diffs.get(`${fromId}>${id}`) || null : null;
}

function changeSentence(summary) {
  if (summary.identical) return t("changes.identical");
  const parts = [];
  const n = (v) => i18n.formatNumber(v);
  if (summary.cells_changed) parts.push(t("changes.part.cells_changed", { count: n(summary.cells_changed) }));
  if (summary.cells_added) parts.push(t("changes.part.cells_added", { count: n(summary.cells_added) }));
  if (summary.cells_removed) parts.push(t("changes.part.cells_removed", { count: n(summary.cells_removed) }));
  if (summary.cells_renamed) parts.push(t("changes.part.cells_renamed", { count: n(summary.cells_renamed) }));
  if (summary.shapes_added || summary.shapes_removed) parts.push(t("changes.part.shapes", { added: n(summary.shapes_added), removed: n(summary.shapes_removed) }));
  if (summary.instances_added || summary.instances_removed || summary.instances_moved) {
    parts.push(t("changes.part.instances", { added: n(summary.instances_added), removed: n(summary.instances_removed), moved: n(summary.instances_moved) }));
  }
  if (summary.hierarchy_edges_added || summary.hierarchy_edges_removed) {
    parts.push(t("changes.part.hierarchy", { added: n(summary.hierarchy_edges_added), removed: n(summary.hierarchy_edges_removed) }));
  }
  return parts.length ? parts.join(t("changes.separator")) : t("changes.only_children");
}

function renderChanges() {
  const id = state.selectedCheckpoint;
  const summaryEl = $("changes-summary"), note = $("changes-note"), details = $("changes-details"), list = $("changes-list");
  note.hidden = true; details.hidden = true; list.innerHTML = "";
  const compare = $("changes-run");
  compare.hidden = !id || !state.playbackDiffDeferred.has(id);
  compare.disabled = state.playing;
  if (!id) { summaryEl.textContent = ""; return; }
  const fromId = previousCheckpointId(id);
  if (!fromId) { summaryEl.textContent = t("changes.first_version"); return; }
  const entry = state.diffs.get(`${fromId}>${id}`);
  if (!entry || entry.status === "idle") {
    summaryEl.textContent = state.playbackDiffDeferred.has(id) ? t("changes.playback_deferred") : "";
    return;
  }
  if (entry.status === "unavailable") { summaryEl.textContent = t("changes.unavailable"); return; }
  if (entry.status === "loading") { summaryEl.textContent = t("changes.loading"); return; }
  if (entry.status === "failed") {
    const code = (entry.error && entry.error.code) || "PREVIEW_WORKER_FAILED";
    const key = `errors.${code.toLowerCase()}`;
    summaryEl.textContent = i18n.has(key) ? t(key) : t("changes.failed");
    note.hidden = false; note.textContent = entry.error && entry.error.message ? entry.error.message : "";
    return;
  }
  const data = entry.data, summary = data.summary;
  summaryEl.textContent = changeSentence(summary);
  summaryEl.dataset.identical = String(Boolean(summary.identical));
  if (data.completeness !== "complete") { note.hidden = false; note.textContent = t("changes.partial"); }
  if (summary.identical) return;
  details.hidden = false;
  const block = (label, items, render) => {
    if (!items || !items.length) return;
    const h = document.createElement("h4"); h.textContent = label;
    const ul = document.createElement("ul"); ul.className = "change-list";
    for (const item of items) { const li = document.createElement("li"); li.textContent = render(item); ul.appendChild(li); }
    list.append(h, ul);
  };
  block(t("changes.cells_added_label"), data.cells.added, (name) => name);
  block(t("changes.cells_removed_label"), data.cells.removed, (name) => name);
  block(t("changes.renamed_label"), data.cells.renamed, (r) => t("changes.renamed_line", { from: r.from, to: r.to }));
  block(t("changes.changed_label"), data.cells.changed, (c) => {
    const bits = [c.name];
    if (c.children_changed && !c.own_content_changed) bits.push(t("changes.via_children"));
    for (const l of c.layers) bits.push(t("changes.layer_line", { layer: l.layer, datatype: l.datatype, added: l.added, removed: l.removed }));
    const ic = c.instances.counts;
    if (ic.added || ic.removed || ic.moved) bits.push(t("changes.instances_line", { added: ic.added, removed: ic.removed, moved: ic.moved }));
    return bits.join(" · ");
  });
  block(t("changes.hierarchy_label"), [
    ...data.hierarchy.edges_added.map((e) => t("changes.edge_added", { parent: e[0], child: e[1] })),
    ...data.hierarchy.edges_removed.map((e) => t("changes.edge_removed", { parent: e[0], child: e[1] })),
    ...data.hierarchy.reparented.map((r) => t("changes.reparented", { child: r.child, from: r.from_parents.join(", "), to: r.to_parents.join(", ") })),
  ], (line) => line);
}

// ---------------------------------------------------------- preview ----
function previewEntry(id) {
  if (!state.previews.has(id)) {
    boundCache(state.previews, CACHE_LIMITS.previews - 1, state.selectedCheckpoint);
    state.previews.set(id, { status: "idle", data: null, error: null, jobId: null, requestId: crypto.randomUUID() });
  }
  return state.previews.get(id);
}

/** Populate a preview entry from a terminal job (succeeded or not) by fetching its asset.
 *  Shared by the initiating request's own wait AND by the background settling of a job whose
 *  local wait already gave up (see waitForJob / settleTrackedJob). */
async function settlePreviewJob(entry, job) {
  if (job.status !== "succeeded") {
    entry.status = "failed";
    entry.error = job.error || { code: "PREVIEW_WORKER_FAILED" };
    return;
  }
  try {
    const response = await fetch(api.assetUrl(job.id), { credentials: "same-origin" });
    if (!response.ok) throw new ApiError(response.status, await response.json().catch(() => null));
    const payload = await response.json();
    entry.data = payload.preview;
    entry.summary = payload.summary;
    entry.status = "ready";
    if (!state.viewport && entry.data.bbox_dbu) state.viewport = fitViewport(entry.data.bbox_dbu);
  } catch (err) {
    entry.status = "failed";
    entry.error = err instanceof ApiError ? { code: err.code, message: err.message, message_key: err.messageKey } : { code: "NETWORK", message: String(err) };
  }
}

async function loadPreview(id) {
  if (!id || !state.documentId) return;
  const entry = previewEntry(id);
  // A once-failed preview is remembered (per checkpoint id) and never auto-retried by a
  // background poll -- only retryPreview() (the manual "Retry" button) clears it.
  if (entry.status === "ready" || entry.status === "loading" || entry.status === "failed") { renderPreview(); return; }
  const gen = state.generation;
  entry.status = "loading";
  entry.error = null;
  renderPreview();
  try {
    const readThumbnail = () => !api.capabilities.capture_screenshots ? null : guarded(gen, async (signal) => {
      const response = await fetch(api.thumbnailUrl(state.documentId, id),
        { credentials: "same-origin", signal });
      if (response.status === 204) return null;
      if (!response.ok) throw new ApiError(response.status, await response.json().catch(() => null));
      const blob = await response.blob();
      const badImage = () => new ApiError(503, { error: { code: "THUMBNAIL_UNREADABLE",
        message_key: "errors.thumbnail_unreadable" } });
      if (blob.size > 2 * 1024 * 1024 || blob.type !== "image/png") throw badImage();
      const url = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error("Cannot load screenshot"));
        reader.readAsDataURL(blob);
      });
      const image = new Image();
      image.src = url;
      try { await image.decode(); } catch (_) { throw badImage(); }
      if (image.naturalWidth > 1920 || image.naturalHeight > 1080) throw badImage();
      return { image, rendered: response.headers.get("X-Vestigraph-Image-Source") === "saved_layout_render" };
    });
    let thumbnail = await readThumbnail();
    let checkpoint = state.checkpoints.find((cp) => cp.id === id);
    if (!thumbnail && thumbnail !== undefined && !checkpoint) {
      checkpoint = await guarded(gen, (signal) => api.checkpoint(state.documentId, id, signal));
      if (checkpoint === undefined) return;
    }
    if (!thumbnail && thumbnail !== undefined && api.capabilities.generated_thumbnails &&
        checkpoint && (checkpoint.imported || checkpoint.size >= 1024 * 1024)) {
      const accepted = await guarded(gen, () => api.post("/documents/" + encodeURIComponent(state.documentId) + "/checkpoints/" + encodeURIComponent(id) + "/thumbnail", {}));
      if (accepted === undefined) return;
      const job = await waitForJob(accepted.job_id, gen);
      if (job === undefined) return;
      if (job.status !== "succeeded") throw new ApiError(409, { error: job.error || {code: "PREVIEW_WORKER_FAILED"} });
      thumbnail = await readThumbnail();
    }
    if (thumbnail === undefined) return;
    if (thumbnail) {
      entry.data = { raster: thumbnail.image, rendered: thumbnail.rendered, bbox_dbu: [0, 0, thumbnail.image.naturalWidth, thumbnail.image.naturalHeight],
        items: [{ kind: "raster" }], completeness: "complete" };
      entry.status = "ready";
      if (id === state.selectedCheckpoint) state.viewport = fitViewport(entry.data.bbox_dbu);
      // Bound decoded image retention independently of the number of timeline pages.
      const images = [...state.previews].filter(([key, value]) => key !== id && value.data?.raster);
      for (const [key] of images.slice(0, Math.max(0, images.length - 7))) state.previews.delete(key);
      renderPreview();
      return;
    }
    // A version outside the loaded timeline page (deep link, older page) is not "unknown":
    // ask the service for its record instead of treating it as too large to preview.

    if (!checkpoint) {
      checkpoint = await guarded(gen, (signal) => api.checkpoint(state.documentId, id, signal));
      if (checkpoint === undefined) return;
    }
    if (!checkpoint || checkpoint.size >= 1024 * 1024) {
      if (api.capabilities.thumbnail_status) {
        entry.imageStatus = await guarded(gen, (signal) => api.thumbnailStatus(state.documentId, id, signal));
        if (entry.imageStatus === undefined) return;
      }
      entry.status = "missing_image"; renderPreview(); return;
    }
    if (!api.capabilities.preview) { entry.status = "unavailable"; renderPreview(); return; }
    const accepted = await guarded(gen, () => api.preview(state.documentId, id, {}, entry.requestId));
    if (accepted === undefined) return;
    entry.jobId = accepted.job_id;
    trackJob("preview", accepted, { checkpointId: id, quiet: true });
    const job = await waitForJob(accepted.job_id, gen);
    if (job === undefined) return;
    await settlePreviewJob(entry, job);
  } catch (err) {
    if (err && err.name === "AbortError") return;
    entry.status = "failed";
    entry.error = err instanceof ApiError ? { code: err.code, message: err.message, message_key: err.messageKey } : { code: "NETWORK", message: String(err) };
  }
  if (gen === state.generation) renderPreview();
}

/** Retry a preview the user explicitly asked to retry (the "Retry" button): forgets the
 *  remembered failure for this checkpoint and issues a brand-new request. Never called by
 *  polling -- only by the click handler in wireEvents(). */
async function retryPreview() {
  const id = state.selectedCheckpoint;
  if (!id) return;
  state.previews.delete(id);
  await loadPreview(id);
}

async function waitForJob(jobId, gen) {
  for (let i = 0; i < 600; i += 1) {
    const job = await guarded(gen, (s) => api.job(jobId, s));
    if (job === undefined) return undefined;
    if (!["queued", "running"].includes(job.status)) {
      const tracked = state.jobs.get(jobId);
      if (tracked) { tracked.status = job.status; tracked.error = job.error; tracked.result = job.result; renderJobs(); }
      return job;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  // Give up watching locally without claiming the coordinator itself failed: the tracked job
  // keeps whatever status it last had ("queued"/"running"), so pollJobs() -> settleTrackedJob()
  // keeps polling it in the background and will settle this entry for real whenever it lands.
  return { id: jobId, status: "unknown", error: { code: "CLIENT_WAIT_TIMEOUT" } };
}

function renderPreview() {
  const id = state.selectedCheckpoint;
  const canvas = $("canvas");
  const note = $("preview-note");
  const status = $("preview-status");
  const retry = $("preview-retry");
  const entry = id ? state.previews.get(id) : null;
  const index = state.checkpoints.findIndex((c) => c.id === id);
  $("step-older").disabled = index < 0 || index >= state.checkpoints.length - 1;
  $("step-newer").disabled = index <= 0;
  $("play").disabled = index < 0 || (index <= 0 && !state.playing);
  $("play").textContent = t(state.playing ? "preview.stop" : "preview.play");
  $("fit").disabled = !(entry && entry.status === "ready" && entry.data && entry.data.bbox_dbu);
  note.hidden = true;
  retry.hidden = true;
  const checkpoint = state.checkpoints[index];
  $("zoom-in").disabled = $("zoom-out").disabled = !(entry && entry.status === "ready");
  $("preview-caption").textContent = checkpoint ? t("preview.shows_saved_at", { time: i18n.formatTime(checkpoint.created_at) }) : "";
  if (!entry) { draw(canvas, null, null); status.textContent = ""; return; }
  if (entry.status === "missing_image") {
    draw(canvas, null, null);
    status.textContent = t("preview.missing_image");
    note.hidden = false;
    const reasons = {
      identity_changed: "preview.image_identity_changed", timeout: "preview.image_timeout",
      invalid_image: "preview.image_invalid", storage_failed: "preview.image_storage_failed",
      unexpected: "preview.image_failed", unsupported: "preview.image_unsupported",
      cancelled: "preview.image_cancelled",
    };
    note.textContent = t(reasons[entry.imageStatus?.reason_code] || "preview.missing_image_detail");
    retry.hidden = false; retry.disabled = false;
    return;
  }
  if (entry.status === "unavailable") {
    draw(canvas, null, null);
    status.textContent = t("preview.unavailable_dependency");
    return;
  }
  if (entry.status === "loading") { status.textContent = t("preview.rendering"); return; }
  if (entry.status === "failed") {
    draw(canvas, null, null);
    const code = (entry.error && entry.error.code) || "PREVIEW_WORKER_FAILED";
    const key = `errors.${code.toLowerCase()}`;
    status.textContent = i18n.has(key) ? t(key) : t("preview.failed");
    note.hidden = false;
    note.textContent = entry.error && entry.error.message ? entry.error.message : "";
    retry.hidden = false;
    retry.disabled = false;
    return;
  }
  const data = entry.data;
  const viewportKind = data.raster ? "image_pixels" : "layout_dbu";
  if (state.viewportKind !== viewportKind) {
    state.viewport = fitViewport(data.bbox_dbu);
    state.viewportKind = viewportKind;
  }
  const diff = currentDiff();
  const highlights = !data.raster && diff && diff.status === "ready" && !diff.data.summary.identical ? diff.data.highlight_bboxes_dbu : null;
  const result = draw(canvas, data, state.viewport || fitViewport(data.bbox_dbu), highlights);
  canvas.dataset.highlighted = String(result.highlighted || 0);
  canvas.dataset.items = String(result.drawn);
  canvas.dataset.checkpoint = id;
  if (!data.items || data.items.length === 0) status.textContent = t("preview.empty");
  else status.textContent = t("preview.items", { count: i18n.formatNumber(data.items.length) });
  if (data.raster) {
    status.textContent = t(data.rendered ? "preview.generated_image" : "preview.screenshot");
    note.hidden = false;
    note.textContent = t(data.rendered ? "preview.generated_image_detail" : "preview.screenshot_detail");
  }
  if (data.completeness !== "complete") {
    note.hidden = false;
    const reasons = (data.warnings || []).map((w) => (i18n.has(`preview.warning.${w.code}`) ? t(`preview.warning.${w.code}`) : w.code));
    note.textContent = t("preview.partial") + (reasons.length ? " " + reasons.join(" ") : "");
  }
}

function stepPreview(direction) {
  const index = state.checkpoints.findIndex((c) => c.id === state.selectedCheckpoint);
  const next = index + direction;           // list is newest first: +1 = older, -1 = newer
  if (index < 0 || next < 0 || next >= state.checkpoints.length) return false;
  selectCheckpoint(state.checkpoints[next].id, { keepPlaying: true });
  return true;
}

function togglePlayback() {
  if (state.playing) { stopPlayback(); renderPreview(); return; }
  state.playing = true;
  renderPreview();
  const tick = () => {
    if (!state.playing) return;
    const entry = state.previews.get(state.selectedCheckpoint);
    if (!entry || entry.status === "loading") { state.playTimer = setTimeout(tick, 250); return; }
    if (entry.status === "failed" || entry.status === "unavailable") { stopPlayback(); renderPreview(); return; }  // never skip a failed version
    if (!stepPreview(-1)) { stopPlayback(); renderPreview(); return; }        // reached the newest version
    state.playTimer = setTimeout(tick, 1000);
  };
  state.playTimer = setTimeout(tick, 1000);
}

function stopPlayback() {
  state.playing = false;
  if (state.playTimer) { clearTimeout(state.playTimer); state.playTimer = null; }
  renderChanges();
}

function fitPreview() {
  const entry = state.previews.get(state.selectedCheckpoint);
  if (entry && entry.status === "ready" && entry.data.bbox_dbu) { state.viewport = fitViewport(entry.data.bbox_dbu); renderPreview(); }
}

// ------------------------------------------------- open in KLayout ----
async function prepareOpenSessions() {
  const field = $("open-session-field");
  const select = $("open-session");
  if (!api.capabilities.open_history) { field.hidden = true; return; }
  try {
    const data = await api.sessions();
    const online = data.items.filter((s) => s.online);
    select.innerHTML = "";
    for (const session of online) {
      const option = document.createElement("option");
      option.value = session.session_id;
      option.dataset.pid = session.pid == null ? "" : String(session.pid);
      option.dataset.instance = session.session_instance_id || option.dataset.pid;
      option.dataset.backend = session.backend_id || "klayout";
      option.textContent = `${session.session_id}${session.layout_path ? " · " + baseName(session.layout_path) : ""}`;
      select.appendChild(option);
    }
    const bound = state.projectStatus && state.projectStatus.session_instance;
    if (bound && online.some((s) => s.session_id === bound.session_id)) select.value = bound.session_id;
    field.hidden = online.length <= 1;
    $("open-klayout").disabled = online.length === 0 || isPending("open_in_klayout");
    $("open-klayout-hint").textContent = online.length === 0 ? t("open.no_window") : "";
  } catch (err) {
    field.hidden = true;
    $("open-klayout").disabled = true;
    $("open-klayout-hint").textContent = describeError(err).text;
  }
}

async function doOpenInKlayout() {
  if (!state.selectedCheckpoint) return;
  const select = $("open-session");
  const option = select.options[select.selectedIndex];
  if (!option) { toast(t("open.no_window"), "bad"); return; }
  const checkpoint = state.checkpoints.find((c) => c.id === state.selectedCheckpoint) || state.detail;
  if (!window.confirm(t("open.confirm", { name: checkpoint ? displayTitle(checkpoint) : "", window: option.value }))) return;
  const requestId = crypto.randomUUID();
  beginPending("open_in_klayout");
  $("open-klayout").disabled = true;
  try {
    const generic = !!api.capabilities.open_in_editor;
    const open = generic ? api.openInEditor.bind(api) : api.openInKlayout.bind(api);
    const accepted = await open(state.documentId, state.selectedCheckpoint, {
      session_id: option.value, expected_session_instance: option.dataset.instance || null, confirm_new_tab: true,
      ...(generic ? {backend_id: option.dataset.backend} : {}),
    }, requestId);
    trackJob("open_in_klayout", accepted, { checkpointId: state.selectedCheckpoint });
  } catch (err) { toastError(err); }
  finally {
    endPending("open_in_klayout");
    const open = api.capabilities.open_history;
    $("open-klayout").disabled = !open;
    if (open) prepareOpenSessions();
  }
}

async function loadDetail() {
  const gen = state.generation;
  const id = state.selectedCheckpoint;
  if (!id || !state.documentId) { renderDetail(); return; }
  try {
    const [detail, notes] = await Promise.all([
      guarded(gen, (s) => api.checkpoint(state.documentId, id, s)),
      guarded(gen, (s) => api.annotations(state.documentId, "checkpoint", id, s)),
    ]);
    if (detail === undefined || id !== state.selectedCheckpoint) return;
    state.detail = detail;
    state.annotations = notes.items;
  } catch (err) {
    if (err.name === "AbortError") return;
    if (err instanceof ApiError && err.status === 404) { state.selectedCheckpoint = null; persistSelection(); }
    else toastError(err);
    state.detail = null;
  }
  renderDetail();
  renderDocBreadcrumb();
}

function renderDetail() {
  const detail = state.detail;
  $("detail-empty").hidden = Boolean(detail);
  $("detail").hidden = !detail;
  if (!detail) return;
  const facts = $("facts");
  facts.innerHTML = "";
  addFact(facts, t("detail.name"), displayTitle(detail));
  const name = $("version-name");
  if (name.dataset.checkpoint !== detail.id || document.activeElement !== name) name.value = detail.title;
  name.dataset.checkpoint = detail.id;
  const readOnly = state.documentIndex.get(state.documentId)?.doc.read_only;
  $("rename-form").hidden = !api.capabilities.checkpoint_rename;
  name.disabled = $("rename-version").disabled = Boolean(readOnly) || isPending("rename");
  addFact(facts, t(detail.imported ? "import.imported_at" : "detail.saved_at"), i18n.formatTime(detail.created_at));
  if (detail.imported) addFact(facts, t("import.date"), detail.historical_at || t("import.unknown_date"));
  addFact(facts, t("detail.file"), detail.document_filename || detail.filename);
  addFact(facts, t("detail.size"), `${i18n.formatBytes(detail.size)} · ${detail.format || "?"}`);
  addFact(facts, t("detail.source"), t("source." + (detail.source || "unknown")));
  const segment = state.segments.find((s) => s.id === detail.segment_id);
  if (segment) addFact(facts, t("detail.activity"), displaySegment(segment));
  const tech = $("technical");
  tech.innerHTML = "";
  addFact(tech, "id", detail.id);
  if (detail.original_title !== detail.title) addFact(tech, t("detail.original_name"), detail.original_title);
  if (detail.presentation_warning) addFact(tech, t("detail.display_warning"), t("detail.display_warning_detail"));
  addFact(tech, "sha256", detail.sha256);
  addFact(tech, "parent", detail.parent_id || "—");
  addFact(tech, "segment", detail.segment_id || "—");
  addFact(tech, "chunks", String(detail.chunk_count));
  addFact(tech, "coverage", detail.coverage || "—");
  const timing = detail.metadata && detail.metadata.timing;
  if (timing) addFact(tech, "timing", JSON.stringify(timing));
  const open = api.capabilities.open_history;
  $("open-klayout").disabled = !open || isPending("open_in_klayout");
  $("open-klayout-hint").textContent = open ? "" : t("detail.open_history_unavailable");
  if (open) prepareOpenSessions();
  const notes = $("annotations");
  notes.innerHTML = "";
  for (const note of state.annotations) {
    const li = document.createElement("li");
    const text = document.createElement("div"); text.textContent = note.text;
    const meta = document.createElement("div"); meta.className = "item-meta"; meta.textContent = i18n.formatTime(note.created_at);
    li.append(text, meta);
    notes.appendChild(li);
  }
}

function addFact(dl, label, value) {
  const dt = document.createElement("dt"); dt.textContent = label;
  const dd = document.createElement("dd"); dd.textContent = value;
  dl.append(dt, dd);
}

// ------------------------------------------------------- activities ----
function renderActivities() {
  const list = $("activities");
  list.innerHTML = "";
  $("activities-empty").hidden = state.segments.length > 0;
  $("more-activities").hidden = !state.segmentsCursor;
  for (const seg of state.segments) {
    const li = document.createElement("li");
    li.className = "item" + (seg.id === state.segmentFilter ? " selected" : "");
    li.tabIndex = 0; li.setAttribute("role", "button");
    const title = document.createElement("div"); title.className = "item-title"; title.textContent = displaySegment(seg);
    const status = document.createElement("span");
    status.className = "tag tag-" + seg.status;
    status.textContent = t("segment." + seg.status);
    title.appendChild(status);
    const meta = document.createElement("div"); meta.className = "item-meta";
    meta.textContent = `${i18n.formatTime(seg.started_at)} · ${t("activities.counts", { versions: seg.checkpoint_count, events: seg.event_count })}`;
    li.append(title, meta);
    const toggle = () => { state.segmentFilter = state.segmentFilter === seg.id ? null : seg.id; persistSelection(); state.lastListSignature = ""; renderActivities(); pollLists(); };
    li.addEventListener("click", toggle);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
    list.appendChild(li);
  }
}

function displaySegment(seg) {
  const titles = {
    "KLink capture baseline": "activity.baseline",
    "Observed KLayout edits": "activity.observed",
    "Observed editor edits": "activity.observed",
    "KLink capture stopped": "activity.stopped",
    "Interrupted KLink capture": "activity.interrupted",
  };
  return titles[seg.title] ? t(titles[seg.title]) : seg.title;
}

async function loadMoreActivities() {
  if (!state.segmentsCursor) return;
  const gen = state.generation;
  const cursor = state.segmentsCursor;
  try {
    const page = await guarded(gen, (s) => api.segments(state.documentId, { limit: 50, cursor }, s));
    if (page === undefined || cursor !== state.segmentsCursor) return;
    state.segments = state.segments.concat(page.items);
    state.segmentsCursor = page.next_cursor;
    renderActivities();
  } catch (err) { toastError(err); }
}

function renderEvents() {
  const list = $("events");
  list.innerHTML = "";
  $("events-empty").hidden = state.events.length > 0;
  for (const ev of state.events) {
    const li = document.createElement("li");
    const title = document.createElement("div"); title.className = "item-title";
    title.textContent = i18n.has("event." + ev.kind) ? t("event." + ev.kind) : ev.kind;
    const meta = document.createElement("div"); meta.className = "item-meta";
    const parts = [i18n.formatTime(ev.created_at), t("source." + (ev.source || "unknown"))];
    if (ev.truncated) parts.push(t("events.truncated"));
    if (ev.summary && ev.summary.caused_by && ev.summary.caused_by.length) parts.push(ev.summary.caused_by.join(", "));
    meta.textContent = parts.join(" · ");
    li.append(title, meta);
    list.appendChild(li);
  }
}

// ------------------------------------------------------------- jobs ----
function trackJob(kind, accepted, extra = {}) {
  const job = { id: accepted.job_id, kind, status: accepted.status, result: null, error: null, ...extra };
  state.jobs.set(job.id, job);
  renderJobs();
  return job;
}

/** Route a terminal job (from the polling loop, i.e. NOT the request that originally submitted
 *  it -- that path settles preview/diff entries itself via settlePreviewJob/settleDiffJob) to
 *  wherever it needs to land. Preview/diff jobs update their entry (and re-render if the
 *  checkpoint is still selected) instead of toasting -- this is also how a job that outlived a
 *  client-side wait timeout (CLIENT_WAIT_TIMEOUT) eventually surfaces its real result. */
async function settleTrackedJob(job, fresh) {
  if (job.kind === "preview" && job.checkpointId) {
    const entry = previewEntry(job.checkpointId);
    if (entry.status !== "ready") {
      await settlePreviewJob(entry, fresh);
      if (job.checkpointId === state.selectedCheckpoint) renderPreview();
    }
    return;
  }
  if (job.kind === "diff" && job.checkpointId && job.fromId) {
    const entry = diffEntry(job.fromId, job.checkpointId);
    if (entry.status !== "ready") {
      await settleDiffJob(entry, fresh);
      if (job.checkpointId === state.selectedCheckpoint) { renderChanges(); renderPreview(); }
    }
    return;
  }
  if (fresh.status === "succeeded") onJobSucceeded(job);
  else toastJob(job);
}

function onJobSucceeded(job) {
  if (job.kind === "download") {
    const filename = (job.result && job.result.filename) || "";
    const link = document.createElement("a");
    link.href = "#";
    link.textContent = t("jobs.download_ready", { name: filename });
    link.addEventListener("click", (e) => { e.preventDefault(); triggerDownload(job.id, filename); });
    toast(link, "ok");
  } else if (job.kind === "milestone") {
    toast(t("toast.named_saved", { title: job.result && job.result.title }), "ok");
    state.lastListSignature = ""; pollLists();
  } else if (job.kind === "pause" || job.kind === "resume") {
    toast(t(job.kind === "pause" ? "toast.paused" : "toast.resumed"), "ok");
    pollStatus();
    refreshSessionsAndRerender();
  } else if (["open_in_klayout", "open_in_editor"].includes(job.kind)) {
    toast(t("toast.opened_in_klayout", { window: (job.result && job.result.session_id) || "" }), "ok");
    pollStatus();
  } else if (job.kind === "relocate") {
    toast(t("toast.storage_moved", { path: (job.result && job.result.new_root) || "" }), "ok");
    $("storage-path").value = "";
    loadStorageStatus(state.generation);
  }
}

function toastJob(job) {
  if (job.quiet) return;                       // preview failures are shown in the preview area
  const error = job.error || {};
  if (["open_in_klayout", "open_in_editor"].includes(job.kind) && job.status === "unknown") {
    toast(t("toast.open_unknown"), "bad", error);
    return;
  }
  toast(describeError(new ApiError(0, { error })).text, "bad", error);
}

function renderJobs() {
  const list = $("jobs");
  list.innerHTML = "";
  const jobs = Array.from(state.jobs.values()).slice(-8).reverse();
  for (const job of jobs) {
    const li = document.createElement("li");
    const title = document.createElement("div"); title.className = "item-title";
    title.textContent = t("jobs.kind." + job.kind);
    const status = document.createElement("span"); status.className = "tag tag-" + job.status; status.textContent = t("jobs.status." + job.status);
    title.appendChild(status);
    li.appendChild(title);
    if (job.kind === "download" && job.status === "succeeded") {
      const filename = (job.result && job.result.filename) || "";
      const link = document.createElement("a");
      link.href = "#"; link.textContent = t("jobs.download_link");
      link.addEventListener("click", (e) => { e.preventDefault(); triggerDownload(job.id, filename); });
      li.appendChild(link);
    }
    if (job.error) {
      const meta = document.createElement("div"); meta.className = "item-meta"; meta.textContent = describeError(new ApiError(0, { error: job.error })).text;
      li.appendChild(meta);
    }
    list.appendChild(li);
  }
}

// ---------------------------------------------------------- actions ----
// Every write action below disables its own button(s) for the span of its HTTP request only
// (not the job's whole lifecycle -- that's tracked separately in the Tasks panel), so a double
// click can never mint two request ids / two jobs. Buttons whose `disabled` a periodic render
// also computes (pause/resume/milestone, window pause/resume, storage) go through
// beginPending/endPending + isPending() so a poll mid-flight can't re-enable them; buttons no
// render function touches (download, annotate, open-in-klayout's own re-render already guards
// itself) are toggled directly.

function parseFilenameFromDisposition(value) {
  if (!value) return null;
  const star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(value);
  if (star) { try { return decodeURIComponent(star[1]); } catch (_) { /* fall through */ } }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(value);
  return plain ? plain[1] : null;
}

/** Download an asset through the browser's own download path (a same-origin <a download>),
 *  never through fetch + Blob: that held the whole file in memory (a GB layout twice over) and
 *  its "fallback" link never ran. An expired link (410 with a JSON error body) is still caught
 *  and toasted: the link is checked with a HEAD request first, so the browser never navigates
 *  to raw JSON in place of the panel. */
async function triggerDownload(jobId, fallbackFilename) {
  try {
    const url = api.assetUrl(jobId);
    const probe = await fetch(url, { method: "HEAD", credentials: "same-origin" });
    if (!probe.ok) {
      let body = null;
      try { body = await (await fetch(url, { credentials: "same-origin" })).json(); } catch (_) { body = null; }
      throw new ApiError(probe.status, body, probe.headers.get("X-Request-ID"));
    }
    const filename = parseFilenameFromDisposition(probe.headers.get("Content-Disposition")) || fallbackFilename || "download";
    const link = document.createElement("a");
    link.href = url; link.download = filename; link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
  } catch (err) {
    toastError(err);
  }
}

async function doRelocate(event) {
  event.preventDefault();
  await relocateTo($("storage-path").value.trim());
}

async function relocateTo(path) {
  if (!path) { toast(t("storage.path_required"), "bad"); return; }
  if (!window.confirm(t("storage.confirm", { path }))) return;
  const requestId = crypto.randomUUID();
  beginPending("relocate");
  renderStorage();
  try {
    trackJob("relocate", await api.relocate(state.storageProjectId, path, requestId));
    toast(t("storage.moving"), "info");
  } catch (err) { toastError(err); }
  finally { endPending("relocate"); renderStorage(); }
}

// Native "choose folder" dialog opened by the service on this desktop (a browser cannot
// read a folder's real path). The picked folder goes straight to the confirm + move flow.
async function doBrowseStorage() {
  const button = $("storage-browse");
  button.disabled = true;
  toast(t("storage.picking"), "info");
  try {
    const result = await api.browseStorage(state.storageProjectId, t("storage.dialog_title"));
    if (result.cancelled || !result.path) return;
    $("storage-path").value = result.path;
    await relocateTo(result.path);
  } catch (err) {
    if (err instanceof ApiError && err.code === "NO_NATIVE_DIALOG") {
      toast(t("storage.browse_unavailable"), "bad", err.message);
      $("storage-path").focus();
    } else toastError(err);
  } finally { button.disabled = $("storage-change").disabled; }
}

async function doPause() {
  const requestId = crypto.randomUUID();
  beginPending("pause:global");
  renderRecording();
  try { trackJob("pause", await api.pause(state.docProjectId, "user", requestId)); }
  catch (err) { toastError(err); }
  finally { endPending("pause:global"); renderRecording(); }
}

async function doResume() {
  const requestId = crypto.randomUUID();
  beginPending("resume:global");
  renderRecording();
  try { trackJob("resume", await api.resume(state.docProjectId, requestId)); }
  catch (err) { toastError(err); }
  finally { endPending("resume:global"); renderRecording(); }
}

async function doMilestone(event) {
  event.preventDefault();
  const title = $("milestone-title").value.trim();
  if (!title) { toast(t("toast.title_required"), "bad"); return; }
  const requestId = crypto.randomUUID();
  beginPending("milestone");
  renderRecording();
  try {
    trackJob("milestone", await api.milestone(state.documentId, title, requestId));
    $("milestone-title").value = "";
  } catch (err) { toastError(err); }
  finally { endPending("milestone"); renderRecording(); }
}

async function doCancelSave() {
  if (!state.documentId) return;
  const requestId = crypto.randomUUID();
  beginPending("cancel_save");
  renderRecording();
  try {
    const result = await api.cancelSave(state.documentId, requestId);
    if (!result.cancelled) toast(result.note || t("recording.cancel_save_no_op"), "info");
  } catch (err) {
    toastError(err);
  } finally {
    endPending("cancel_save");
    renderRecording();
  }
}

async function doRetryCapture() {
  if (!state.documentId) return;
  beginPending("retry_capture");
  renderRecording();
  try {
    await api.retryCapture(state.documentId, crypto.randomUUID());
    await pollLists();
    await pollStatus();
  } catch (err) { toastError(err); }
  finally { endPending("retry_capture"); renderRecording(); }
}

async function doDownload() {
  if (!state.selectedCheckpoint) return;
  const requestId = crypto.randomUUID();
  const button = $("download");
  button.disabled = true;
  try { trackJob("download", await api.download(state.documentId, state.selectedCheckpoint, requestId), { checkpointId: state.selectedCheckpoint }); }
  catch (err) { toastError(err); }
  finally { button.disabled = false; }
}

async function doAnnotate(event) {
  event.preventDefault();
  const text = $("note-text").value.trim();
  if (!text || !state.selectedCheckpoint) return;
  const requestId = crypto.randomUUID();
  const button = document.querySelector("#note-form button[type=submit]");
  if (button) button.disabled = true;
  try {
    await api.annotate(state.documentId, "checkpoint", state.selectedCheckpoint, text, requestId);
    $("note-text").value = "";
    await loadDetail();
  } catch (err) { toastError(err); }
  finally { if (button) button.disabled = false; }
}

async function setLanguage(lang) {
  if (lang === i18n.language()) return;
  await i18n.load(lang);
  i18n.applyTo(document);
  document.querySelectorAll("[data-lang]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lang === lang)));
  // Re-render from state only: no requests, no job re-submission, no selection loss.
  if (state.route === "doc") {
    renderRecording(); renderTimeline(); renderActivities(); renderEvents(); renderDetail();
    renderJobs(); renderPreview(); renderChanges(); renderRecordedChanges(); renderDocBreadcrumb();
  } else if (state.route === "windows") {
    renderWindowsView();
  } else if (state.route === "window") {
    renderWindowView();
  } else if (state.route === "docs") {
    renderDocsView();
  }
}

// ----------------------------------------------------------- toasts ----
function describeError(err) {
  if (!(err instanceof ApiError)) return { text: String(err && err.message || err), raw: String(err) };
  const key = err.messageKey && i18n.has(err.messageKey) ? err.messageKey : (i18n.has(`errors.${err.code.toLowerCase()}`) ? `errors.${err.code.toLowerCase()}` : "errors.generic");
  const nextKey = err.nextActionKey && i18n.has(err.nextActionKey) ? err.nextActionKey : (i18n.has(`errors.${err.code.toLowerCase()}.next`) ? `errors.${err.code.toLowerCase()}.next` : null);
  const text = t(key, err.messageArgs) + (nextKey ? " " + t(nextKey) : (key === "errors.generic" && err.nextAction ? " " + err.nextAction : ""));
  return { text, raw: `${err.code}: ${err.message}${err.requestId ? " (request " + err.requestId + ")" : ""}` };
}

function toastError(err) {
  if (err && err.name === "AbortError") return;
  const { text, raw } = describeError(err);
  toast(text, "bad", raw);
}

const MAX_TOASTS = 5;

function toastKey(kind, content) {
  const text = typeof content === "string" ? content : (content && content.textContent) || "";
  return kind + "\0" + text;
}

/** Add a toast, or -- if an identical one (same kind + visible text) is already showing --
 *  just refresh its auto-dismiss timer instead of piling on a duplicate. Also caps the toast
 *  container so a repeating failure (e.g. the service being unreachable) can never grow it
 *  without bound: the oldest toast is dropped once the cap is exceeded. */
function toast(content, kind = "info", raw = null) {
  const key = toastKey(kind, content);
  const container = $("toasts");
  const existing = Array.from(container.children).find((el) => el.dataset.toastKey === key);
  if (existing) {
    if (existing._dismissTimer) clearTimeout(existing._dismissTimer);
    existing._dismissTimer = kind !== "bad" ? setTimeout(() => existing.remove(), 8000) : null;
    return;
  }
  const box = document.createElement("div");
  box.className = "toast toast-" + kind;
  box.dataset.toastKey = key;
  box.setAttribute("role", kind === "bad" ? "alert" : "status");
  const body = document.createElement("div");
  if (typeof content === "string") body.textContent = content; else body.appendChild(content);
  box.appendChild(body);
  if (raw) {
    const details = document.createElement("details");
    const summary = document.createElement("summary"); summary.textContent = t("toast.raw");
    const pre = document.createElement("pre"); pre.textContent = typeof raw === "string" ? raw : JSON.stringify(raw, null, 2);
    details.append(summary, pre);
    box.appendChild(details);
  }
  const close = document.createElement("button");
  close.type = "button"; close.className = "close"; close.setAttribute("aria-label", t("a11y.dismiss")); close.textContent = "×";
  close.addEventListener("click", () => box.remove());
  box.appendChild(close);
  container.appendChild(box);
  if (kind !== "bad") box._dismissTimer = setTimeout(() => box.remove(), 8000);
  while (container.children.length > MAX_TOASTS) container.removeChild(container.firstChild);
}

function baseName(path) { return String(path).split(/[\\/]/).pop(); }

// ------------------------------------------------------------ wiring ----
async function renameVersion(event) {
  event.preventDefault();
  const detail = state.detail, did = state.documentId, gen = state.generation;
  if (!detail || isPending("rename")) return;
  const title = $("version-name").value.trim();
  if (!title) return;
  beginPending("rename"); $("rename-version").disabled = true;
  try {
    const result = await api.renameCheckpoint(did, detail.id, title, detail.title_revision || 0);
    if (gen !== state.generation) return;
    for (const cp of state.checkpoints) if (cp.id === detail.id) Object.assign(cp, result);
    if (state.detail?.id === detail.id) Object.assign(state.detail, result);
    renderTimeline(); renderDetail();
  } catch (error) {
    toastError(error);
    if (gen === state.generation) await loadDetail();
  } finally { endPending("rename"); if (gen === state.generation) renderDetail(); }
}

function wireViewport() {
  const canvas = $("canvas"), pointers = new Map();
  canvas.style.touchAction = "none";
  canvas.style.cursor = "grab";
  function ready() {
    return state.previews.get(state.selectedCheckpoint)?.status === "ready" && state.viewport;
  }
  function zoom(factor, x, y) {
    if (!ready()) return;
    const rect = canvas.getBoundingClientRect(), v = state.viewport;
    const w = v[2] - v[0], h = v[3] - v[1];
    const bbox = state.previews.get(state.selectedCheckpoint).data.bbox_dbu;
    const base = Math.max(1, bbox[2] - bbox[0]);
    const next = Math.max(base / 32, Math.min(base * 16, w * factor));
    factor = next / w;
    const scale = Math.min(rect.width / w, rect.height / h);
    const px = x == null ? rect.width / 2 : x - rect.left;
    const py = y == null ? rect.height / 2 : y - rect.top;
    const ax = v[0] + (px - (rect.width - w * scale) / 2) / scale;
    const ay = v[1] + (rect.height - py - (rect.height - h * scale) / 2) / scale;
    state.viewport = [ax + (v[0] - ax) * factor, ay + (v[1] - ay) * factor,
      ax + (v[2] - ax) * factor, ay + (v[3] - ay) * factor];
    canvas.dataset.zoom = String(base / next);
    renderPreview();
  }
  $("zoom-in").addEventListener("click", () => zoom(0.8));
  $("zoom-out").addEventListener("click", () => zoom(1.25));
  canvas.addEventListener("wheel", (event) => {
    if (!ready()) return;
    event.preventDefault(); zoom(Math.exp(Math.max(-1, Math.min(1, event.deltaY * 0.002))), event.clientX, event.clientY);
  }, { passive: false });
  canvas.addEventListener("dblclick", fitPreview);
  canvas.addEventListener("pointerdown", (event) => {
    if (!ready() || (event.pointerType === "mouse" && event.button !== 0)) return;
    pointers.set(event.pointerId, [event.clientX, event.clientY]);
    canvas.setPointerCapture(event.pointerId);
    canvas.style.cursor = "grabbing";
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!pointers.has(event.pointerId) || !ready()) return;
    const old = [...pointers.values()], prior = pointers.get(event.pointerId);
    pointers.set(event.pointerId, [event.clientX, event.clientY]);
    if (pointers.size === 2) {
      const now = [...pointers.values()];
      const distance = (p) => Math.hypot(p[0][0] - p[1][0], p[0][1] - p[1][1]);
      if (distance(now) > 1 && distance(old) > 1) zoom(distance(old) / distance(now),
        (now[0][0] + now[1][0]) / 2, (now[0][1] + now[1][1]) / 2);
    } else if (pointers.size === 1) {
      const rect = canvas.getBoundingClientRect(), v = state.viewport;
      const scale = Math.min(rect.width / (v[2] - v[0]), rect.height / (v[3] - v[1]));
      const dx = (event.clientX - prior[0]) / scale, dy = (event.clientY - prior[1]) / scale;
      state.viewport = [v[0] - dx, v[1] + dy, v[2] - dx, v[3] + dy];
      canvas.dataset.panned = "true"; renderPreview();
    }
  });
  for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) canvas.addEventListener(name, (event) => {
    pointers.delete(event.pointerId); if (!pointers.size) canvas.style.cursor = "grab";
  });
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(() => {
    if (state.selectedCheckpoint && !$("detail").hidden) renderPreview();
  }).observe(canvas);
}

function wireEvents() {
  wireViewport();
  $("rename-form").addEventListener("submit", renameVersion);
  $("goto-docs").addEventListener("click", navigateDocs);
  $("window-back").addEventListener("click", navigateWindows);
  $("docs-back").addEventListener("click", navigateWindows);
  $("window-pause").addEventListener("click", () => {
    const session = state.sessionIndex.get(state.sessionId);
    const rec = session ? primaryRecording(session) : null;
    if (rec) doWindowPause(rec.project_id, state.sessionId);
  });
  $("window-resume").addEventListener("click", () => {
    const session = state.sessionIndex.get(state.sessionId);
    const rec = session ? primaryRecording(session) : null;
    if (rec) doWindowResume(rec.project_id, state.sessionId);
  });
  $("filter-clear").addEventListener("click", () => { state.segmentFilter = null; persistSelection(); state.lastListSignature = ""; renderActivities(); pollLists(); });
  $("more-checkpoints").addEventListener("click", loadMoreCheckpoints);
  $("recorded-changes-more").addEventListener("click", loadMoreRecordedChanges);
  $("more-activities").addEventListener("click", loadMoreActivities);
  $("pause").addEventListener("click", doPause);
  $("resume").addEventListener("click", doResume);
  $("cancel-save").addEventListener("click", doCancelSave);
  $("retry-capture").addEventListener("click", doRetryCapture);
  $("changes-run").addEventListener("click", () => {
    state.playbackDiffDeferred.delete(state.selectedCheckpoint);
    loadChanges(state.selectedCheckpoint);
  });
  $("milestone-form").addEventListener("submit", doMilestone);
  $("storage-form").addEventListener("submit", doRelocate);
  $("storage-browse").addEventListener("click", doBrowseStorage);
  if (/(?:^|[#&])storage=1/.test(location.hash || "")) {
    history.replaceState(null, "", location.pathname + (location.hash || "").replace(/[#&]storage=1/, ""));
    setTimeout(() => { const el = $("storage-path"); el.scrollIntoView({ block: "center" }); el.focus(); }, 300);
  }
  $("download").addEventListener("click", doDownload);
  $("note-form").addEventListener("submit", doAnnotate);
  document.querySelectorAll("[data-lang]").forEach((b) => b.addEventListener("click", () => setLanguage(b.dataset.lang)));
  document.querySelectorAll("[data-lang]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lang === i18n.language())));
  // Pasting a fresh sign-in link into a tab that already shows the page only changes the
  // fragment (no reload), so handle it here instead of leaving the user on the login screen.
  // A plain hash change (back/forward, a pasted #doc=/#session=/#docs link) re-routes too.
  window.addEventListener("hashchange", async () => {
    const match = /(?:^|[#&])bootstrap=([^&]+)/.exec(location.hash || "");
    if (match) {
      history.replaceState(null, "", location.pathname + location.search);
      try {
        await api.bootstrap(decodeURIComponent(match[1]));
        state.signedIn = true;
        showApp();
        await refreshProjects();
        applyRouteFromHash();
        if (!statusTimer) startPolling();
      } catch (err) { showLogin(err); }
      return;
    }
    if (state.signedIn) applyRouteFromHash();
  });
  $("open-klayout").addEventListener("click", doOpenInKlayout);
  $("step-older").addEventListener("click", () => { stopPlayback(); stepPreview(1); });
  $("step-newer").addEventListener("click", () => { stopPlayback(); stepPreview(-1); });
  $("play").addEventListener("click", togglePlayback);
  $("fit").addEventListener("click", fitPreview);
  $("preview-retry").addEventListener("click", retryPreview);
}

boot();

setupHistoryImport({ api, t, getDocument: () => state.documentId,
  getReadOnly: () => Boolean(state.documentIndex.get(state.documentId)?.doc.read_only), changed: async (did) => {
  if (state.documentId !== did) return;
  state.listScopeKey = null; state.lastListSignature = ""; state.detail = null;
  state.recordedChanges.clear(); state.diffs.clear();
  await pollLists();
}});

setupSkills({api,t,getDocument:()=>state.documentId,getSelected:()=>state.selectedCheckpoint,
  getProjects:()=>state.projects,getProject:()=>state.docProjectId});
