import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_NAME = "CustomNodeInstaller";
const LS_KEY = "mli.token.v1";

const SRC_LABEL = {
  "manager-db": "ComfyUI-Manager database",
  registry: "Comfy Registry",
  hint: "workflow metadata (unverified)",
  user: "added by you",
};
const TRUSTED = new Set(["manager-db", "registry"]);

const CSS = `
.mli-root{display:flex;flex-direction:column;gap:6px;height:100%;box-sizing:border-box;padding:4px;font-size:12px;color:var(--input-text,#ddd);}
.mli-root *{box-sizing:border-box;}
.mli-root input[type=text],.mli-root input[type=password]{background:var(--comfy-input-bg,#222);color:var(--input-text,#ddd);border:1px solid var(--border-color,#4e4e4e);border-radius:4px;padding:4px 6px;font-size:12px;min-width:0;}
.mli-top{display:flex;gap:10px;align-items:center;flex-wrap:wrap;}
.mli-top input[type=password]{flex:1;min-width:160px;}
.mli-top label{display:flex;gap:4px;align-items:center;white-space:nowrap;opacity:.85;}
.mli-tools{display:flex;gap:4px;flex-wrap:wrap;align-items:center;}
.mli-btn{background:var(--comfy-menu-bg,#353535);color:var(--input-text,#ddd);border:1px solid var(--border-color,#4e4e4e);border-radius:4px;padding:4px 8px;cursor:pointer;font-size:12px;}
.mli-btn:hover{filter:brightness(1.3);}
.mli-go{background:#2e7d4f;border-color:#2e7d4f;color:#fff;font-weight:600;}
.mli-stop{background:#8a3333;border-color:#8a3333;color:#fff;}
.mli-hot{background:#b9770e;border-color:#b9770e;color:#fff;font-weight:600;animation:mli-pulse 1.4s infinite;}
@keyframes mli-pulse{50%{filter:brightness(1.35);}}
.mli-filter{flex:1;min-width:90px;}
.mli-note{min-height:16px;opacity:.9;word-break:break-word;}
.mli-note.err{color:#ff7777;}.mli-note.ok{color:#7fe3a0;}.mli-note.warn{color:#ffd37a;}
.mli-list{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:6px;padding-right:2px;}
.mli-row{border:1px solid var(--border-color,#4e4e4e);border-radius:6px;padding:6px;display:flex;flex-direction:column;gap:3px;background:rgba(255,255,255,.03);}
.mli-row.off{opacity:.55;}
.mli-line{display:flex;gap:6px;align-items:center;}
.mli-line>input.mli-url{flex:1;}
.mli-ref{width:130px;flex:none;}
.mli-x{background:none;border:none;color:#ff8888;cursor:pointer;font-size:14px;padding:0 4px;}
.mli-switch{position:relative;width:34px;height:18px;flex:none;}
.mli-switch input{opacity:0;width:0;height:0;position:absolute;}
.mli-slider{position:absolute;inset:0;background:#666;border-radius:18px;cursor:pointer;transition:.15s;}
.mli-slider:before{content:"";position:absolute;width:14px;height:14px;left:2px;top:2px;background:#fff;border-radius:50%;transition:.15s;}
.mli-switch input:checked+.mli-slider{background:#2e9e5b;}
.mli-switch input:checked+.mli-slider:before{transform:translateX(16px);}
.mli-info{font-size:11px;opacity:.6;word-break:break-word;}
.mli-status{font-size:11px;min-height:14px;word-break:break-word;}
.mli-status.ok{color:#7fe3a0;}.mli-status.err{color:#ff7777;}.mli-status.warn{color:#ffd37a;}.mli-status.dim{opacity:.7;}
`;

function injectCss() {
  if (document.getElementById("mli-css")) return;
  const s = document.createElement("style");
  s.id = "mli-css";
  s.textContent = CSS;
  document.head.appendChild(s);
}

function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null) continue;
    if (k === "class") e.className = v;
    else if (k === "style") e.style.cssText = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e[k] = v;
  }
  for (const kid of kids.flat()) if (kid) e.append(kid);
  return e;
}

