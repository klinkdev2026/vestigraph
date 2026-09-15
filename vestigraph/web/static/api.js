// Same-origin API client. No tokens in storage: the bootstrap token lives only
// in the URL fragment for one request, then the HttpOnly cookie carries the session.
const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(status, body, requestId) {
    const error = (body && body.error) || {};
    super(error.message || `HTTP ${status}`);
    this.status = status;
    this.code = error.code || (status === 0 ? "NETWORK" : "HTTP_ERROR");
    this.messageKey = error.message_key || null;
    this.messageArgs = error.message_args || {};
    this.nextAction = error.next_action || "";
    this.nextActionKey = error.next_action_key || null;
    this.retryable = Boolean(error.retryable);
    this.details = error.details;
    this.requestId = (body && body.request_id) || requestId || null;
  }
}

export class Api {
  constructor() {
    this.csrf = null;
    this.capabilities = {};
  }

  async request(method, path, { body, requestId, signal } = {}) {
    const headers = { "Accept": "application/json" };
    if (method !== "GET") {
      headers["X-CSRF-Token"] = this.csrf || "";
      headers["X-Request-ID"] = requestId || crypto.randomUUID();
      if (body !== undefined) headers["Content-Type"] = "application/json";
    }
    let response;
    try {
      response = await fetch(BASE + path, {
        method, headers, signal, credentials: "same-origin",
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (err) {
      if (err && err.name === "AbortError") throw err;
      throw new ApiError(0, { error: { code: "NETWORK", message: String(err) } });
    }
    let json = null;
    try { json = await response.json(); } catch (_) { json = null; }
    if (!response.ok || !json || json.ok !== true) {
      throw new ApiError(response.status, json, response.headers.get("X-Request-ID"));
    }
    return json.data;
  }

  get(path, opts) { return this.request("GET", path, opts); }
  post(path, body, opts = {}) { return this.request("POST", path, { ...opts, body }); }
  put(path, body, opts = {}) { return this.request("PUT", path, { ...opts, body }); }

  // ---- auth ---------------------------------------------------------------
  async bootstrap(token) {
    const response = await fetch(BASE + "/auth/bootstrap", {
      method: "POST", credentials: "same-origin",
      headers: { "Authorization": "Bearer " + token, "Accept": "application/json" },
    });
    const json = await response.json().catch(() => null);
    if (!response.ok || !json || !json.ok) throw new ApiError(response.status, json);
    this.csrf = json.data.csrf_token;
    this.capabilities = json.data.capabilities || {};
    return json.data;
  }

  async session() {
    const data = await this.get("/auth/session");
    this.csrf = data.csrf_token;
    this.capabilities = data.capabilities || {};
    return data;
  }

  logout() { return this.post("/auth/logout"); }

  // ---- reads --------------------------------------------------------------
  status(signal) { return this.get("/status", { signal }); }
  sessions(signal) { return this.get("/sessions", { signal }); }
  projects(signal) { return this.get("/projects", { signal }); }
  documents(pid, signal) { return this.get(`/projects/${enc(pid)}/documents`, { signal }); }
  projectStatus(pid, signal) { return this.get(`/projects/${enc(pid)}/status`, { signal }); }
  checkpoints(did, { cursor, limit, segmentId } = {}, signal) {
    return this.get(`/documents/${enc(did)}/checkpoints${query({ cursor, limit, segment_id: segmentId })}`, { signal });
  }
  segments(did, { cursor, limit } = {}, signal) {
    return this.get(`/documents/${enc(did)}/segments${query({ cursor, limit })}`, { signal });
  }
  events(did, { cursor, limit, segmentId } = {}, signal) {
    return this.get(`/documents/${enc(did)}/events${query({ cursor, limit, segment_id: segmentId })}`, { signal });
  }
  checkpoint(did, cid, signal) { return this.get(`/documents/${enc(did)}/checkpoints/${enc(cid)}`, { signal }); }
  thumbnailUrl(did, cid) { return `${BASE}/documents/${enc(did)}/checkpoints/${enc(cid)}/thumbnail`; }
  thumbnailStatus(did, cid, signal) {
    return this.get(`/documents/${enc(did)}/checkpoints/${enc(cid)}/thumbnail-status`, { signal });
  }
  renameCheckpoint(did, cid, title, expectedRevision, requestId) {
    return this.put(`/documents/${enc(did)}/checkpoints/${enc(cid)}/title`,
      { title, expected_revision: expectedRevision, actor: "user" }, { requestId });
  }
  changes(did, cid, { cursor, limit, kind } = {}, signal) {
    return this.get(`/documents/${enc(did)}/checkpoints/${enc(cid)}/changes${query({ cursor, limit, kind })}`, { signal });
  }
  annotations(did, targetType, targetId, signal) {
    return this.get(`/documents/${enc(did)}/annotations${query({ target_type: targetType, target_id: targetId })}`, { signal });
  }
  job(jid, signal) { return this.get(`/jobs/${enc(jid)}`, { signal }); }
  assetUrl(jid) { return `${BASE}/jobs/${enc(jid)}/asset`; }

  // ---- writes (each user action gets one request id, reused on retry) ------
  setPolicy(pid, updates, expectedVersion) {
    return this.put(`/projects/${enc(pid)}/policy`, { ...updates, expected_policy_version: expectedVersion });
  }
  browseStorage(pid, title) { return this.post(`/projects/${enc(pid)}/storage/browse`, { title }); }
  relocate(pid, historyRoot, requestId) { return this.put(`/projects/${enc(pid)}/storage`, { history_root: historyRoot }, { requestId }); }
  pause(pid, reason, requestId, sessionId) {
    const body = { reason };
    if (sessionId) body.session_id = sessionId;
    return this.post(`/projects/${enc(pid)}/pause`, body, { requestId });
  }
  resume(pid, requestId, sessionId) {
    const body = sessionId ? { session_id: sessionId } : undefined;
    return this.post(`/projects/${enc(pid)}/resume`, body, { requestId });
  }
  milestone(did, title, requestId) { return this.post(`/documents/${enc(did)}/milestones`, { title }, { requestId }); }
  cancelSave(did, requestId) { return this.post(`/documents/${enc(did)}/save/cancel`, undefined, { requestId }); }
  retryCapture(did, requestId) { return this.post(`/documents/${enc(did)}/capture/retry`, undefined, { requestId }); }
  annotate(did, targetType, targetId, text, requestId) {
    return this.post(`/documents/${enc(did)}/annotations`, { target_type: targetType, target_id: targetId, text }, { requestId });
  }
  download(did, cid, requestId) { return this.post(`/documents/${enc(did)}/checkpoints/${enc(cid)}/downloads`, undefined, { requestId }); }
  diff(did, fromId, toId, requestId) { return this.post(`/documents/${enc(did)}/diffs`, { from: fromId, to: toId }, { requestId }); }
  preview(did, cid, body, requestId) { return this.post(`/documents/${enc(did)}/checkpoints/${enc(cid)}/preview`, body || {}, { requestId }); }
  openInKlayout(did, cid, body, requestId) { return this.post(`/documents/${enc(did)}/checkpoints/${enc(cid)}/open-in-klayout`, body, { requestId }); }
  openInEditor(did, cid, body, requestId) { return this.post(`/documents/${enc(did)}/checkpoints/${enc(cid)}/open-in-editor`, body, { requestId }); }
}

function enc(value) { return encodeURIComponent(String(value)); }

function query(params) {
  const parts = [];
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") parts.push(`${enc(key)}=${enc(value)}`);
  }
  return parts.length ? "?" + parts.join("&") : "";
}
