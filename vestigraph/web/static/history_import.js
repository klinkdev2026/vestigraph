// Local native selection grants file access; HTTP confirmation carries only plan item IDs.
export function setupHistoryImport({ api, getDocument, getReadOnly, changed, t }) {
  const $ = (id) => document.getElementById(id);
  const dialog = $("history-import-dialog");
  let did = null, plan = null, busy = false, timer = null, anchorCursor = null, generation = 0;
  let blockedReason = "", initialized = false;
  const endpoint = (suffix = "") => `/documents/${encodeURIComponent(did)}/history-imports${suffix}`;
  const message = (text) => { $("history-import-status").textContent = text; };
  const fail = (error) => { message(error.message || String(error)); busy = false; render(); };
  const action = (name, fn) => {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = t(name); b.addEventListener("click", fn);
    return b;
  };
  function render() {
    $("history-import-pick").disabled = busy || !initialized || Boolean(blockedReason);
    $("history-import-run").disabled = busy || Boolean(blockedReason) || !plan || !plan.items.length ||
      plan.items.some((i) => !i.sha256) || plan.status === "completed";
    $("history-import-run").textContent = t(plan && plan.status !== "draft" ? "import.resume" : "import.run");
    $("history-import-cancel").disabled = !plan || !busy;
    $("history-import-before").disabled = busy || !plan || plan.status !== "draft";
    const list = $("history-import-items");
    list.replaceChildren();
    if (!plan) return;
    const editable = plan.status === "draft" && !busy;
    plan.items.forEach((item, index) => {
      const li = document.createElement("li"); li.className = "import-item";
      const name = document.createElement("p");
      name.textContent = item.name;
      const detail = document.createElement("p"); detail.className = "muted";
      detail.textContent = item.error || (item.checkpoint_id ? t("import.item_done") :
        item.duplicate_of ? t("import.duplicate") : t("import.item_waiting"));
      li.append(name, detail);
      for (const [key, label, type] of [["title", "import.name", "text"], ["note", "import.note", "text"], ["historical_at", "import.date", "date"]]) {
        const field = document.createElement("label");
        field.append(document.createTextNode(t(label)));
        const input = document.createElement("input"); input.type = type;
        input.value = item[key] || ""; input.disabled = !editable;
        input.maxLength = key === "note" ? 2000 : key === "title" ? 200 : 64;
        input.dataset.field = key;
        input.addEventListener("input", () => { item[key] = input.value; });
        field.append(input); li.append(field);
      }
      const buttons = document.createElement("div"); buttons.className = "actions";
      const up = action("import.up", () => {
        [plan.items[index-1], plan.items[index]] = [plan.items[index], plan.items[index-1]]; render();
      });
      const down = action("import.down", () => {
        [plan.items[index+1], plan.items[index]] = [plan.items[index], plan.items[index+1]]; render();
      });
      const remove = action("import.remove", () => { plan.items.splice(index, 1); render(); });
      up.disabled = !editable || index === 0;
      down.disabled = !editable || index === plan.items.length-1;
      remove.disabled = !editable;
      buttons.append(up, down, remove); li.append(buttons); list.append(li);
    });
  }
  async function plans() {
    const token = generation;
    const result = await api.get(endpoint());
    if (token !== generation) return;
    const list = $("history-import-plans");
    list.replaceChildren();
    for (const item of result.items) {
      const row = document.createElement("div"); row.className = "actions";
      const open = action("import.open_plan", () => load(item.id).catch(fail));
      const text = document.createElement("span");
      text.textContent = `${item.created_at.slice(0, 10)} \u00b7 ${t("import.state_" + item.status)} \u00b7 ${item.completed}/${item.total}`;
      const remove = action("import.remove_plan", async () => {
        try {
          await api.request("DELETE", endpoint("/" + item.id));
          if (plan && plan.id === item.id) { plan = null; busy = false; render(); }
          await plans();
        } catch (error) { fail(error); }
      });
      remove.disabled = ["ready", "running"].includes(item.status);
      open.disabled = busy;
      row.append(text, open, remove); list.append(row);
    }
  }
  async function anchors(cursor = null) {
    const token = generation;
    const page = await api.checkpoints(did, { limit: 100, cursor });
    if (token !== generation) return;
    const select = $("history-import-before");
    for (const cp of page.items) {
      if (![...select.options].some((o) => o.value === cp.id)) {
        const option = document.createElement("option"); option.value = cp.id;
        option.textContent = cp.title || cp.filename; select.append(option);
      }
    }
    anchorCursor = page.next_cursor;
    $("history-import-more").hidden = !anchorCursor;
    if (plan) select.value = plan.before_id;
  }
  async function load(id) {
    const token = generation;
    if (timer) clearTimeout(timer);
    const loaded = await api.get(endpoint("/" + id));
    if (token !== generation) return;
    plan = loaded;
    const select = $("history-import-before");
    if (![...select.options].some((o) => o.value === plan.before_id)) {
      const cp = await api.checkpoint(did, plan.before_id);
      if (token !== generation) return;
      const option = document.createElement("option"); option.value = cp.id;
      option.textContent = cp.title || cp.filename; select.append(option);
    }
    select.value = plan.before_id;
    busy = ["ready", "running"].includes(plan.status);
    message(t("import.state_" + plan.status) +
      ` \u00b7 ${plan.items.filter((i) => i.checkpoint_id).length}/${plan.items.length}` +
      (plan.error ? ` \u00b7 ${plan.error}` : ""));
    render();
    if (busy && plan.active_job_id) watch(plan.active_job_id, false);
  }
  async function watch(jobId, preparing) {
    if (timer) clearTimeout(timer);
    const capturedDid = did, token = generation;
    const tick = async () => {
      if (!dialog.open || token !== generation || did !== capturedDid) return;
      try {
        const job = await api.job(jobId);
        if (token !== generation) return;
        if (["queued", "running"].includes(job.status)) {
          message(t(preparing ? "import.preparing" : "import.running") + ` \u00b7 ${Math.round((job.progress || 0)*100)}%`);
          if (!preparing && plan) {
            const updated = await api.get(endpoint("/" + plan.id));
            if (token !== generation) return;
            plan = updated; render();
          }
          timer = setTimeout(tick, 800);
          return;
        }
        busy = false;
        if (job.status !== "succeeded") {
          if (plan) await load(plan.id);
          throw new Error(job.error?.message || t("import.interrupted"));
        }
        if (preparing) await load(job.result.plan_id);
        else { await load(plan.id); await changed(capturedDid); }
        await plans();
      } catch (error) { fail(error); }
    };
    await tick();
  }
  $("history-import-open").addEventListener("click", async () => {
    if (busy && dialog.open) return;
    generation++;
    did = getDocument(); if (!did) return;
    plan = null; busy = false; initialized = false;
    blockedReason = !api.capabilities.legacy_import ? "import.service_outdated" : getReadOnly() ? "import.read_only" : "";
    $("history-import-plans").replaceChildren();
    $("history-import-before").replaceChildren();
    message(t(blockedReason || "import.hint")); render(); dialog.showModal();
    if (blockedReason) return;
    const token = generation;
    try {
      await anchors();
      if (token !== generation) return;
      if (!$("history-import-before").options.length) {
        blockedReason = "import.no_current_version";
        message(t(blockedReason));
      }
      initialized = true; render();
      await plans();
    } catch (error) { if (token === generation) fail(error); }
  });
  $("history-import-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { generation++; if (timer) clearTimeout(timer); timer = null; });
  $("history-import-before").addEventListener("change", () => { if (plan) plan.before_id = $("history-import-before").value; });
  $("history-import-more").addEventListener("click", () => anchors(anchorCursor).catch(fail));
  $("history-import-pick").addEventListener("click", async () => {
    if (busy) return;
    const token = generation;
    busy = true; render(); message(t("import.selecting"));
    try {
      const result = await api.post(endpoint("/browse"), { title: t("import.picker_title") });
      if (token !== generation) return;
      if (result.cancelled) { busy = false; message(t("import.hint")); render(); return; }
      plan = null; render(); await watch(result.job_id, true);
    } catch (error) { fail(error); }
  });
  $("history-import-run").addEventListener("click", async () => {
    if (!plan || busy) return;
    const token = generation, planId = plan.id;
    busy = true; render();
    try {
      const result = await api.post(endpoint("/" + plan.id + "/run"), {
        before_id: plan.before_id,
        items: plan.status === "draft" ? plan.items.map(({ id, title, note, historical_at }) => ({ id, title, note, historical_at })) : null,
      });
      if (token !== generation) return;
      await watch(result.job_id, false);
    } catch (error) {
      if (token !== generation) return;
      try { await load(planId); } catch (_) {}
      fail(error);
    }
  });
  $("history-import-cancel").addEventListener("click", async () => {
    if (!plan) return;
    try { await api.post(endpoint("/" + plan.id + "/cancel")); message(t("import.cancelling")); }
    catch (error) { fail(error); }
  });
}