const uid = () => Math.random().toString(36).slice(2, 10);
const normUrl = (u) =>
  (u || "").trim().toLowerCase()
    .replace(/^(?:ssh:\/\/)?git@([^:/]+)[:/]/, "$1/")
    .replace(/^[a-z]+:\/\//, "").replace(/\.git$/, "").replace(/\/+$/, "");

function hideWidget(w) {
  w.computeSize = () => [0, -4];
  w.type = "converted-widget";
  w.hidden = true;
  if (w.element) w.element.style.display = "none";
}

function collectMissing() {
  const reg = window.LiteGraph?.registered_node_types || {};
  const out = new Map();
  const visit = (graph) => {
    for (const n of graph?._nodes || graph?.nodes || []) {
      const t = n.type;
      if (t && !(t in reg) && !/^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(t)) {
        const e = out.get(t) || { type: t, cnr_id: null, aux_id: null };
        const p = n.properties || {};
        e.cnr_id = e.cnr_id || p.cnr_id || null;
        e.aux_id = e.aux_id || p.aux_id || null;
        out.set(t, e);
      }
      if (n.subgraph) visit(n.subgraph);
    }
  };
  visit(app.graph);
  return [...out.values()];
}

function setup(node) {
  injectCss();
  const stateW = node.widgets?.find((w) => w.name === "state");
  if (!stateW) return;
  hideWidget(stateW);

  const S = {
    entries: [], gh: "", saveToken: false,
    opts: { requirements: true, install_py: true, protect_torch: true, autoscan: true, autoinstall: false },
  };
  const rows = new Map();
  const live = new Map();
  let jobId = null, jobMode = "install", timer = null, checkTimer = null, scanning = false, lastScan = 0, restartNeeded = false;

  const newEntry = (p = {}) => ({
    id: p.id || uid(), on: p.on !== false, url: p.url || "", ref: p.ref || "",
    src: p.src || "user", title: p.title || "", covers: p.covers || [],
  });
  const byId = (id) => S.entries.find((e) => e.id === id);
  const isBlankOnly = () => S.entries.length === 1 && !S.entries[0].url;

  // ------------------------------------------------------------ persistence
  function persist() {
    const out = { v: 1, entries: S.entries, opts: S.opts, save_token: S.saveToken };
    if (S.saveToken) out.gh_token = S.gh;
    stateW.value = JSON.stringify(out);
    try { localStorage.setItem(LS_KEY, S.gh); } catch {}
    try { node.graph?.change?.(); } catch {}
  }

  let loaded = false;
  function load() {
    let d = {};
    try { d = JSON.parse(stateW.value || "{}"); } catch {}
    let saved = "";
    try { saved = localStorage.getItem(LS_KEY) || ""; } catch {}
    S.entries = Array.isArray(d.entries) ? d.entries.map(newEntry) : [];
    S.opts = { ...S.opts, ...(d.opts || {}) };
    S.saveToken = !!d.save_token;
    S.gh = d.gh_token || saved || "";
    if (!S.entries.length) S.entries = [newEntry()];
    ghIn.value = S.gh; saveChk.checked = S.saveToken;
    for (const [k, c] of Object.entries(optBoxes)) c.checked = !!S.opts[k];
    renderAll();
    scheduleCheck();
    loaded = true;
  }

  // ------------------------------------------------------------ top + tools
  const ghIn = h("input", { type: "password", placeholder: "GitHub token (optional – only for private repos)", autocomplete: "new-password",
    oninput: () => { S.gh = ghIn.value.trim(); persist(); } });
  const saveChk = h("input", { type: "checkbox", onchange: () => { S.saveToken = saveChk.checked; persist(); } });
  const optBoxes = {};
  const opt = (key, label, title) => {
    const c = h("input", { type: "checkbox", onchange: () => { S.opts[key] = c.checked; persist(); } });
    optBoxes[key] = c;
    return h("label", { title }, c, label);
  };
  const top = h("div", { class: "mli-top" }, ghIn,
    h("label", { title: "Token is normally remembered in this browser only. Tick to store it INSIDE the workflow (never share such a workflow)." }, saveChk, "save in workflow ⚠"));
  const optsBar = h("div", { class: "mli-top" },
    opt("requirements", "pip requirements.txt", "Run pip install -r requirements.txt after cloning"),
    opt("install_py", "run install.py", "Run the repo's install.py if it has one"),
    opt("protect_torch", "protect torch", "Skip torch / torchvision / torchaudio / xformers lines so a node can't break your CUDA setup"),
    opt("autoscan", "auto-scan on load", "When a workflow with this node loads, look up the missing nodes and add them to the list"),
    opt("autoinstall", "auto-install ⚡", "After an auto-scan, install repos found in the ComfyUI-Manager DB / Comfy Registry right away. Repos named only by workflow metadata are never auto-installed."));

  const note = h("div", { class: "mli-note" });
  const say = (t, cls = "") => { note.textContent = t; note.className = "mli-note " + cls; };

  const btn = (label, title, fn, cls = "") => h("button", { class: "mli-btn " + cls, title, textContent: label, onclick: fn });
  const filter = h("input", { type: "text", class: "mli-filter", placeholder: "🔍 filter…", oninput: applyFilter });
  const goBtn = btn("⬇ Install selected", "Install every row that is switched ON (already installed ones are skipped)", () => startJob("install"), "mli-go");
  const stopBtn = btn("⏹ Stop", "Stop (a half-cloned folder is removed)", () => stopJob(), "mli-stop");
  stopBtn.style.display = "none";
  const restartBtn = btn("🔄 Restart ComfyUI", "Restart the server so new nodes load, then reload this page", restart);

  const tools = h("div", { class: "mli-tools" },
    btn("➕ Add", "Add an empty row", () => addEntry({}, true)),
    btn("📋 Paste many", "Paste several git links at once", pasteMany),
    btn("🔎 Scan workflow", "Find missing nodes in the current workflow and add the repos that provide them", () => scan(false)),
    btn("📥 Add installed", "Add every git-installed custom node of THIS machine (build your list in one click)", addInstalled),
    btn("All ON", "", () => setAll(true)), btn("All OFF", "", () => setAll(false)),
    btn("⟳ Update", "git pull the ON rows that are already installed", () => startJob("update")),
    btn("⇪ Export", "Copy list as JSON", exportList), btn("⇩ Import", "Paste a JSON list or a ComfyUI-Manager snapshot", importList),
    filter, restartBtn, stopBtn, goBtn);

  const list = h("div", { class: "mli-list" });
  list.addEventListener("wheel", (e) => { if (list.scrollHeight > list.clientHeight) e.stopPropagation(); }, { passive: true });
  const root = h("div", { class: "mli-root" }, top, optsBar, tools, note, list);

  // ------------------------------------------------------------ rows
  function renderAll() {
    list.textContent = "";
    rows.clear();
    for (const e of S.entries) list.append(renderRow(e));
    applyFilter();
  }

  function infoText(e) {
    const bits = [];
    if (e.title) bits.push(e.title);
    if (e.covers?.length) bits.push("provides: " + e.covers.slice(0, 4).join(", ") + (e.covers.length > 4 ? ` +${e.covers.length - 4}` : ""));
    if (e.src && e.src !== "user") bits.push("source: " + (SRC_LABEL[e.src] || e.src));
    return bits.join("  ·  ");
  }

  function renderRow(e) {
    const cb = h("input", { type: "checkbox", checked: e.on, onchange: () => {
      e.on = cb.checked; row.classList.toggle("off", !e.on); persist(); } });
    const sw = h("label", { class: "mli-switch", title: "ON = install this one" }, cb, h("span", { class: "mli-slider" }));
    const url = h("input", { type: "text", class: "mli-url", value: e.url,
      placeholder: "https://github.com/user/repo(.git)   ·   git@…   ·   user/repo",
      oninput: () => { e.url = url.value.trim(); e.src = e.src === "user" ? "user" : e.src; persist(); scheduleCheck(); },
      onpaste: () => setTimeout(() => { url.value = url.value.trim(); e.url = url.value; persist(); scheduleCheck(); }, 0) });
    const ref = h("input", { type: "text", class: "mli-ref", value: e.ref, placeholder: "branch / tag / commit",
      title: "Optional: branch, tag or commit hash to check out",
      oninput: () => { e.ref = ref.value.trim(); persist(); } });
    const del = h("button", { class: "mli-x", title: "Remove row", textContent: "✕", onclick: () => removeEntry(e.id) });
    const info = h("div", { class: "mli-info", textContent: infoText(e) });
    const status = h("div", { class: "mli-status" });
    const row = h("div", { class: "mli-row" + (e.on ? "" : " off") },
      h("div", { class: "mli-line" }, sw, url, ref, del), info, status);
    rows.set(e.id, { root: row, status, info });
    if (live.has(e.id)) paint(e.id, live.get(e.id));
    return row;
  }

  function addEntry(p, focus) {
    const e = newEntry(p);
    S.entries.push(e);
    const el = renderRow(e);
    list.append(el);
    persist();
    if (focus) { el.querySelector("input.mli-url")?.focus(); list.scrollTop = list.scrollHeight; }
    return e;
  }

  function removeEntry(id) {
    S.entries = S.entries.filter((e) => e.id !== id);
    rows.get(id)?.root.remove(); rows.delete(id); live.delete(id);
    if (!S.entries.length) addEntry({}, false);
    persist();
  }

  function setAll(on) {
    for (const e of S.entries) {
      const r = rows.get(e.id);
      if (!r || r.root.style.display === "none") continue;
      e.on = on;
      r.root.querySelector("input[type=checkbox]").checked = on;
      r.root.classList.toggle("off", !on);
    }
    persist();
  }

  function applyFilter() {
    const q = filter.value.trim().toLowerCase();
    for (const e of S.entries) {
      const r = rows.get(e.id);
      if (r) r.root.style.display = !q || (e.url + " " + e.title + " " + (e.covers || []).join(" ")).toLowerCase().includes(q) ? "" : "none";
    }
  }

  function addUrls(urls, extra = {}) {
    const have = new Set(S.entries.map((e) => normUrl(e.url)).filter(Boolean));
    const fresh = [];
    for (const u of urls) {
      const url = (typeof u === "string" ? u : u.url).trim();
      if (!url || have.has(normUrl(url))) continue;
      have.add(normUrl(url));
      fresh.push(newEntry({ ...extra, ...(typeof u === "string" ? {} : u), url }));
    }
    if (!fresh.length) return 0;
    if (isBlankOnly()) S.entries = [];
    S.entries.push(...fresh);
    persist(); renderAll(); scheduleCheck();
    return fresh.length;
  }

  // ------------------------------------------------------------ painting
  function paint(id, st) {
    live.set(id, st);
    const r = rows.get(id);
    if (!r) return;
    const m = st.msg ? `  ·  ${st.msg}` : "";
    let text = "", cls = "dim";
    switch (st.state) {
      case "queued": text = "⏳ queued"; break;
      case "cloning": text = `⬇ ${st.msg || "cloning…"}`; cls = ""; break;
      case "installing": text = `📦 ${st.msg || "installing dependencies…"}`; cls = ""; break;
      case "done": text = `✅ installed${m}`; cls = "ok"; break;
      case "updated": text = `✅ updated${m}`; cls = "ok"; break;
      case "uptodate": text = "✔ already up to date"; cls = "ok"; break;
      case "warning": text = `⚠ installed, but ${st.msg}`; cls = "warn"; break;
      case "skipped": text = `⏭ skipped – ${st.msg}`; cls = "warn"; break;
      case "installed": text = `✔ already installed${st.dir ? ` (${st.dir})` : ""}`; cls = "ok"; break;
      case "disabled": text = `⏸ installed but disabled in ComfyUI-Manager (${st.dir})`; cls = "warn"; break;
      case "invalid": text = `❌ ${st.msg}`; cls = "err"; break;
      case "error": text = `❌ ${st.msg}`; cls = "err"; break;
      case "cancelled": text = "⏹ cancelled"; cls = "warn"; break;
    }
    r.status.textContent = text;
    r.status.className = "mli-status " + cls;
  }

  function scheduleCheck() { clearTimeout(checkTimer); checkTimer = setTimeout(runCheck, 600); }
  async function runCheck() {
    const items = S.entries.filter((e) => e.url.trim()).map(({ id, url }) => ({ id, url }));
    if (!items.length) return;
    try {
      const r = await api.fetchApi("/mli/check", { method: "POST", body: JSON.stringify({ entries: items }) });
      const j = await r.json();
      for (const e of items) {
        const c = j.result?.[e.id];
        const cur = live.get(e.id);
        if (cur && ["queued", "cloning", "installing"].includes(cur.state)) continue;
        if (c?.invalid) paint(e.id, { state: "invalid", msg: c.invalid });
        else if (c?.installed) paint(e.id, { state: c.disabled ? "disabled" : "installed", dir: c.dir });
        else if (cur && ["installed", "disabled", "invalid", "skipped"].includes(cur.state)) paint(e.id, { state: "idle" });
      }
    } catch {}
  }

  // ------------------------------------------------------------ jobs
  async function startJob(mode, onlyIds = null) {
    if (jobId) return;
    const sel = S.entries.filter((e) => e.url.trim() && (onlyIds ? onlyIds.includes(e.id) : e.on));
    if (!sel.length) { say("Nothing selected – switch some rows ON and fill in a link", "err"); return; }
    try {
      const r = await api.fetchApi("/mli/start", { method: "POST", body: JSON.stringify({
        mode, entries: sel.map(({ id, url, ref }) => ({ id, url, ref })),
        opts: { requirements: S.opts.requirements, install_py: S.opts.install_py, protect_torch: S.opts.protect_torch },
        tokens: { github: S.gh } }) });
      const j = await r.json();
      if (!r.ok) { say(j.error || "Failed to start", "err"); return; }
      jobId = j.job; jobMode = mode;
      for (const e of sel) paint(e.id, { state: "queued" });
      goBtn.style.display = "none"; stopBtn.style.display = "";
      say(`${mode === "update" ? "Updating" : "Installing"} ${sel.length} node pack(s)…`);
      timer = setInterval(poll, 900);
    } catch (err) { say("Couldn't start: " + err, "err"); }
  }

  async function poll() {
    if (!jobId) return;
    try {
      const r = await api.fetchApi(`/mli/status?job=${jobId}`);
      if (!r.ok) throw new Error("job lost");
      const j = await r.json();
      for (const [id, st] of Object.entries(j.items)) paint(id, st);
      if (j.state !== "running") {
        clearInterval(timer); timer = null; jobId = null;
        goBtn.style.display = ""; stopBtn.style.display = "none";
        const c = {};
        for (const st of Object.values(j.items)) c[st.state] = (c[st.state] || 0) + 1;
        const ok = (c.done || 0) + (c.updated || 0), warn = c.warning || 0, bad = c.error || 0;
        say(`Finished – ${ok} ${jobMode === "update" ? "updated" : "installed"}, ${c.skipped || 0} skipped${warn ? `, ${warn} with warnings` : ""}${bad ? `, ${bad} failed` : ""}.` +
          (j.needs_restart ? "  ⚠ Restart ComfyUI to load the new nodes." : ""), bad ? "err" : j.needs_restart ? "warn" : "ok");
        if (j.needs_restart) { restartNeeded = true; restartBtn.classList.add("mli-hot"); }
        scheduleCheck();
      }
    } catch (err) {
      clearInterval(timer); timer = null; jobId = null;
      goBtn.style.display = ""; stopBtn.style.display = "none";
      say("Lost connection to the job: " + err + " (it may have been restarted)", "err");
    }
  }

  async function stopJob() {
    if (!jobId) return;
    await api.fetchApi("/mli/cancel", { method: "POST", body: JSON.stringify({ job: jobId }) });
    say("Stopping…");
  }

  async function restart() {
    if (!confirm((restartNeeded ? "" : "Nothing seems to need a restart. ") + "Restart the ComfyUI server now?\n(Any running generation will be lost; the page reloads when the server is back.)")) return;
    try { await api.fetchApi("/mli/restart", { method: "POST", body: "{}" }); } catch {}
    say("Restarting… this page reloads automatically when ComfyUI is back.", "warn");
    setTimeout(async function ping() {
      try {
        const r = await api.fetchApi("/system_stats", { cache: "no-store" });
        if (r.ok) { location.reload(); return; }
      } catch {}
      setTimeout(ping, 2000);
    }, 5000);
  }

  // ------------------------------------------------------------ scan
  async function scan(auto) {
    if (scanning) return;
    scanning = true; lastScan = Date.now();
    try {
      const missing = collectMissing();
      if (!missing.length) { say(auto ? "" : "No missing nodes in this workflow ✔", "ok"); return; }
      say(`Looking up ${missing.length} missing node type(s)…`);
      const r = await api.fetchApi("/mli/resolve", { method: "POST", body: JSON.stringify({ items: missing }) });
      const j = await r.json();
      if (!r.ok) { say(j.error || "Lookup failed", "err"); return; }

      const have = new Set(S.entries.map((e) => normUrl(e.url)).filter(Boolean));
      const fresh = [], already = [];
      for (const repo of j.repos) {
        if (have.has(normUrl(repo.url))) { already.push(repo); continue; }
        fresh.push({ url: repo.url, src: repo.source, title: repo.title, covers: repo.covers, on: !repo.installed });
      }
      let added = 0;
      if (fresh.length) {
        added = fresh.length;
        if (isBlankOnly()) S.entries = [];
        const created = fresh.map((f) => newEntry(f));
        S.entries.push(...created);
        persist(); renderAll(); scheduleCheck();
        fresh.forEach((f, i) => { f._id = created[i].id; });
      }
      const alreadyInstalled = j.repos.filter((x) => x.installed).length;
      const parts = [`${j.repos.length} repo(s) provide the ${missing.length} missing node type(s)`];
      if (added) parts.push(`${added} added to the list`);
      if (alreadyInstalled) parts.push(`${alreadyInstalled} already installed – restart ComfyUI or check its console for import errors`);
      if (j.unresolved.length) {
        parts.push(`no repo found for ${j.unresolved.length}: ` + j.unresolved.slice(0, 4).map((u) => u.type).join(", ") + (j.unresolved.length > 4 ? "…" : ""));
        console.warn("[Custom Node Installer] unresolved:", j.unresolved);
      }
      const hasHint = j.repos.some((x) => x.source === "hint" && !x.installed);
      if (hasHint) parts.push("repos from workflow metadata are unverified – check them before installing");
      say(parts.join("  ·  "), j.unresolved.length || hasHint ? "warn" : "ok");

      if (auto && S.opts.autoinstall) {
        const ids = fresh.filter((f) => TRUSTED.has(f.src) && f.on).map((f) => f._id);
        if (ids.length) startJob("install", ids);
      }
    } catch (err) {
      say("Scan failed: " + err, "err");
    } finally {
      scanning = false;
    }
  }

  async function addInstalled() {
    try {
      const r = await api.fetchApi("/mli/installed");
      const j = await r.json();
      const n = addUrls((j.nodes || []).map((x) => ({ url: x.url, ref: "", title: x.dir, on: true })));
      say(n ? `Added ${n} git-installed node pack(s) from this machine` : "Everything installed here is already in the list", "ok");
    } catch (err) { say("Couldn't read installed nodes: " + err, "err"); }
  }

  function pasteMany() {
    const t = prompt("Paste git links (separated by spaces, commas or new lines):");
    if (!t) return;
    const n = addUrls(t.split(/[\s,]+/).filter(Boolean));
    say(n ? `Added ${n} link(s)` : "No new links found", n ? "ok" : "warn");
  }

  async function exportList() {
    const text = JSON.stringify({ entries: S.entries.filter((e) => e.url).map(({ url, ref, on }) => ({ url, ref, on })) }, null, 2);
    try { await navigator.clipboard.writeText(text); say("List copied to clipboard", "ok"); }
    catch { prompt("Copy this JSON:", text); }
  }

  function importList() {
    const t = prompt("Paste an exported list (JSON) or a ComfyUI-Manager snapshot:");
    if (!t) return;
    try {
      const d = JSON.parse(t);
      let items = [];
      if (Array.isArray(d)) items = d;
      else if (d.entries) items = d.entries;
      else if (d.git_custom_nodes) items = Object.entries(d.git_custom_nodes).filter(([, v]) => !v?.disabled).map(([url, v]) => ({ url, ref: v?.hash || "" }));
      const n = addUrls(items);
      say(`Imported ${n} row(s)`, "ok");
    } catch { say("That isn't valid JSON", "err"); }
  }

  // ------------------------------------------------------------ mount
  node.addDOMWidget("mli_ui", "mli", root, { serialize: false, hideOnZoom: false, getMinHeight: () => 320 });
  node.setSize([Math.max(node.size?.[0] || 0, 800), Math.max(node.size?.[1] || 0, 600)]);
  const prevRemoved = node.onRemoved;
  node.onRemoved = function () { clearInterval(timer); clearTimeout(checkTimer); prevRemoved?.apply(this, arguments); };

  node.__mli = {
    load,
    autoScan() {
      if (!loaded || !S.opts.autoscan || scanning || Date.now() - lastScan < 4000) return;
      scan(true);
    },
  };
  load();
  // node added by hand to an already loaded workflow
  setTimeout(() => node.__mli.autoScan(), 1800);
}

app.registerExtension({
  name: "mli.CustomNodeInstaller",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE_NAME) return;
    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      onCreated?.apply(this, arguments);
      setup(this);
    };
    const onConf = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      onConf?.apply(this, arguments);
      this.__mli?.load();
    };
  },
  // runs once the whole workflow is loaded -> every installer node in it scans for missing nodes
  async afterConfigureGraph() {
    setTimeout(() => {
      for (const n of app.graph?._nodes || []) if (n.type === NODE_NAME) n.__mli?.autoScan();
    }, 1200);
  },
});
