/* AProver workbench — connect → scope → live run, wired to the backend.
 *
 * No framework: a small state machine drives three screens + a settings modal.
 * Each transition calls a real endpoint (/api/clone, /api/tree, /api/run, …);
 * the live run streams Server-Sent Events from /api/run/{id}/events.
 */
(function () {
  "use strict";

  // ---- tiny helpers -------------------------------------------------
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  // Escape for both text nodes AND single/double-quoted attribute values — the
  // same esc() is interpolated into `class='…'` / `value='…'`, where a bare
  // quote would otherwise break out of the attribute.
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.add("hidden"), 4200);
  }

  // ---- run id in the URL (so a refresh can reconnect to the run) ----
  // Only the run id rides in the URL; the session stays in the HttpOnly cookie,
  // and the server gates /api/run/{id} on that cookie — so a bare run id grants
  // nothing on its own.
  const runInUrl = () => new URLSearchParams(location.search).get("run");
  function setRunInUrl(id) {
    history.replaceState(null, "", location.pathname + "?run=" + encodeURIComponent(id));
  }
  function clearRunInUrl() {
    if (runInUrl()) history.replaceState(null, "", location.pathname);
  }

  // ---- settings (localStorage) --------------------------------------
  const CFG_KEY = "aprover_llm_config";
  const PROVIDERS = {
    anthropic: { backend: "anthropic", base_url: "", model: "claude-sonnet-4-6" },
    openrouter: { backend: "openai", base_url: "https://openrouter.ai/api/v1", model: "anthropic/claude-sonnet-4.6" },
    openai: { backend: "openai", base_url: "https://api.openai.com/v1", model: "gpt-4o" },
  };
  // Per-provider page metadata. `baseUrl`: "none" hides it, "fixed" shows it
  // read-only, "editable" lets the user change it. `modelFixed` locks the model.
  const PROVIDER_META = {
    anthropic: { label: "Anthropic", desc: "Claude models · native Messages API", keyPlaceholder: "sk-ant-…", keyLink: "https://console.anthropic.com/settings/keys", baseUrl: "none", modelFixed: false },
    openrouter: { label: "OpenRouter", desc: "Hundreds of models · OpenAI-compatible", keyPlaceholder: "sk-or-…", keyLink: "https://openrouter.ai/keys", baseUrl: "editable", modelFixed: false },
    openai: { label: "OpenAI", desc: "GPT models · or any OpenAI-compatible endpoint", keyPlaceholder: "sk-…", keyLink: "https://platform.openai.com/api-keys", baseUrl: "editable", modelFixed: false },
  };
  const PROVIDER_ORDER = ["anthropic", "openrouter", "openai"];

  function loadCfg() {
    try {
      const c = JSON.parse(localStorage.getItem(CFG_KEY) || "{}");
      return {
        provider: c.provider || "anthropic",
        backend: c.backend || "anthropic",
        model: c.model || "claude-sonnet-4-6",
        base_url: c.base_url || "",
        key: c.key || "",
        // true when `model` is a free-text custom id (not a priced preset).
        model_is_custom: !!c.model_is_custom,
        budget_cap: c.budget_cap == null ? null : c.budget_cap,
        // Per-run file cap for directory sweeps; null = server env-default.
        max_files: c.max_files == null ? null : c.max_files,
      };
    } catch (_) {
      return { provider: "anthropic", backend: "anthropic", model: "claude-sonnet-4-6", base_url: "", key: "", model_is_custom: false, budget_cap: null, max_files: null };
    }
  }
  function saveCfg(c) { localStorage.setItem(CFG_KEY, JSON.stringify(c)); }

  // ---- model presets (single source of truth: GET /api/models) ------
  // Loaded once at init; falls back to a minimal built-in set if the fetch
  // fails so Settings still works offline. Keyed by provider id.
  let MODEL_PRESETS = {
    anthropic: [{ id: "claude-sonnet-4-6", label: "Claude Sonnet 4.6", input: 3, output: 15, default: true }],
    openrouter: [{ id: "anthropic/claude-sonnet-4.6", label: "Claude Sonnet 4.6", input: 3, output: 15, default: true }],
    openai: [{ id: "gpt-4o", label: "GPT-4o", input: 2.5, output: 10, default: true }],
  };
  const CUSTOM_OPT = "__custom__";
  // Live OpenRouter id → [input, output] $/Mtok map, filled from /api/models on
  // load. Lets the Settings page price a custom OpenRouter model id as it's typed.
  let OPENROUTER_PRICES = {};
  async function loadPresets() {
    try {
      const data = await api("GET", "/api/models");
      if (data && data.presets) MODEL_PRESETS = data.presets;
      if (data && data.openrouter_prices) OPENROUTER_PRICES = data.openrouter_prices;
    } catch (_) { /* keep built-in fallback */ }
  }
  // Live price for an OpenRouter model id, as a " · $in/$out per Mtok" suffix,
  // or "" when we have no figure. OpenRouter returns [input, output] pairs.
  function orPriceSuffix(model) {
    const p = OPENROUTER_PRICES[(model || "").trim()];
    return Array.isArray(p) ? (" · $" + p[0] + "/$" + p[1] + " per Mtok") : "";
  }
  // Hint under the model select. A custom OpenRouter id we have a live price for
  // shows that price; otherwise custom ids fall back to the no-estimate notice.
  function modelHint(provider, custom) {
    const m = PROVIDER_META[provider];
    if (m && m.modelFixed) return "fixed for this provider";
    if (!custom) return "presets include a cost estimate · pick Custom for any other id";
    if (provider === "openrouter") {
      const suffix = orPriceSuffix($("#set-model").value);
      if (suffix) return "custom model id" + suffix + " · live OpenRouter price";
    }
    return "custom model id — no cost estimate will be shown";
  }
  function presetsFor(id) { return MODEL_PRESETS[id] || []; }
  function isPresetModel(id, model) {
    return presetsFor(id).some((p) => p.id === model);
  }

  // The non-secret LLM selection rides on every request; the key is added only
  // when `includeKey` is set (the run/retry POSTs), so it isn't echoed into
  // server access logs for /api/models, /api/tree, /api/file, /api/clone, etc.
  function llmHeaders(includeKey) {
    const c = loadCfg();
    const h = {
      "X-LLM-Backend": c.backend,
      "X-LLM-Model": c.model,
      "X-LLM-Base-Url": c.base_url || "",
    };
    if (includeKey) h["X-LLM-Key"] = c.key || "";
    return h;
  }

  function refreshModelTag() {
    const c = loadCfg();
    // Show the provider's friendly name, not the wire backend: OpenRouter routes
    // through the "openai" backend, so c.backend would mislabel it as "openai".
    const label = (PROVIDER_META[c.provider] && PROVIDER_META[c.provider].label) || c.provider;
    $$("[data-modeltag]").forEach((e) => { e.textContent = label + " · " + c.model; });
    $("#scope-budget").textContent = budgetStr(c);
  }

  // ---- api ----------------------------------------------------------
  async function api(method, url, body, cfg) {
    cfg = cfg || {};
    const opts = { method, headers: { ...llmHeaders(cfg.includeKey) }, credentials: "same-origin" };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    const r = await fetch(url, opts);
    let data = {};
    try { data = await r.json(); } catch (_) { data = {}; }
    if (!r.ok || data.ok === false) {
      throw new Error(data.error || ("HTTP " + r.status));
    }
    return data;
  }

  // ---- global state -------------------------------------------------
  const S = {
    screen: "connect",
    sourceType: "github",
    repo: null,        // {repo, files, n_files}
    ref: "",
    tree: null,        // root node from /api/tree
    mode: "whole",     // whole | subdir | file
    selPath: "",       // selected dir/file path
    selFns: 0,
    fnAll: [],         // all function names in the current scope (file or directory)
    fnSel: null,       // Set of chosen names, or null = verify all
    fnFilter: "",      // substring filter for the function picker (long dir lists)
    runId: null,
    es: null,          // EventSource
    startMs: 0,
    timer: null,
    fileLoaded: "",
    local: false,      // local-folder (File System Access) mode
    localRoot: "",     // picked folder name
    localFiles: [],    // [{path, handle}] code files, read lazily at upload
  };

  // ---- screen routing ----------------------------------------------
  function go(screen) {
    S.screen = screen;
    ["connect", "scope", "run"].forEach((s) => {
      $("#screen-" + s).classList.toggle("hidden", s !== screen);
    });
  }

  // ==================================================================
  // CONNECT
  // ==================================================================
  function initConnect() {
    $$("#source-seg .seg").forEach((b) => b.addEventListener("click", () => {
      $$("#source-seg .seg").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      S.sourceType = b.dataset.src;
      const local = S.sourceType === "local";
      $("#remote-block").classList.toggle("hidden", local);
      $("#local-block").classList.toggle("hidden", !local);
      $("#connect-hint").textContent = local
        ? "Pick a folder, then choose which directory to verify →"
        : "After cloning, you choose what to verify →";
      const prefix = $("#repo-prefix"), label = $("#repo-fld-label"), inp = $("#repo-input");
      if (S.sourceType === "github") {
        prefix.style.display = ""; prefix.textContent = "github.com/";
        inp.placeholder = "owner/repo"; label.textContent = "repository";
      } else if (S.sourceType === "git") {
        prefix.style.display = "none";
        inp.placeholder = "https://git.example.com/org/repo.git"; label.textContent = "git url";
      }
    }));

    $("#repo-input").addEventListener("keydown", (e) => { if (e.key === "Enter") doClone(); });
    $("#ref-input").addEventListener("keydown", (e) => { if (e.key === "Enter") doClone(); });
    $("#clone-btn").addEventListener("click", doClone);
    $("#pick-folder-btn").addEventListener("click", pickFolder);
  }

  function repoUrl() {
    const v = $("#repo-input").value.trim();
    if (!v) return null;
    if (S.sourceType === "github") return "https://github.com/" + v.replace(/^\/+/, "");
    if (S.sourceType === "git") return v;
    return null; // local handled separately
  }

  async function doClone() {
    if (S.sourceType === "local") {
      toast("Local path cloning is only available on self-hosted instances.");
      return;
    }
    const url = repoUrl();
    if (!url) { toast("Enter a repository first."); return; }
    const ref = $("#ref-input").value.trim();
    const btn = $("#clone-btn");
    const orig = btn.innerHTML;
    btn.disabled = true; btn.innerHTML = "<span class='gly'>⟳</span> Cloning…";
    $("#connect-hint").textContent = "Cloning " + url + " …";
    try {
      const data = await api("POST", "/api/clone", { url, ref });
      S.repo = data; S.ref = ref || "HEAD";
      await enterScope();
      go("scope");
    } catch (err) {
      $("#connect-hint").textContent = "";
      toast("Clone failed: " + err.message);
    } finally {
      btn.disabled = false; btn.innerHTML = orig;
    }
  }

  // ---- local folder via File System Access API ---------------------
  const SKIP_DIRS = new Set([".git", "node_modules", "target", "build", "dist",
    ".venv", "venv", "__pycache__", ".cache", ".idea", ".vscode"]);
  // Verifiable source extensions, mirroring bmc_agent.source_parser.CODE_EXTENSIONS
  // (C/Rust/Java). Add a language here when one is added to the registry.
  const CODE_RE = /\.(c|h|i|rs|java)$/i;
  function langForName(name) {
    if (/\.rs$/i.test(name)) return "rust";
    if (/\.java$/i.test(name)) return "java";
    return "c";
  }

  async function pickFolder() {
    if (typeof window.showDirectoryPicker !== "function") {
      toast("Local folders need a Chromium browser (Chrome/Edge) on https or localhost.");
      return;
    }
    let dirHandle;
    try {
      dirHandle = await window.showDirectoryPicker({ mode: "read" });
    } catch (err) {
      if (err && err.name === "AbortError") return; // user cancelled
      toast("Could not open folder: " + (err.message || err));
      return;
    }
    $("#local-folder-name").textContent = "reading " + dirHandle.name + "/ …";
    S.localFiles = [];           // [{path, handle}] for code files (lazy read at upload)
    S.localRoot = dirHandle.name;
    const counter = { n: 0 };
    try {
      const root = await walkDir(dirHandle, "", counter);
      root.name = dirHandle.name; root.path = "";
      S.tree = root; S.local = true;
      $("#local-folder-name").textContent = dirHandle.name + "/ · " + S.localFiles.length + " source files";
      await enterScopeLocal();
      go("scope");
    } catch (err) {
      $("#local-folder-name").textContent = "no folder selected";
      toast("Could not read folder: " + (err.message || err));
    }
  }

  // Recursively enumerate (metadata only — no file contents read here).
  async function walkDir(dirHandle, rel, counter) {
    const node = { name: dirHandle.name, type: "dir", path: rel, n_functions: 0, children: [] };
    const dirs = [], files = [];
    for await (const [name, handle] of dirHandle.entries()) {
      if (++counter.n > 20000) break; // runaway guard
      if (name.startsWith(".") || SKIP_DIRS.has(name)) continue;
      const childPath = rel ? rel + "/" + name : name;
      if (handle.kind === "directory") {
        const child = await walkDir(handle, childPath, counter);
        node.n_functions += child.n_functions; // here n_functions = code-file count
        dirs.push(child);
      } else {
        const isCode = CODE_RE.test(name);
        files.push({ name, type: "file", path: childPath, lang: langForName(name), code: isCode });
        if (isCode) { node.n_functions += 1; S.localFiles.push({ path: childPath, handle }); }
      }
    }
    dirs.sort((a, b) => a.name.localeCompare(b.name));
    files.sort((a, b) => a.name.localeCompare(b.name));
    node.children = dirs.concat(files);
    return node;
  }

  // A fresh scope load starts from a clean slate: run settings back to their
  // defaults and the domain-knowledge box cleared. (Global settings — API key,
  // budget cap, max files — live in the gear modal and are intentionally kept;
  // the function picker is reset separately by the enter* flows.)
  function resetScopeFields() {
    saveOptions({});                                  // run settings → defaults
    const dk = $("#domain-input"); if (dk) dk.value = "";
    renderRunSettings();                              // reflect defaults + clear "N changed"
  }

  async function enterScopeLocal() {
    resetScopeFields();
    $("#scope-repo").textContent = S.localRoot + "/";
    $("#scope-ref").textContent = "local";
    $("#scope-cloned").textContent = S.localFiles.length + " source files · on your machine";
    $('#scope-cards .scope-card[data-mode="file"]').classList.remove("hidden");
    S.mode = "whole"; S.selPath = ""; S.selFns = 0;
    resetFnPicker();
    syncScopeCards();
    renderTree();
    updateScopeSummary();
    loadFnPicker();   // whole-project scope: populate the function list up front
  }

  // ==================================================================
  // SCOPE
  // ==================================================================
  async function enterScope() {
    resetScopeFields();
    S.local = false;
    $('#scope-cards .scope-card[data-mode="file"]').classList.remove("hidden");
    $("#scope-repo").textContent = (S.sourceType === "github" ? "github.com/" : "") + $("#repo-input").value.trim();
    $("#scope-ref").textContent = "ref " + (S.ref || "HEAD");
    $("#scope-cloned").textContent = S.repo.n_files + " source files · cloned";
    S.mode = "whole"; S.selPath = ""; S.selFns = 0;
    resetFnPicker();
    syncScopeCards();
    $("#tree").innerHTML = "<div class='empty' style='padding:12px 22px;'>loading tree…</div>";
    try {
      const data = await api("GET", "/api/tree?repo=" + encodeURIComponent(S.repo.repo));
      S.tree = data.tree;
      renderTree();
      updateScopeSummary();
      loadFnPicker();   // whole-project scope: populate the function list up front
    } catch (err) {
      $("#tree").innerHTML = "<div class='empty' style='padding:12px 22px;'>tree failed: " + esc(err.message) + "</div>";
    }
  }

  function initScope() {
    $$("#scope-cards .scope-card").forEach((c) => c.addEventListener("click", () => {
      S.mode = c.dataset.mode;
      // selection must match the mode; clear if incompatible
      if (S.mode === "whole") { S.selPath = ""; }
      syncScopeCards();
      renderTree();
      updateScopeSummary();
      // Whole scope can load its function list immediately; subdir/file wait for a
      // tree selection (loadFnPicker resets the picker when there's nothing to load).
      loadFnPicker();
    }));
    $("#change-source").addEventListener("click", () => { clearRunInUrl(); go("connect"); });
    $("#run-btn").addEventListener("click", startRun);
    const fnSearch = $("#fnpick-search");
    if (fnSearch) fnSearch.addEventListener("input", () => {
      S.fnFilter = fnSearch.value || "";
      renderFnPicker();
    });
    const fnToggle = $("#fnpick-toggle");
    if (fnToggle) fnToggle.addEventListener("click", () => {
      if (!fnPickerActive()) return;
      // All selected → clear (= verify all, a stepping stone to picking a few);
      // otherwise re-select everything. Operates on the full list, not the filter.
      S.fnSel = (S.fnSel.size === S.fnAll.length) ? new Set() : new Set(S.fnAll);
      renderFnPicker();
      updateScopeSummary();
    });
  }

  function syncScopeCards() {
    $$("#scope-cards .scope-card").forEach((c) => c.classList.toggle("on", c.dataset.mode === S.mode));
    $("#tree-hint").textContent = S.mode === "whole" ? "whole project selected"
      : (S.mode === "file" ? "select a file to scope" : "select a directory to scope");
  }

  function renderTree() {
    const host = $("#tree");
    host.innerHTML = "";
    if (!S.tree) return;
    // render root's children (skip the root dir node itself)
    const rows = [];
    walkTree(S.tree, 0, rows, true);
    rows.forEach((r) => host.appendChild(r));
  }

  function walkTree(node, depth, rows, isRoot) {
    if (!isRoot) {
      rows.push(treeRow(node, depth));
    }
    if (node.type === "dir" && node.children) {
      const d = isRoot ? 0 : depth + 1;
      node.children.forEach((ch) => walkTree(ch, d, rows, false));
    }
  }

  function treeRow(node, depth) {
    const row = document.createElement("div");
    row.className = "trow";
    const isDir = node.type === "dir";
    // Files are selectable in "file" mode; in local mode only code files qualify
    // (the tree also lists non-code files for context).
    const selectable = isDir ? (S.mode === "subdir") : (S.mode === "file" && (!S.local || node.code));
    const isSel = selectable && S.selPath === node.path;
    if (isSel) row.classList.add("sel");
    row.style.paddingLeft = (22 + depth * 18) + "px";
    const ico = isDir ? "📁" : "📄";
    const unit = S.local ? "files" : "fns";
    // Local file nodes carry no function count (computed after upload) — a single
    // source file counts as 1.
    const count = node.n_functions != null ? node.n_functions : 1;
    let badge;
    if (!isDir && S.local && !node.code) {
      badge = ""; row.style.opacity = ".55"; // non-code file, shown for context only
    } else if (isSel) {
      badge = "<span class='badge'>selected · " + count + " " + unit + "</span>";
    } else if (isDir) {
      badge = "<span class='cnt'>" + node.n_functions + " " + unit + "</span>";
    } else {
      badge = ""; // individual files: no count needed
    }
    row.innerHTML =
      "<span class='ico'>" + ico + "</span>" +
      "<span class='nm'>" + esc(node.name) + (isDir ? "/" : "") + "</span>" + badge;
    if (selectable) {
      row.addEventListener("click", () => {
        S.selPath = node.path;
        S.selFns = node.n_functions != null ? node.n_functions : 1;
        renderTree();
        updateScopeSummary();
        // Both file and subdir scope target a selection — (re)load its functions.
        loadFnPicker();
      });
    } else {
      row.style.cursor = "default";
    }
    return row;
  }

  // ---- per-function picker (any scope) -------------------------------------
  // Restrict a run to a subset of functions (only_functions), matching the CLI's
  // --functions. The function list's source depends on scope + origin:
  //   single file  — cloned: GET /api/functions; local: POST /api/functions-raw
  //   whole/subdir — cloned: GET /api/functions-dir; local: POST /api/functions-raw
  //                  with a {files:[…]} batch (union across the in-scope handles).
  // For directory scope the cross-file call graph is still built over the whole
  // dir; only Phase 2 BMC is narrowed to the chosen names (server-side).
  function fnPickerActive() {
    const scoped = S.mode === "whole" || !!S.selPath;
    return scoped && S.fnAll.length > 0;
  }
  // The chosen subset, or null when "all" (omit only_functions ⇒ verify all).
  function chosenFunctions() {
    if (!fnPickerActive() || !S.fnSel) return null;
    if (S.fnSel.size === 0 || S.fnSel.size === S.fnAll.length) return null;
    return Array.from(S.fnSel);
  }
  function resetFnPicker() {
    S.fnAll = []; S.fnSel = null; S.fnFilter = "";
    const sb = $("#fnpick-search"); if (sb) sb.value = "";
    renderFnPicker();
  }
  // Read the in-scope local code-file handles' contents (whole/subdir scope).
  // Mirrors uploadLocalSelection's inScope prefix, code files only.
  async function readLocalScopeSources() {
    const prefix = S.mode === "whole" ? "" : S.selPath;
    const inScope = (p) => !prefix || p === prefix || p.startsWith(prefix + "/");
    const chosen = S.localFiles.filter((f) => inScope(f.path));
    const files = [];
    for (const f of chosen) {
      files.push({ name: f.path, content: await (await f.handle.getFile()).text() });
    }
    return files;
  }
  async function loadFnPicker() {
    resetFnPicker();
    if (S.mode !== "whole" && !S.selPath) return;
    try {
      let data;
      if (S.local) {
        // No server-side files yet — parse the handles the browser already holds.
        if (S.mode === "file") {
          const f = S.localFiles.find((x) => x.path === S.selPath);
          if (!f) { renderFnPicker(); return; }
          const content = await (await f.handle.getFile()).text();
          data = await api("POST", "/api/functions-raw", { name: S.selPath, content });
        } else {
          const files = await readLocalScopeSources();
          if (!files.length) { renderFnPicker(); return; }
          data = await api("POST", "/api/functions-raw", { files });
        }
      } else {
        if (!S.repo || !S.repo.repo) return;
        const r = encodeURIComponent(S.repo.repo);
        data = (S.mode === "file")
          ? await api("GET", "/api/functions?repo=" + r + "&path=" + encodeURIComponent(S.selPath))
          : await api("GET", "/api/functions-dir?repo=" + r + "&path=" + encodeURIComponent(S.mode === "whole" ? "" : S.selPath));
      }
      S.fnAll = data.functions || [];
      S.fnSel = new Set(S.fnAll);   // default: all selected
    } catch (_) {
      S.fnAll = []; S.fnSel = null; // graceful fallback: verify all
    }
    renderFnPicker();
    updateScopeSummary();
  }
  function renderFnPicker() {
    const box = $("#fnpick");
    if (!fnPickerActive()) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    // Show the search box only when the list is long enough to warrant filtering.
    const search = $("#fnpick-search");
    if (search) search.classList.toggle("hidden", S.fnAll.length <= 8);
    const q = (S.fnFilter || "").toLowerCase();
    const list = $("#fnpick-list");
    list.innerHTML = "";
    for (const name of S.fnAll) {
      if (q && !name.toLowerCase().includes(q)) continue;
      const row = document.createElement("label");
      row.className = "fnrow";
      const cb = document.createElement("input");
      cb.type = "checkbox"; cb.checked = S.fnSel.has(name);
      cb.addEventListener("change", () => {
        if (cb.checked) S.fnSel.add(name); else S.fnSel.delete(name);
        updateFnHint();
        updateScopeSummary();
      });
      const nm = document.createElement("span");
      nm.className = "fnname mono"; nm.textContent = name;
      row.appendChild(cb); row.appendChild(nm);
      list.appendChild(row);
    }
    updateFnHint();
  }
  function updateFnHint() {
    if (!fnPickerActive()) return;
    const sel = S.fnSel.size, all = S.fnAll.length;
    $("#fnpick-hint").textContent = sel === 0 ? "none selected — verify all"
      : sel === all ? "all selected (" + all + ")"
      : sel + " of " + all + " selected";
    // The header button doubles as deselect/reset: clear when all are selected,
    // otherwise re-select everything.
    const tog = $("#fnpick-toggle");
    if (tog) tog.textContent = sel === all ? "deselect all" : "select all";
  }
  // Optional free-text domain knowledge injected into spec generation.
  function domainKnowledge() {
    const el = $("#domain-input");
    return el ? (el.value || "").trim() : "";
  }

  function selectedFnCount() {
    if (!S.tree) return 0;
    const chosen = chosenFunctions();
    if (chosen) return chosen.length;
    if (S.mode === "whole") return S.tree.n_functions || 0;
    return S.selFns || 0;
  }

  function budgetStr(cfg) {
    return cfg.budget_cap ? ("$" + Number(cfg.budget_cap).toFixed(2)) : "unlimited";
  }
  function fmtTok(n) {
    n = Math.round(n || 0);
    return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "K" : String(n);
  }
  function fmtUsd(n) { return "$" + Number(n || 0).toFixed(2); }

  // One-line summary string for the Scope footer from an /api/estimate result.
  function fmtEstimateLine(est, cfg) {
    const tok = est.total_tokens || {};
    const usd = est.usd || {};
    const tokStr = "~" + fmtTok(tok.expected) + " tok (" + fmtTok(tok.low) + "–" + fmtTok(tok.high) + ")";
    let money;
    if (est.free) money = "free";
    else if (usd.expected == null) money = "no $ estimate (custom model)";
    else money = "~" + fmtUsd(usd.expected) + " (" + fmtUsd(usd.low) + "–" + fmtUsd(usd.high) + ")";
    return "est. " + money + " · " + tokStr + " · budget " + budgetStr(cfg);
  }

  let _estTimer = null;
  function updateScopeSummary() {
    const n = selectedFnCount();
    const noun = S.local ? "source files" : "functions";
    let label;
    if (S.mode === "whole") label = "Whole project";
    else if (S.mode === "file") label = "Single file: " + (S.selPath ? baseName(S.selPath) : "(pick a file)");
    else label = "Subdirectory: " + (S.selPath || "(pick a directory)");
    $("#scope-label").textContent = label;
    $("#scope-detail").textContent = n + " " + noun;
    const cfg = loadCfg();
    const ready = S.mode === "whole" || !!S.selPath;
    $("#run-btn").disabled = !ready;
    $("#run-btn").style.opacity = ready ? "1" : ".5";
    S.lastEstimate = null;

    if (!ready) {
      $("#scope-est").innerHTML = "est. — · budget " + budgetStr(cfg);
      return;
    }
    if (S.local) {
      // function counts aren't known until the server parses the upload.
      $("#scope-est").innerHTML = "est. computed after upload · budget " + budgetStr(cfg);
      return;
    }
    // Real, tokenizer-grounded estimate from the server (debounced).
    $("#scope-est").innerHTML = "estimating… · budget " + budgetStr(cfg);
    if (!S.repo || !S.repo.repo) {
      $("#scope-est").innerHTML = "est. — · budget " + budgetStr(cfg);
      return;
    }
    clearTimeout(_estTimer);
    _estTimer = setTimeout(async () => {
      const body = { repo: S.repo.repo, mode: S.mode, path: S.mode === "whole" ? "" : S.selPath, max_files: loadCfg().max_files, options: buildOptions() };
      const onlyFns = chosenFunctions();
      if (onlyFns) body.only_functions = onlyFns;
      try {
        const est = (await api("POST", "/api/estimate", body)).estimate;
        S.lastEstimate = est;
        // Re-gate the run-settings panel when the in-scope languages change
        // (e.g. C-only modeling knobs hidden for a Rust/Java scope).
        if (est.languages && est.languages.join() !== (S.langs || []).join()) {
          S.langs = est.languages;
          applyLangGating();
        }
        $("#scope-est").innerHTML = esc(fmtEstimateLine(est, loadCfg()));
      } catch (_) {
        // graceful fallback: coarse per-fn heuristic.
        const c = loadCfg();
        const perFn = /opus/i.test(c.model) ? 0.18 : /haiku/i.test(c.model) ? 0.012 : 0.04;
        $("#scope-est").innerHTML = "est. ~$" + (n * perFn).toFixed(2) + " · ~" +
          Math.round(n * 1.1) + "K tok · budget " + budgetStr(c);
      }
    }, 350);
  }

  // Fetch an estimate for a specific run body (used by the confirm gate; the
  // local-upload path needs this after the upload, when functions are known).
  async function fetchEstimate(body) {
    try { return (await api("POST", "/api/estimate", body)).estimate; }
    catch (_) { return null; }
  }

  // ==================================================================
  // RUN
  // ==================================================================
  // "2m10s" / "45s" / "1h05m" — mirrors bmc_agent.eta.fmt_duration.
  function fmtDuration(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return s + "s";
    if (s < 3600) {
      const m = Math.floor(s / 60), sec = s % 60;
      return m + "m" + String(sec).padStart(2, "0") + "s";
    }
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h + "h" + String(m).padStart(2, "0") + "m";
  }

  function resetRunView() {
    S.phases = {}; S.phaseDetail = {}; S.functions = {}; S.bmcFunctions = {}; S.findings = []; S.fileLoaded = "";
    // Per-function tree state: ordered names, built-once DOM refs, per-function
    // classify caption + iteration label, and the global classify batch note.
    S.fnList = []; S.fnRows = {}; S.fnDetail = {}; S.fnIter = {}; S.classifyNote = ""; S.curFn = "";
    // Per-file grouping: a directory sweep verifies files sequentially, so rows
    // are grouped under a per-file header (shown only when >1 file) — otherwise
    // the next file's spec phase reads as "regenerating" the previous file's rows.
    // fnFile: name -> source file; fileOrder: files in arrival order;
    // fileSections: file -> {el, hdr, host} built-once DOM refs.
    S.fnFile = {}; S.fileOrder = []; S.fileSections = {};
    // The file whose phase status S.phases currently reflects — phase keys are
    // reused per file in a directory sweep, so they're reset at each new file.
    S.curFile = "";
    // Directory-sweep file-selection breakdown (type=tree_summary), null until
    // the sweep reports how many files it found / capped / skipped.
    S.treeSummary = null;
    // Per-function spec chip state: name -> "active"|"done". Lets each row's spec
    // chip settle independently (spec-gen runs functions in a layer in parallel).
    S.specFn = {};
    // Setup checklist steps (directory pre-pass + per-file spec sub-steps),
    // appended live before the function rows seed. {key,label,chip,lbl}.
    S.setupSteps = [];
    S.eta = null;
    $("#findings").innerHTML = "<div class='empty'>No findings yet — the pipeline is running.</div>";
    $("#findings-count").textContent = "0 confirmed";
    $("#findings-count").className = "tag";
    $("#logbox").textContent = "";
    $("#recovery-banner").classList.add("hidden");
    $("#run-tree").classList.add("hidden");
    $("#src").innerHTML = "";
    $("#elapsed").textContent = fmtDuration(0);
    $("#eta").textContent = "estimating…";
    updateReliability(null);
    setPill("live", "verifying");
    $("#pause-btn").textContent = "❚❚ pause"; $("#pause-btn").disabled = false;
    $("#cancel-btn").disabled = false;
    $("#activity").classList.remove("hidden");
    initPipeline();
  }

  function setPill(kind, text) {
    const p = $("#run-pill");
    p.className = "pill " + (kind === "warn" ? "warn" : kind === "idle" ? "" : "live");
    $("#run-status").textContent = text;
  }

  // ---- run-confirm dialog -------------------------------------------
  let _confirmResolve = null;
  function _closeConfirm(ok) {
    $("#confirm-overlay").classList.add("hidden");
    const r = _confirmResolve; _confirmResolve = null;
    if (r) r(ok);
  }
  // Show the estimate and wait for an explicit Run/Cancel. Resolves true/false.
  function confirmRun(est) {
    const cfg = loadCfg();
    $("#confirm-body").innerHTML = renderConfirm(est, cfg);
    $("#confirm-overlay").classList.remove("hidden");
    return new Promise((resolve) => { _confirmResolve = resolve; });
  }
  function renderConfirm(est, cfg) {
    const row = (k, v, cls) => "<div class='crow" + (cls ? " " + cls : "") +
      "'><span class='ck'>" + esc(k) + "</span><span class='cv'>" + v + "</span></div>";
    if (!est) {
      return "<div class='hint' style='margin-bottom:10px;'>Couldn't compute an estimate — you can still run; the budget cap will stop it if it overspends.</div>" +
        row("budget cap", esc(budgetStr(cfg)));
    }
    const tok = est.total_tokens || {}, usd = est.usd || {}, reqs = est.requests || {};
    // Lead the scope row with the run MODE in words — a whole-project sweep is
    // otherwise easy to mistake for the single file the user clicked.
    const modeLabel = S.mode === "file" ? "Single file: " + baseName(S.selPath)
      : S.mode === "subdir" ? "Subdirectory: " + (S.selPath || "(none)")
      : "Whole project";
    const countStr = est.n_functions + (est.n_functions === 1 ? " function" : " functions") +
      (est.n_files ? " · " + est.n_files + (est.n_files === 1 ? " file" : " files") : "");
    const scope = modeLabel + " — " + countStr;
    let cost;
    if (est.free) cost = "<b>free</b>";
    else if (usd.expected == null) cost = "—  <span class='hint'>no estimate for a custom model</span>";
    else cost = "<b>~" + fmtUsd(usd.expected) + "</b>  <span class='hint'>(" + fmtUsd(usd.low) + "–" + fmtUsd(usd.high) + ")</span>";
    let out = row("scope", esc(scope)) +
      row("LLM requests", "~" + (reqs.expected || 0) + "  <span class='hint'>(" + (reqs.low || 0) + "–" + (reqs.high || 0) + ")</span>") +
      row("tokens", "~" + fmtTok(tok.expected) + "  <span class='hint'>(" + fmtTok(tok.low) + "–" + fmtTok(tok.high) + ")</span>") +
      row("est. cost", cost) +
      row("budget cap", esc(budgetStr(cfg)));
    // Directory sweeps verify only the first N files (runner cap); say so when
    // the scope holds more, so "whole project · 95 files" doesn't mislead.
    const cap = est.max_files || 0;
    if (S.mode !== "file" && cap && est.n_files > cap) {
      out += "<div class='errline' style='margin-top:12px;display:block;'>⚠ Verifies only the first " +
        cap + " of " + est.n_files + " files. Scope to a file or subdirectory to target the rest.</div>";
    }
    // Budget warning when the priced estimate may cross the cap.
    if (cfg.budget_cap && !est.free && usd.expected != null) {
      if (usd.high > cfg.budget_cap) {
        const overExpected = usd.expected > cfg.budget_cap;
        out += "<div class='errline' style='margin-top:12px;display:block;'>⚠ " +
          (overExpected
            ? "The expected cost (~" + fmtUsd(usd.expected) + ") exceeds your budget cap (" + fmtUsd(cfg.budget_cap) + "). "
            : "The high end (" + fmtUsd(usd.high) + ") could exceed your budget cap (" + fmtUsd(cfg.budget_cap) + "). ") +
          "The run stops cleanly once spend crosses the cap.</div>";
      }
    }
    return out;
  }

  async function startRun() {
    if (!loadCfg().key) { openSettings(); toast("Add an API key to run a verification."); return; }
    const ready = S.mode === "whole" || !!S.selPath;
    if (!ready) { toast("Pick a scope first."); return; }

    if (S.local) {
      // Lazily read + upload only the selected subtree, then estimate + confirm
      // (function counts aren't known until the server parses the upload).
      go("run");
      resetRunView();
      $("#src-name").textContent = S.mode === "whole" ? S.localRoot : (S.mode === "file" ? baseName(S.selPath) : S.selPath);
      // Single file: render its source immediately from the local file handle
      // (no server round-trip), so the left panel shows code rather than a
      // placeholder while the upload + estimate run.
      if (S.mode === "file") loadSource(S.selPath);
      else $("#src").innerHTML = "<div class='empty' style='padding:14px 16px;'>Uploading selected source files…</div>";
      setPill("live", "uploading");
      try {
        S.repo = await uploadLocalSelection(); // {repo, n_files, files}
      } catch (err) {
        setPill("warn", "failed");
        toast("Upload failed: " + err.message);
        $("#activity").classList.add("hidden");
        return;
      }
      const body = { repo: S.repo.repo, mode: "whole", path: "", budget_cap: loadCfg().budget_cap, max_files: loadCfg().max_files, options: buildOptions() };
      const dkLocal = domainKnowledge();
      if (dkLocal) body.domain_knowledge = dkLocal;
      const onlyFnsLocal = chosenFunctions();
      if (onlyFnsLocal) body.only_functions = onlyFnsLocal;
      const est = await fetchEstimate({ repo: S.repo.repo, mode: "whole", path: "", max_files: loadCfg().max_files, options: buildOptions(), only_functions: onlyFnsLocal || undefined });
      if (!(await confirmRun(est))) { go("scope"); return; }
      setPill("live", "verifying");
      // Single-file source is already shown (loaded above); only swap in the
      // "Verifying…" placeholder for whole/subdir scopes.
      if (S.mode !== "file") $("#src").innerHTML = "<div class='empty' style='padding:14px 16px;'>Verifying " + esc($("#scope-detail").textContent) + ". Click a finding to view its source.</div>";
      await launchRun(body);
      return;
    }

    const body = {
      repo: S.repo.repo,
      mode: S.mode,
      path: S.mode === "whole" ? "" : S.selPath,
      budget_cap: loadCfg().budget_cap,
      max_files: loadCfg().max_files,
      options: buildOptions(),
    };
    const dk = domainKnowledge();
    if (dk) body.domain_knowledge = dk;
    const onlyFns = chosenFunctions();
    if (onlyFns) body.only_functions = onlyFns;
    const est = S.lastEstimate || await fetchEstimate({ repo: body.repo, mode: body.mode, path: body.path, max_files: loadCfg().max_files, options: buildOptions(), only_functions: onlyFns || undefined });
    if (!(await confirmRun(est))) return; // declined — stay on the Scope screen
    go("run");
    resetRunView();
    $("#src-name").textContent = S.mode === "file" ? baseName(S.selPath) : (S.mode === "subdir" ? S.selPath : S.repo.repo);
    $("#src-meta").textContent = selectedFnCount() + " fns";
    if (S.mode === "file") loadSource(S.selPath);
    else $("#src").innerHTML = "<div class='empty' style='padding:14px 16px;'>Verifying " + esc($("#scope-detail").textContent) + ". Click a finding to view its source.</div>";
    await launchRun(body);
  }

  // POST /api/run and open the live event stream.
  async function launchRun(body) {
    try {
      const data = await api("POST", "/api/run", body, { includeKey: true });
      S.runId = data.run_id;
      setRunInUrl(S.runId);
      startTimer();
      openStream(data.run_id);
    } catch (err) {
      setPill("warn", "failed");
      toast("Could not start run: " + err.message);
      $("#activity").classList.add("hidden");
    }
  }

  function initConfirm() {
    $("#confirm-go").addEventListener("click", () => _closeConfirm(true));
    $("#confirm-cancel").addEventListener("click", () => _closeConfirm(false));
    $("#confirm-x").addEventListener("click", () => _closeConfirm(false));
    $("#confirm-scrim").addEventListener("click", () => _closeConfirm(false));
  }

  // Read (lazily) the code files under the selected scope and POST them.
  // Headers are ALWAYS included, even for a single-file/subdir scope: the file
  // being verified #includes the project's own headers (often in a sibling
  // include/ dir outside the scope), and without them cc -E can't resolve the
  // include — the harness then fails to build for every function.
  const HDR_RE = /\.(h|hpp|hh|hxx)$/i;
  async function uploadLocalSelection() {
    const prefix = S.mode === "whole" ? "" : S.selPath;
    const inScope = (p) =>
      !prefix || p === prefix || p.startsWith(prefix + "/");
    const chosen = S.localFiles.filter((f) => inScope(f.path) || HDR_RE.test(f.path));
    if (!chosen.length) throw new Error("No source files in the selection.");
    const files = [];
    for (const f of chosen) {
      const file = await f.handle.getFile();
      files.push({ path: f.path, content: await file.text() });
    }
    const data = await api("POST", "/api/upload", { name: S.localRoot, files });
    return { repo: data.repo, n_files: data.n_files, files: data.files };
  }

  // Flatten a tree node into [{path}] for every file, so resolveRepoPath/
  // loadSource can map a finding's basename to its repo-relative path after a
  // refresh (the original clone/upload listing is gone).
  function flattenFiles(node, out) {
    out = out || [];
    if (!node) return out;
    if (node.type === "file") out.push({ path: node.path });
    (node.children || []).forEach((c) => flattenFiles(c, out));
    return out;
  }

  // Reconnect to an existing run after a page refresh: rebuild the run view from
  // the server snapshot, then replay + continue its event stream. Returns false
  // (and clears the URL) when the run is gone/unreachable, leaving the user on
  // Connect.
  async function restoreRun(runId) {
    let snap;
    try {
      snap = await api("GET", "/api/run/" + runId);
    } catch (_) {
      clearRunInUrl();
      return false;
    }
    S.runId = runId;
    const scope = snap.scope || {};
    S.repo = { repo: scope.repo || "", files: [] };
    S.mode = scope.mode || "whole";
    S.selPath = scope.path || "";

    // Best-effort: rebuild the file list for source-path resolution.
    if (S.repo.repo) {
      try {
        const t = await api("GET", "/api/tree?repo=" + encodeURIComponent(S.repo.repo));
        S.tree = t.tree;
        S.repo.files = flattenFiles(t.tree);
      } catch (_) { /* path resolution degrades to best-effort */ }
    }

    go("run");
    resetRunView();

    // Header + source panel, mirroring startRun's non-local branch.
    $("#src-name").textContent = S.mode === "file" ? baseName(S.selPath)
      : (S.mode === "subdir" ? S.selPath : (S.repo.repo || "project"));
    if (S.mode === "file" && S.selPath) {
      loadSource(S.selPath);
    } else {
      $("#src").innerHTML = "<div class='empty' style='padding:14px 16px;'>Verifying. Click a finding to view its source.</div>";
    }

    // Seed elapsed from the snapshot so the timer stays accurate post-refresh.
    const elapsed = (snap.eta && snap.eta.elapsed_s) || 0;
    const live = snap.status === "running" || snap.status === "paused";
    if (live) {
      S.startMs = Date.now() - elapsed * 1000;
      startTimer();
    } else {
      $("#elapsed").textContent = fmtDuration(elapsed);
    }
    if (snap.cost) { updateSpend(snap.cost); updateReliability(snap.cost.reliability); }

    // Replay rebuilds phases/functions/findings/cost; a finished run's terminal
    // 'done' event drives finishRun, a live one then streams new events.
    openStream(runId);

    // Reflect a paused run (no terminal event will arrive to set this).
    if (snap.status === "paused") {
      const btn = $("#pause-btn");
      btn.dataset.paused = "1"; btn.textContent = "▶ resume";
      setPill("warn", "paused");
    }
    return true;
  }

  function openStream(runId) {
    if (S.es) { S.es.close(); S.es = null; }
    const es = new EventSource("/api/run/" + runId + "/events");
    S.es = es;
    const handle = (ev) => {
      let d = {};
      try { d = JSON.parse(ev.data); } catch (_) { return; }
      onEvent(d);
    };
    // server names events by their "type"; listen for the known set + default
    ["phase", "function", "spec_fn", "prep", "tree_summary", "finding", "log", "started", "result", "progress", "done", "error", "message"]
      .forEach((name) => es.addEventListener(name, handle));
    es.onmessage = handle;
    es.onerror = () => { /* server closes the stream when done; ignore */ };
  }

  function onEvent(d) {
    const t = d.type;
    if (t === "phase") {
      // A directory sweep runs files sequentially but reuses the SAME phase keys
      // (spec/bmc/classify/report) per file. Scope them to the current file so a
      // finished file's "complete" doesn't bleed onto the next file's rows (bmc
      // showing green before it runs) or the bottom strip ("done" mid-run). The
      // per-function maps (specFn, bmcFunctions, …) persist, so already-finished
      // files' rows stay correct without the global phase status. (A BMC-skipped
      // function in an earlier file may briefly show pending while a later file
      // runs — rare/transient; per-file phase maps would be the robust upgrade.)
      if (d.file && d.file !== S.curFile) {
        S.curFile = d.file;
        S.phases = {};
        S.phaseDetail = {};
      }
      S.phases[d.phase] = d.status;
      // BMC start carries function_locs (name→LOC): the full function list,
      // in order — seed the tree rows from it.
      if (d.function_locs) seedFunctions(d.function_locs, d.file);
      // Live captions: a per-function note (d.function) lands on that row; a
      // batch classify note (3b/3c) becomes the global classify line; spec
      // progress feeds the global spec line. Cleared on completion.
      if (d.status === "complete") { delete S.phaseDetail[d.phase]; if (d.phase === "classify") S.classifyNote = ""; }
      else if (d.detail) {
        if (d.function) S.fnDetail[d.function] = d.detail;
        else if (d.phase === "classify") S.classifyNote = d.detail;
        else S.phaseDetail[d.phase] = d.detail;
        // Per-file spec sub-steps (parse, analyze, preprocess) feed the setup
        // checklist until the rows seed, continuing the directory pre-pass list.
        if (d.phase === "spec" && S.fnList.length === 0) noteSetup(d.detail);
      }
      renderPipeline();
    } else if (t === "function") {
      S.functions[d.name] = d;
      // BMC verdicts (verified / CEX) read only from BMC-phase events; classify
      // events carry the per-function caption + iteration label instead.
      if (d.phase === "bmc") S.bmcFunctions[d.name] = d;
      // Inline captions are classify-only; a bmc "unresolved" detail rides on
      // S.bmcFunctions[name].detail and surfaces as the chip tooltip instead.
      if (d.phase === "classify" && d.detail) S.fnDetail[d.name] = d.detail;
      if (d.iter) S.fnIter[d.name] = d.iter;
      noteFn(d.name, d.file);  // lazy (refresh replay) + remember its file
      S.curFn = d.name;
      renderPipeline();
      if (d.status === "verified") renderFindings();   // accumulate proofs live
    } else if (t === "spec_fn") {
      // Per-function spec progress: chip goes active -> done as each spec is
      // generated. Defensive seed in case the row wasn't seeded yet.
      if (d.name) {
        S.specFn[d.name] = d.status;
        noteFn(d.name, d.file);
      }
      renderPipeline();
    } else if (t === "prep") {
      // Directory-level pre-pass heartbeat (call-graph build, domain analysis)
      // — runs before the first file's spec phase, so it has its own checklist
      // steps to keep the column from sitting blank.
      if (S.fnList.length === 0) noteSetup(d.detail);
      renderPipeline();
    } else if (t === "tree_summary") {
      // Directory sweep file-selection breakdown (found / cap / verified /
      // skipped-no-functions / not-reached). Later events supersede earlier
      // ones (the start event omits verified/skipped; the final fills them in).
      S.treeSummary = d;
      renderPipeline();
    } else if (t === "finding") {
      // Dedupe by identity: the browser's native EventSource silently
      // reconnects on a transient drop and the server replays its event buffer,
      // so a plain push would double the findings list and the "N confirmed"
      // badge. Every other stream slice is keyed and replay-safe; this is the
      // one append-only path.
      if (d.bug && addFinding(d.bug)) appendOneFinding(d.bug);
    } else if (t === "log") {
      appendLog(d.message || "");
    } else if (t === "result") {
      if (d.bugs && !S.findings.length) { S.findings = d.bugs.slice(); renderFindings(); }
    } else if (t === "done") {
      finishRun(d);
    } else if (t === "error") {
      appendLog("ERROR: " + (d.message || ""));
    }
    if (d.cost) { updateSpend(d.cost); updateReliability(d.cost.reliability); }
    // ETA rides along on every event; remember the latest server estimate +
    // when we got it, so the timer can count it down smoothly between updates.
    if (d.eta) S.eta = { rem: d.eta.remaining_s, at: Date.now() };
  }

  function updateSpend(cost) {
    const tok = cost.total_tokens || 0;
    $("#spend-tok").textContent = (tok >= 1000 ? (tok / 1000).toFixed(1) + "K" : tok) + " tok";
    const usd = cost.usd;
    $("#spend-usd").textContent = usd == null ? "—" : "$" + Number(usd).toFixed(2);
  }

  function fmtLatency(ms) {
    if (ms == null) return "";
    return ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : Math.round(ms) + "ms";
  }

  // API reliability + latency badge. `rel` is the cost.reliability sub-dict from
  // the pipeline (LLMClient.reliability_snapshot). The forecast dot is coloured
  // from the last <=5 calls: <=1 fail green, 2-3 yellow, >=4 red.
  function updateReliability(rel) {
    const badge = $("#rel-badge");
    if (!rel || !rel.total) {
      $("#rel-pct").textContent = "—"; $("#rel-lat").textContent = "";
      badge.className = "rel"; badge.title = "API reliability — no calls yet";
      return;
    }
    const total = rel.total, pct = (n) => Math.round(((n || 0) / total) * 100);
    $("#rel-pct").textContent = pct(rel.success) + "%";
    const lat = rel.latency_ms_recent != null ? rel.latency_ms_recent : rel.latency_ms_avg;
    $("#rel-lat").textContent = lat != null ? "· " + fmtLatency(lat) : "";
    const f = rel.recent_fail || 0;
    const level = f >= 4 ? "bad" : f >= 2 ? "warn" : "ok";
    badge.className = "rel rel-" + level;
    const parts = [pct(rel.success) + "% success"];
    if (rel.timeout) parts.push(pct(rel.timeout) + "% timeout");
    if (rel.decode) parts.push(pct(rel.decode) + "% decode (invalid response)");
    if (rel.other) parts.push(pct(rel.other) + "% other");
    const latTxt = rel.latency_ms_avg != null ? " · ~" + fmtLatency(rel.latency_ms_avg) + " avg" : "";
    badge.title = parts.join(" · ") + "\nlast " + (rel.recent_total || 0) + ": " + f + " failed" + latTxt;
  }

  // ==================================================================
  // PIPELINE — unified per-function tree
  // ==================================================================
  // The middle column is one list of function rows; each row carries its own
  // spec → bmc → classify progress, and a function with counterexamples expands
  // a nested classify sub-block. Rows are built ONCE and patched in place (class
  // / text only, every write diff-guarded) so streaming events never rebuild the
  // DOM — that is what keeps the column from jittering "up and down".
  const CHIP = {
    pending: { g: "", c: "pending" },
    active:  { g: "", c: "active" },
    done:    { g: "", c: "done" },
    cex:     { g: "", c: "cex" },
    warn:    { g: "", c: "warn" },
  };

  // Record a function (ordered list + its source file) the first time it's seen.
  // The file groups rows under a per-file header during multi-file sweeps; rows
  // and headers build once, so this only ever appends.
  function noteFn(name, file) {
    if (!name) return;
    if (!(name in S.fnFile)) {
      const f = file || "";
      S.fnFile[name] = f;
      if (S.fileOrder.indexOf(f) === -1) S.fileOrder.push(f);
    }
    if (S.fnList.indexOf(name) === -1) S.fnList.push(name);
  }

  // Add names to the ordered list (no rebuild; new rows append on next render).
  function seedFunctions(locs, file) {
    Object.keys(locs || {}).forEach((name) => noteFn(name, file));
  }

  // Build the column scaffold once per run: global spec line, the rows
  // container, the classify batch-note line, and the report footer.
  function initPipeline() {
    const host = $("#pipeline");
    host.className = "ftree";
    host.innerHTML =
      "<div class='tree-files hidden' id='tree-files'></div>" +
      "<div class='tree-setup hidden' id='tree-setup'></div>" +
      "<div class='tree-spec hidden' id='tree-spec'></div>" +
      "<div class='ftree-rows' id='ftree-rows'></div>" +
      "<div class='tree-note hidden' id='tree-note'></div>" +
      "<div class='tree-report hidden' id='tree-report'></div>";
    S.fnRows = {};
    renderPipeline();
  }

  // Append (or advance) a setup-checklist step. The step key is the caption with
  // any "· n/m" counter suffix stripped, so a counter tick (e.g. "building call
  // graph · 3/10") updates the current row's label in place rather than adding a
  // new row; a genuinely new caption appends a fresh step (built once).
  function noteSetup(detail) {
    if (!detail) return;
    const key = detail.replace(/\s*·.*$/, "").trim();
    const last = S.setupSteps[S.setupSteps.length - 1];
    if (last && last.key === key) { last.label = detail; return; }
    const host = $("#tree-setup");
    if (!host) return;
    const el = document.createElement("div");
    el.className = "setup-step";
    el.innerHTML = "<i class='fchip'></i><span class='slbl'></span>";
    host.appendChild(el);
    S.setupSteps.push({
      key, label: detail,
      chip: el.querySelector(".fchip"), lbl: el.querySelector(".slbl"),
    });
  }

  // Build (once) a per-file group: a header chip + a rows host, appended to
  // #ftree-rows in file-arrival order. The header is hidden for single-file runs
  // (renderPipeline toggles it), so those stay visually flat — no regression.
  function buildFileSection(file) {
    if (S.fileSections[file]) return S.fileSections[file];
    const sec = document.createElement("div");
    sec.className = "ffile";
    sec.innerHTML =
      "<div class='ffile-hdr hidden'><span class='ffname'></span><span class='ffcount'></span></div>" +
      "<div class='ffile-rows'></div>";
    $("#ftree-rows").appendChild(sec);
    const ref = {
      el: sec,
      hdr: sec.querySelector(".ffile-hdr"),
      name: sec.querySelector(".ffname"),
      count: sec.querySelector(".ffcount"),
      host: sec.querySelector(".ffile-rows"),
    };
    ref.name.textContent = file ? baseName(file) : "";
    S.fileSections[file] = ref;
    return ref;
  }

  // Create one function row (once) and cache its node refs in S.fnRows.
  function buildRow(name) {
    const el = document.createElement("div");
    el.className = "frow";
    el.innerHTML =
      "<div class='frow-main'>" +
        "<span class='fname'></span>" +
        "<span class='fchips'>" +
          "<span class='fstage'><i class='lbl'>spec</i><i class='fchip'></i></span>" +
          "<span class='fstage'><i class='lbl'>bmc</i><i class='fchip'></i></span>" +
          "<span class='fstage cls hidden'><i class='arr'>→</i><i class='lbl'>classify</i><i class='fchip'></i></span>" +
        "</span>" +
      "</div>" +
      "<div class='fsub hidden'>" +
        "<div class='fcex'></div><div class='fdetail'></div><div class='fiter'></div>" +
      "</div>";
    buildFileSection(S.fnFile[name] || "").host.appendChild(el);
    const stages = el.querySelectorAll(".fstage");
    const ref = {
      el,
      name: el.querySelector(".fname"),
      specChip: stages[0].querySelector(".fchip"),
      bmcChip: stages[1].querySelector(".fchip"),
      clsStage: stages[2],
      classifyChip: stages[2].querySelector(".fchip"),
      sub: el.querySelector(".fsub"),
      cex: el.querySelector(".fcex"),
      detail: el.querySelector(".fdetail"),
      iter: el.querySelector(".fiter"),
    };
    ref.name.textContent = name;
    S.fnRows[name] = ref;
    return ref;
  }

  // Diff-guarded setters — only touch the DOM (and so only restart the ring
  // animation) when the value actually changes.
  function setChip(node, state) {
    const sp = CHIP[state] || CHIP.pending;
    const cls = "fchip " + sp.c;
    if (node.className !== cls) node.className = cls;
    if (node.textContent !== sp.g) node.textContent = sp.g;
  }
  function setText(node, txt) { if (node.textContent !== txt) node.textContent = txt; }
  function setTitle(node, txt) {
    const cur = node.getAttribute("title") || "";
    if (cur === (txt || "")) return;
    if (txt) node.setAttribute("title", txt);
    else node.removeAttribute("title");
  }
  function setHidden(node, hide) {
    if (node.classList.contains("hidden") !== hide) node.classList.toggle("hidden", hide);
  }

  function renderPipeline() {
    if (!$("#ftree-rows")) return;   // scaffold not built yet
    const bmcActive = S.phases.bmc === "start";

    // Directory-sweep file-selection banner — makes "2 of 95 verified" legible
    // (cap + zero-function headers) instead of looking like an early abort.
    const tf = $("#tree-files");
    if (tf) {
      const ts = S.treeSummary;
      setHidden(tf, !ts);
      if (ts) {
        const parts = [ts.files_found + (ts.files_found === 1 ? " file" : " files") + " found"];
        if (ts.cap != null && ts.not_reached > 0) parts.push("cap " + ts.cap);
        if (ts.verified != null) parts.push(ts.verified + " verified");
        if (ts.skipped_no_functions > 0) parts.push(ts.skipped_no_functions + " skipped (no functions)");
        if (ts.not_reached > 0) parts.push(ts.not_reached + " not reached (cap)");
        setText(tf, parts.join(" · "));
      }
    }

    S.fnList.forEach((name) => {
      const r = S.fnRows[name] || buildRow(name);

      // spec — per-function: pending until its spec_fn event arrives, active
      // while generating, done once it settles. Spec-complete collapses every
      // row to done (covers v1, which emits no per-function events, and any
      // function that was pruned to a trivial spec without an event).
      let specState;
      if (S.phases.spec === "complete") specState = "done";
      else {
        const sf = S.specFn[name];
        specState = sf === "done" ? "done" : sf === "active" ? "active" : "pending";
      }
      setChip(r.specChip, specState);

      // bmc — per-function verdict once it settles; in-flight while the phase
      // runs (CBMC checks functions in parallel, so "active" = not yet settled)
      const bf = S.bmcFunctions[name];
      let bmcState;
      if (bf) {
        bmcState = bf.status === "verified" ? "done"
          : bf.status === "counterexample" ? "cex" : "warn";
      } else if (bmcActive) {
        bmcState = "active";
      } else if (S.phases.bmc === "complete") {
        bmcState = "done";
      } else {
        bmcState = "pending";
      }
      setChip(r.bmcChip, bmcState);
      // Explain the ⚠ (unresolved) chip on hover — CBMC reached neither a proof
      // nor a counterexample. Reason rides on the bmc event's detail; cleared
      // for every other state so a chip never keeps a stale tooltip.
      setTitle(r.bmcChip, bmcState === "warn"
        ? "unresolved — " + ((bf && bf.detail) || "BMC could not prove or disprove this function")
        : "");

      // classify — only for functions with counterexamples (original BMC, or a
      // recheck that newly produced them). Chip + nested sub-block.
      const fe = S.functions[name];
      const isClsEvent = fe && fe.phase === "classify";
      const cexN = (bf && bf.n_counterexamples) ||
        (isClsEvent && fe.n_counterexamples) || 0;
      const hasClassify = cexN > 0 || isClsEvent;
      setHidden(r.clsStage, !hasClassify);
      setHidden(r.sub, !hasClassify);
      if (hasClassify) {
        const clsState = S.phases.classify === "complete" ? "done"
          : isClsEvent ? (name === S.curFn ? "active" : "done")
          : "pending";
        setChip(r.classifyChip, clsState);
        setText(r.cex, cexN ? CHIP.cex.g + " " + cexN + " CEX" : "");
        setText(r.detail, S.fnDetail[name] || "");
        setText(r.iter, S.fnIter[name] ? "└ " + S.fnIter[name] : "");
      }

      // Row highlight only during the sequential classify phase (BMC runs many
      // functions at once, so a single "current" row would be misleading there).
      const isActive = name === S.curFn && S.phases.classify === "start";
      const cls = "frow" + (isActive ? " active" : "");
      if (r.el.className !== cls) r.el.className = cls;
    });

    // Per-file group headers: shown only for multi-file (directory) runs, so a
    // single-file run stays flat. Each header carries the file's function count
    // so the next file starting reads as a new file, not a spec regeneration.
    const multiFile = S.fileOrder.length > 1;
    S.fileOrder.forEach((file) => {
      const sec = S.fileSections[file];
      if (!sec) return;
      setHidden(sec.hdr, !multiFile);
      if (multiFile) {
        const n = S.fnList.reduce((a, nm) => a + (S.fnFile[nm] === file ? 1 : 0), 0);
        setText(sec.count, n + (n === 1 ? " fn" : " fns"));
      }
    });

    // setup checklist — fills the silent pre-rows window. Steps are appended
    // live (see noteSetup) from directory-level `prep` events (call-graph build,
    // domain analysis) and the per-file spec sub-notes (parse, analyze). Shown
    // only until the function rows seed, then the per-function chips take over.
    const setup = $("#tree-setup");
    if (setup) {
      const showSetup = S.setupSteps.length > 0;
      setHidden(setup, !showSetup);
      if (showSetup) {
        // The last appended step is in flight only while still in the pre-pass
        // (fnList empty AND spec not yet started). Once spec takes over, every
        // prep step is done and stays visible as a history header.
        const prepDone = S.fnList.length > 0
          || S.phases.spec === "start" || S.phases.spec === "complete";
        S.setupSteps.forEach((step, i) => {
          const isLast = i === S.setupSteps.length - 1;
          setChip(step.chip, (isLast && !prepDone) ? "active" : "done");
          setText(step.lbl, step.label);
        });
      }
    }

    // global spec progress line — shown once rows exist (the checklist owns the
    // pre-rows window); carries the "generating specs · done/total" caption.
    const specLine = $("#tree-spec");
    if (specLine) {
      const show = S.phases.spec === "start" && S.fnList.length > 0;
      setHidden(specLine, !show);
      if (show) setText(specLine, S.phaseDetail.spec || "generating specifications…");
    }

    // classify batch note (3b/3c "re-verifying N functions" — not tied to a row)
    const note = $("#tree-note");
    if (note) {
      const show = S.phases.classify === "start" && !!S.classifyNote;
      setHidden(note, !show);
      if (show) setText(note, S.classifyNote);
    }

    // report footer (global stage, not per-function)
    const rep = $("#tree-report");
    if (rep) {
      const rs = S.phases.report;
      const show = rs === "start" || rs === "complete";
      setHidden(rep, !show);
      if (show) {
        const g = rs === "complete" ? CHIP.done.g : CHIP.active.g;
        const n = S.findings.length;
        setText(rep, g + " Evidence report" + (n ? " · " + n + " finding" + (n === 1 ? "" : "s") : ""));
      }
    }

    // liveness caption in the bottom heartbeat strip (per-function name now
    // lives in the rows, so this is just a phase-level "what's happening")
    const act = $("#activity-text");
    if (act) {
      let label = "starting…";
      if (S.phases.report === "complete") label = "done";
      else if (S.phases.classify === "start") label = S.curFn ? "classifying · " + S.curFn : "classifying…";
      else if (bmcActive) {
        const settled = Object.keys(S.bmcFunctions).length;
        label = "model-checking · " + settled + "/" + (S.fnList.length || "?");
      } else if (S.fnList.length === 0 && S.setupSteps.length) {
        // Still in the setup window (directory pre-pass or per-file parse) —
        // mirror the in-flight checklist step rather than jumping ahead.
        label = S.setupSteps[S.setupSteps.length - 1].label;
      } else if (S.phases.spec === "start") {
        const done = Object.values(S.specFn).filter((s) => s === "done").length;
        label = S.fnList.length
          ? "generating specs · " + done + "/" + S.fnList.length
          : "generating specifications…";
      }
      setText(act, label);
    }
  }

  // Functions CBMC verified clean (the proofs). Each carries its harness source
  // in the streamed event (see bmc_engine progress_cb).
  function verifiedProofs() {
    return Object.values(S.bmcFunctions).filter((f) => f.status === "verified");
  }

  function updateFindingsBadge(n) {
    const badge = $("#findings-count");
    if (n === 0) {
      badge.textContent = "0 confirmed"; badge.className = "tag";
      badge.style.color = ""; badge.style.borderColor = "";
    } else {
      badge.textContent = "▲ " + n + " confirmed";
      badge.className = "tag"; badge.style.color = "#dc2626"; badge.style.borderColor = "#f0b4b4";
    }
  }

  function findingsLegend() {
    const legend = document.createElement("div");
    legend.className = "tierlegend";
    ["confirmed_dynamic", "confirmed_system_entry", "confirmed_bmc", "likely"].forEach((t) => {
      const s = document.createElement("span"); s.textContent = t; legend.appendChild(s);
    });
    return legend;
  }

  function renderFindings() {
    const host = $("#findings");
    const n = S.findings.length;
    const proofs = verifiedProofs();
    const exportWrap = $("#export-wrap");
    if (exportWrap) exportWrap.hidden = n === 0;
    host.innerHTML = "";
    updateFindingsBadge(n);

    if (n > 0) {
      const cards = document.createElement("div");
      cards.id = "findings-cards";
      S.findings.forEach((b) => {
        const card = findingCard(b);
        cards.appendChild(card);
        setupReasonClamp(card);
      });
      host.appendChild(cards);
      host.appendChild(findingsLegend());
    }

    renderProofs(host, proofs, n);
  }

  // Append just the newest finding's card instead of rebuilding the whole list.
  // The `finding` SSE event fires once per confirmed bug; a full re-render each
  // time is O(N^2) plus a forced reflow (setupReasonClamp) per existing card.
  // Proofs + legend don't change on a finding event, so they're left in place;
  // falls back to a full render on the 0→1 transition (no cards container yet).
  function appendOneFinding(b) {
    const cards = $("#findings-cards");
    if (!cards) { renderFindings(); return; }
    const card = findingCard(b);
    cards.appendChild(card);
    setupReasonClamp(card);
    updateFindingsBadge(S.findings.length);
  }

  // Proofs block, always shown: when there are zero findings it is the panel's
  // main content; otherwise it sits below the findings.
  function renderProofs(host, proofs, nFindings) {
    if (!proofs.length) {
      if (nFindings === 0) {
        host.innerHTML = "<div class='empty'>No findings and no completed proofs yet.</div>";
      }
      return;
    }
    const sec = document.createElement("div");
    sec.className = "proofs";
    const head = document.createElement("div");
    head.className = "proofs-head";
    head.innerHTML = "<span class='t'>proofs</span>" +
      "<span class='tag'>✓ " + proofs.length + " verified</span>";
    sec.appendChild(head);
    proofs.slice().sort((a, b) => a.name.localeCompare(b.name)).forEach((f) => {
      sec.appendChild(proofRow(f));
    });
    host.appendChild(sec);
  }

  function proofRow(f) {
    const row = document.createElement("div");
    row.className = "proof";
    const hasHarness = !!f.harness;
    row.innerHTML =
      "<span class='ic'>✓</span>" +
      "<span class='fn'>" + esc(f.name) + "</span>" +
      "<span class='tag ok'>verified · unwind 4</span>" +
      (hasHarness ? "<span class='view'>view harness ›</span>" : "");
    if (hasHarness) {
      row.style.cursor = "pointer";
      row.title = "View the CBMC harness that proved " + f.name;
      row.addEventListener("click", () => showHarness(f.name));
    }
    return row;
  }

  // Render the proof harness for `fnName` into the left source panel.
  function showHarness(fnName) {
    const f = S.bmcFunctions[fnName] || S.functions[fnName];
    if (!f || !f.harness) return;
    S.fileLoaded = "";                       // harness isn't a repo file; don't dedupe against it
    const lang = f.harness_lang === "rust" ? "rust" : f.harness_lang === "java" ? "java" : "c";
    $("#src-name").textContent = "harness · " + fnName;
    $("#src-lang").textContent = LANG_LABEL[lang] || "C";
    renderCodeLines($("#src"), f.harness, lang);
    $("#src-meta").textContent = f.harness.split("\n").length + " lines";
  }

  // Stable identity for a finding, so a replayed `finding` event (EventSource
  // auto-reconnect) doesn't add a duplicate card / inflate the confirmed count.
  function findingKey(b) {
    return [b.file || "", b.function || "", b.bug_type || "", b.violated_property || "",
            (b.call_chain || []).join(">")].join("|");
  }

  // Append a finding unless one with the same identity is already present.
  // Returns true if it was added (so the caller knows whether to re-render).
  function addFinding(b) {
    const k = findingKey(b);
    if (S.findings.some((f) => findingKey(f) === k)) return false;
    S.findings.push(b);
    return true;
  }

  function findingCard(b) {
    const card = document.createElement("div");
    card.className = "finding";
    const tier = b.confidence || "likely";
    const chain = (b.call_chain && b.call_chain.length) ? b.call_chain.join(" → ") : "";
    card.innerHTML =
      "<div class='top'><span class='fn'>" + esc(b.function) + "</span>" +
      "<span class='tierbadge tier-" + esc(tier) + "'>" + esc(tier) + "</span>" +
      (b.bug_type ? "<span class='typebadge'>" + esc(b.bug_type.replace(/_/g, " ")) + "</span>" : "") +
      "</div><div class='body'>" +
      (b.violated_property ? "<div class='prop'>violates <b>" + esc(b.violated_property) + "</b></div>" : "") +
      (chain ? "<div class='chain'>via " + esc(chain) + "</div>" : "") +
      (b.reasoning ? "<div class='reason clamped'>" + esc(b.reasoning) + "</div>" : "") +
      "</div>";
    if (b.file) {
      card.style.cursor = "pointer";
      card.title = "View " + b.file;
      card.addEventListener("click", () => {
        const p = resolveRepoPath(b.file);
        $("#src-name").textContent = baseName(p);
        loadSource(p);
      });
    }
    return card;
  }

  // Add a "show more / show less" toggle to a finding card whose reasoning is
  // clamped AND actually overflows. Must run after the card is in the DOM, since
  // the overflow check (scrollHeight vs clientHeight) needs computed layout.
  function setupReasonClamp(card) {
    const reason = card.querySelector(".reason");
    if (!reason || reason.scrollHeight <= reason.clientHeight) return;
    const btn = document.createElement("button");
    btn.className = "more-btn";
    btn.type = "button";
    btn.textContent = "show more ▾";
    btn.addEventListener("click", (e) => {
      e.stopPropagation();   // don't trigger the card's "view source" handler
      const clamped = reason.classList.toggle("clamped");
      btn.textContent = clamped ? "show more ▾" : "show less ▴";
    });
    reason.insertAdjacentElement("afterend", btn);
  }

  // ---- findings export (CSV / print-to-PDF) -------------------------
  function exportBaseName() {
    const repo = (S.repo && S.repo.repo) ? baseName(S.repo.repo) : "";
    return "findings-" + (repo || S.runId || "aprover");
  }

  function csvCell(v) {
    const s = String(v == null ? "" : v);
    return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  function exportFindingsCSV() {
    const cols = ["function", "bug_type", "violated_property", "confidence",
      "call_chain", "file", "reasoning"];
    const rows = [cols.join(",")];
    S.findings.forEach((b) => {
      rows.push(cols.map((c) => {
        if (c === "call_chain") return csvCell((b.call_chain || []).join(" -> "));
        return csvCell(b[c]);
      }).join(","));
    });
    const blob = new Blob(["﻿" + rows.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = exportBaseName() + ".csv";
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  }

  function exportFindingsPDF() {
    const repo = (S.repo && S.repo.repo) || "project";
    const title = "AProver findings — " + esc(repo);
    const cards = S.findings.map((b) => {
      const tier = b.confidence || "likely";
      const chain = (b.call_chain && b.call_chain.length) ? b.call_chain.join(" → ") : "";
      return "<div class='f'>" +
        "<div class='top'><span class='fn'>" + esc(b.function) + "</span>" +
        "<span class='tier'>" + esc(tier) + "</span>" +
        (b.bug_type ? "<span class='ty'>" + esc(b.bug_type.replace(/_/g, " ")) + "</span>" : "") +
        (b.file ? "<span class='file'>" + esc(b.file) + "</span>" : "") +
        "</div>" +
        (b.violated_property ? "<div class='prop'>violates <b>" + esc(b.violated_property) + "</b></div>" : "") +
        (chain ? "<div class='chain'>via " + esc(chain) + "</div>" : "") +
        (b.reasoning ? "<div class='reason'>" + esc(b.reasoning) + "</div>" : "") +
        "</div>";
    }).join("");
    const css =
      "*{box-sizing:border-box;}" +
      "body{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#15181d;margin:32px;}" +
      "h1{font-size:18px;margin:0 0 4px;}" +
      ".meta{color:#6b7280;font-size:12px;margin-bottom:20px;}" +
      ".f{border:1px solid #e1e4e9;border-radius:8px;padding:12px 14px;margin-bottom:12px;page-break-inside:avoid;}" +
      ".top{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px;}" +
      ".fn{font-weight:700;font-family:ui-monospace,monospace;}" +
      ".tier,.ty,.file{font-size:11px;padding:2px 7px;border-radius:999px;border:1px solid #e1e4e9;color:#4b5563;}" +
      ".tier{background:#fef2f2;border-color:#f0b4b4;color:#b91c1c;}" +
      ".file{margin-left:auto;font-family:ui-monospace,monospace;}" +
      ".prop{font-size:12.5px;margin:3px 0;} .prop b{font-family:ui-monospace,monospace;}" +
      ".chain{font-size:12px;color:#4b5563;margin:3px 0;font-family:ui-monospace,monospace;}" +
      ".reason{font-size:12.5px;color:#374151;margin-top:6px;white-space:pre-wrap;}" +
      "@media print{body{margin:0;}}";
    const html = "<!doctype html><html><head><meta charset='utf-8'><title>" + title +
      "</title><style>" + css + "</style></head><body>" +
      "<h1>" + title + "</h1>" +
      "<div class='meta'>" + S.findings.length + " confirmed · " +
      esc(S.runId || "") + " · " + esc(new Date().toISOString().slice(0, 10)) + "</div>" +
      cards + "</body></html>";
    const w = window.open("", "_blank");
    if (!w) { toast("Allow pop-ups to export PDF"); return; }
    w.document.write(html);
    w.document.close();
    w.focus();
    w.print();
  }

  function initExport() {
    const btn = $("#export-btn");
    const menu = $("#export-menu");
    if (!btn || !menu) return;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      menu.classList.toggle("hidden");
    });
    menu.addEventListener("click", (e) => {
      const item = e.target.closest("[data-export]");
      if (!item) return;
      menu.classList.add("hidden");
      if (item.dataset.export === "csv") exportFindingsCSV();
      else exportFindingsPDF();
    });
    document.addEventListener("click", () => menu.classList.add("hidden"));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") menu.classList.add("hidden");
    });
  }

  // The pipeline reports a finding's file by basename only (e.g. "main.c"),
  // but /api/file resolves paths relative to the repo root. Map the basename
  // to the full repo-relative path using the clone/upload listing.
  function resolveRepoPath(file) {
    if (!file) return file;
    const files = (S.repo && S.repo.files) || [];
    const exact = files.find((f) => f.path === file);
    if (exact) return exact.path;
    const tail = files.filter((f) => f.path === file || f.path.endsWith("/" + file));
    if (tail.length) return tail[0].path; // first match (basenames are usually unique)
    return file; // best effort
  }

  function appendLog(line) {
    const box = $("#logbox");
    box.textContent += (box.textContent ? "\n" : "") + line;
    box.scrollTop = box.scrollHeight;
  }

  function finishRun(d) {
    stopTimer();
    S.eta = null;
    $("#eta").textContent = "—";
    $("#activity").classList.add("hidden");
    if (S.es) { S.es.close(); S.es = null; }
    const status = d.status || "done";
    if (status === "done") {
      setPill("idle", "complete");
      // mark all started phases as complete-looking
      ["spec", "bmc", "classify", "report"].forEach((k) => { if (!S.phases[k]) S.phases[k] = "complete"; });
      S.phases.report = "complete";
      renderPipeline();
      renderFindings();   // ensure proofs paint after a refresh/replay
      $("#pause-btn").disabled = true; $("#cancel-btn").disabled = true;
    } else if (status === "halted") {
      showRecovery(d);
    } else { // error
      setPill("warn", "error");
      $("#pause-btn").disabled = true; $("#cancel-btn").disabled = true;
      showRecovery(d, true);
    }
  }

  function showRecovery(d, isError) {
    setPill("warn", isError ? "error" : "halted · needs attention");
    $("#pause-btn").disabled = true; $("#cancel-btn").disabled = true;
    const banner = $("#recovery-banner");
    banner.classList.remove("hidden");
    const reason = d.halt_reason || "";
    const failed = Object.values(S.functions).find((f) => f.status !== "verified");
    let title = "Run halted.";
    if (reason === "budget") title = "Run halted — budget cap reached.";
    else if (reason === "timeout") title = "Run halted — solver timed out.";
    else if (isError) title = "Run failed.";
    $("#recovery-title").textContent = title;
    $("#recovery-sub").textContent = (d.error ? d.error : "Completed work and confirmed findings are preserved.") +
      (S.findings.length ? "  " + S.findings.length + " finding(s) kept." : "");
    const acts = $("#recovery-acts");
    acts.innerHTML = "";
    if (failed) {
      acts.appendChild(mkBtn("↻ retry " + failed.name, "btn sm", () => retry({ mode: "retry_function", function: failed.name })));
      acts.appendChild(mkBtn("retry · scaled", "btn ghost sm", () => retry({ mode: "retry_function", function: failed.name, scale_down: true })));
    }
    acts.appendChild(mkBtn("→ continue", "btn ghost sm", () => retry({ mode: "continue" })));
    acts.appendChild(mkBtn("re-run all", "btn ghost sm", () => retry({ mode: "rerun_all" })));
  }

  function mkBtn(label, cls, fn) {
    const b = document.createElement("button");
    b.className = cls; b.textContent = label; b.addEventListener("click", fn);
    return b;
  }

  async function retry(opts) {
    if (!S.runId) return;
    resetRunView();
    try {
      const data = await api("POST", "/api/run/" + S.runId + "/retry", opts, { includeKey: true });
      S.runId = data.run_id;
      setRunInUrl(S.runId);
      startTimer();
      openStream(data.run_id);
    } catch (err) {
      setPill("warn", "failed");
      toast("Retry failed: " + err.message);
    }
  }

  function initRunControls() {
    $("#pause-btn").addEventListener("click", async () => {
      if (!S.runId) return;
      const btn = $("#pause-btn");
      const paused = btn.dataset.paused === "1";
      try {
        if (paused) {
          await api("POST", "/api/run/" + S.runId + "/resume");
          btn.dataset.paused = ""; btn.textContent = "❚❚ pause"; setPill("live", "verifying");
        } else {
          await api("POST", "/api/run/" + S.runId + "/pause");
          btn.dataset.paused = "1"; btn.textContent = "▶ resume"; setPill("warn", "paused");
        }
      } catch (err) { toast(err.message); }
    });
    $("#cancel-btn").addEventListener("click", async () => {
      if (!S.runId) return;
      try { await api("POST", "/api/run/" + S.runId + "/cancel"); setPill("warn", "stopping…"); }
      catch (err) { toast(err.message); }
    });
  }

  // elapsed timer
  function startTimer() {
    S.startMs = Date.now();
    stopTimer();
    S.timer = setInterval(() => {
      $("#elapsed").textContent = fmtDuration((Date.now() - S.startMs) / 1000);
      // Count the server's last ETA down locally between updates.
      if (S.eta && S.eta.rem != null) {
        const rem = S.eta.rem - (Date.now() - S.eta.at) / 1000;
        $("#eta").textContent = "~" + fmtDuration(rem);
      } else {
        $("#eta").textContent = "estimating…";
      }
    }, 100);
  }
  function stopTimer() { if (S.timer) { clearInterval(S.timer); S.timer = null; } }

  // source view
  const LANG_LABEL = { c: "C", rust: "Rust", java: "Java" };

  // Split highlight.js output HTML into one fragment per source line, keeping
  // span nesting valid across newlines: on each '\n' the currently-open
  // highlight spans are closed for that line and reopened on the next. This
  // preserves correct tokenization of C block comments that span lines, which
  // per-line highlighting would otherwise break. Only real '<' starts a tag;
  // '&...;' entities pass through untouched.
  function splitHighlightLines(html) {
    const out = [];
    const open = [];          // opener tags of currently-open spans, in order
    let cur = "";
    const flushClose = () => { for (let k = open.length - 1; k >= 0; k--) cur += "</span>"; };
    const reopen = () => { for (let k = 0; k < open.length; k++) cur += open[k]; };
    let i = 0;
    while (i < html.length) {
      const c = html[i];
      if (c === "<") {
        const close = html.indexOf(">", i);
        if (close === -1) { cur += html.slice(i); break; }
        const tag = html.slice(i, close + 1);
        if (tag[1] === "/") { open.pop(); cur += tag; }
        else { open.push(tag); cur += tag; }
        i = close + 1;
      } else if (c === "\n") {
        flushClose();
        cur += "\n";
        out.push(cur);
        cur = "";
        reopen();
        i++;
      } else {
        cur += c;
        i++;
      }
    }
    flushClose();
    out.push(cur);
    return out;
  }

  // Render `text` as numbered code lines into `host` (shared by repo source and
  // proof harnesses). `lang` selects the highlight.js grammar; if the library
  // or grammar is unavailable it degrades to escaped plain text.
  function renderCodeLines(host, text, lang) {
    const src = String(text == null ? "" : text);
    host.innerHTML = "";
    const lines = src.split("\n");
    const draw = (lineHtml, i) => {
      const div = document.createElement("div");
      div.className = "ln";
      div.innerHTML = "<span class='no'>" + (i + 1) + "</span><code class='hljs'>" + lineHtml + "</code>";
      host.appendChild(div);
    };
    if (typeof hljs === "undefined" || !lang || !hljs.getLanguage(lang)) {
      lines.forEach((ln, i) => draw(esc(ln), i));
      return lines.length;
    }
    const frag = splitHighlightLines(
      hljs.highlight(src, { language: lang, ignoreIllegals: true }).value);
    frag.forEach((lineHtml, i) => draw(lineHtml, i));
    return frag.length;
  }

  async function loadSource(path) {
    if (!path || S.fileLoaded === path) return;
    S.fileLoaded = path;
    const lang = langForName(baseName(path));
    $("#src-lang").textContent = LANG_LABEL[lang] || "C";
    const host = $("#src");
    host.innerHTML = "<div class='empty' style='padding:14px 16px;'>loading " + esc(baseName(path)) + "…</div>";
    try {
      // Local mode: read straight from the File System Access handle the browser
      // already holds. Only in-scope files were uploaded, but the run-tree lists
      // the whole folder — so /api/file would 404 on out-of-scope files that
      // exist locally. Reading the handle works for any local code file (and
      // keeps unselected files off the server). Falls back to /api/file for
      // remote repos and post-refresh (handles gone; tree is the uploaded repo).
      const local = S.local && S.localFiles.find((f) => f.path === path);
      let content;
      if (local) {
        content = await (await local.handle.getFile()).text();
      } else {
        const data = await api("GET", "/api/file?repo=" + encodeURIComponent(S.repo.repo) + "&path=" + encodeURIComponent(path));
        content = data.content;
      }
      $("#src-meta").textContent = renderCodeLines(host, content, lang) + " lines";
    } catch (err) {
      host.innerHTML = "<div class='empty' style='padding:14px 16px;'>could not load source: " + esc(err.message) + "</div>";
    }
  }

  function baseName(p) { return (p || "").split("/").pop(); }

  // ---- run-view file browser ---------------------------------------
  // The "📁 files" button toggles a clickable tree (#run-tree) over the source
  // panel. It reuses S.tree (populated by enterScope / walkDir / restoreRun);
  // each node's repo-relative `path` feeds loadSource directly.
  function initRunBrowse() {
    const btn = $("#src-browse-btn");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const host = $("#run-tree");
      const show = host.classList.contains("hidden");
      host.classList.toggle("hidden", !show);
      if (show) renderRunTree();
    });
  }

  function renderRunTree() {
    const host = $("#run-tree");
    host.innerHTML = "";
    if (!S.tree) {
      host.innerHTML = "<div class='empty'>no file tree available</div>";
      return;
    }
    const rows = [];
    runTreeRows(S.tree, 0, rows, true);   // skip the root dir node itself
    rows.forEach((r) => host.appendChild(r));
  }

  function runTreeRows(node, depth, rows, isRoot) {
    if (!isRoot) rows.push(runTreeRow(node, depth));
    if (node.type === "dir" && node.children) {
      const d = isRoot ? 0 : depth + 1;
      node.children.forEach((ch) => runTreeRows(ch, d, rows, false));
    }
  }

  function runTreeRow(node, depth) {
    const row = document.createElement("div");
    row.className = "trow";
    const isDir = node.type === "dir";
    // In local mode the tree lists non-code files for context; only code files
    // were uploaded, so leave the rest non-clickable.
    const clickable = !isDir && (!S.local || node.code);
    row.style.paddingLeft = (14 + depth * 16) + "px";
    if (clickable && S.fileLoaded === node.path) row.classList.add("active");
    const ico = isDir ? "📁" : "📄";
    row.innerHTML =
      "<span class='ico'>" + ico + "</span>" +
      "<span class='nm'>" + esc(node.name) + (isDir ? "/" : "") + "</span>";
    if (clickable) {
      row.addEventListener("click", () => {
        $("#src-name").textContent = node.name;
        loadSource(node.path);
        renderRunTree();   // refresh the active-file highlight
      });
    } else {
      row.style.cursor = "default";
      if (!isDir) row.style.opacity = ".55";
    }
    return row;
  }

  // ==================================================================
  // SETTINGS MODAL — chooser → per-provider page
  // ==================================================================
  let _editingProvider = null;   // provider id being edited on the detail page

  function showChooser() {
    _editingProvider = null;
    const cfg = loadCfg();
    const host = $("#settings-chooser");
    host.innerHTML = "";
    PROVIDER_ORDER.forEach((id) => {
      const m = PROVIDER_META[id];
      const saved = cfg.provider === id && !!cfg.key;
      const row = document.createElement("button");
      row.className = "prov-row" + (cfg.provider === id ? " current" : "");
      row.innerHTML =
        "<span class='radio'></span>" +
        "<span class='pr-text'><span class='pr-name'>" + esc(m.label) + "</span>" +
        "<span class='pr-desc'>" + esc(m.desc) + "</span></span>" +
        (saved ? "<span class='pr-on'>✓ configured</span>" : "") +
        "<span class='pr-chev'>›</span>";
      row.addEventListener("click", () => openProviderPage(id));
      host.appendChild(row);
    });
    $("#set-title").textContent = "Settings";
    $("#set-subtitle").textContent = "choose a provider · keys stay in your browser";
    $("#settings-chooser").classList.remove("hidden");
    $("#settings-detail").classList.add("hidden");
    $("#set-save").classList.add("hidden");
  }

  function openProviderPage(id) {
    _editingProvider = id;
    const m = PROVIDER_META[id], def = PROVIDERS[id], cfg = loadCfg();
    const sameAsSaved = cfg.provider === id;
    $("#set-title").textContent = m.label;
    $("#set-subtitle").textContent = m.desc;
    $("#set-key").value = sameAsSaved ? (cfg.key || "") : "";
    $("#set-key").placeholder = m.keyPlaceholder;
    $("#set-keylink").href = m.keyLink;
    // model — preset dropdown (+ Custom… reveals a free-text box)
    const curModel = sameAsSaved && cfg.model ? cfg.model : def.model;
    const presets = presetsFor(id);
    const sel = $("#set-model-select");
    sel.innerHTML = "";
    presets.forEach((p) => {
      const o = document.createElement("option");
      o.value = p.id;
      const priced = p.free ? " · free" : (p.input != null && p.output != null
        ? " · $" + p.input + "/$" + p.output + " per Mtok" : " · no estimate");
      o.textContent = p.label + priced;
      sel.appendChild(o);
    });
    if (!m.modelFixed) {
      const o = document.createElement("option");
      o.value = CUSTOM_OPT;
      o.textContent = "Custom…";
      sel.appendChild(o);
    }
    // Decide selection: saved custom → Custom; known preset → that preset; else
    // the provider default (or first preset).
    const savedCustom = sameAsSaved && cfg.model_is_custom && !m.modelFixed;
    const matchesPreset = isPresetModel(id, curModel);
    if (savedCustom || (!matchesPreset && !m.modelFixed)) {
      sel.value = CUSTOM_OPT;
    } else if (matchesPreset) {
      sel.value = curModel;
    } else {
      sel.value = (presets[0] && presets[0].id) || curModel;
    }
    sel.disabled = !!m.modelFixed;
    const custom = sel.value === CUSTOM_OPT;
    $("#set-model").classList.toggle("hidden", !custom);
    $("#set-model").value = custom ? curModel : "";
    $("#set-model-hint").textContent = modelHint(id, custom);
    // base url
    const wrap = $("#set-baseurl-wrap"), inp = $("#set-baseurl");
    if (m.baseUrl === "none") {
      wrap.classList.add("hidden");
    } else {
      wrap.classList.remove("hidden");
      inp.value = (sameAsSaved && cfg.base_url) ? cfg.base_url : def.base_url;
      inp.readOnly = m.baseUrl === "fixed";
      $("#set-baseurl-hint").textContent = m.baseUrl === "fixed"
        ? "fixed endpoint for this provider" : "leave blank for the provider default";
    }
    $("#set-budget").value = cfg.budget_cap != null ? String(cfg.budget_cap) : "";
    $("#set-maxfiles").value = cfg.max_files != null ? String(cfg.max_files) : "";
    $("#set-error").classList.add("hidden");
    $("#settings-chooser").classList.add("hidden");
    $("#settings-detail").classList.remove("hidden");
    $("#set-save").classList.remove("hidden");
  }

  function openSettings() {
    showChooser();
    $("#settings-overlay").classList.remove("hidden");
  }
  function closeSettings() { $("#settings-overlay").classList.add("hidden"); }

  function saveProvider() {
    const id = _editingProvider || "anthropic";
    const m = PROVIDER_META[id], def = PROVIDERS[id];
    const key = $("#set-key").value.trim();
    const sel = $("#set-model-select");
    const isCustom = !m.modelFixed && sel.value === CUSTOM_OPT;
    const model = isCustom
      ? ($("#set-model").value.trim() || def.model)
      : (sel.value || def.model);
    const baseUrl = m.baseUrl === "none" ? "" : ($("#set-baseurl").value.trim() || def.base_url);
    const budgetRaw = $("#set-budget").value.trim();
    const err = $("#set-error");
    if (!key) { err.textContent = "An API key is required."; err.classList.remove("hidden"); return; }
    if (isCustom && !$("#set-model").value.trim()) {
      err.textContent = "Enter a custom model id, or pick a preset."; err.classList.remove("hidden"); return;
    }
    let budget = null;
    if (budgetRaw) {
      const n = Number(budgetRaw);
      if (!isFinite(n) || n <= 0) { err.textContent = "Budget cap must be a positive number."; err.classList.remove("hidden"); return; }
      budget = n;
    }
    const maxFilesRaw = $("#set-maxfiles").value.trim();
    let maxFiles = null;
    if (maxFilesRaw) {
      const n = Number(maxFilesRaw);
      if (!isFinite(n) || n <= 0 || Math.floor(n) !== n) { err.textContent = "Max files must be a positive whole number."; err.classList.remove("hidden"); return; }
      maxFiles = n;
    }
    saveCfg({ provider: id, backend: def.backend, model, base_url: baseUrl, key, model_is_custom: isCustom, budget_cap: budget, max_files: maxFiles });
    closeSettings();
    refreshModelTag();
    updateScopeSummary();
    toast("Saved — using " + m.label + ".");
  }

  function initSettings() {
    $$("[data-open-settings]").forEach((b) => b.addEventListener("click", openSettings));
    $$("[data-close-settings]").forEach((b) => b.addEventListener("click", closeSettings));
    $("#set-back").addEventListener("click", showChooser);
    $("#set-model-select").addEventListener("change", () => {
      const id = _editingProvider || "anthropic";
      const custom = $("#set-model-select").value === CUSTOM_OPT;
      $("#set-model").classList.toggle("hidden", !custom);
      if (custom) $("#set-model").focus();
      $("#set-model-hint").textContent = modelHint(id, custom);
    });
    // Re-price a custom OpenRouter id live as the user types it.
    $("#set-model").addEventListener("input", () => {
      const id = _editingProvider || "anthropic";
      $("#set-model-hint").textContent = modelHint(id, true);
    });
    $("#set-save").addEventListener("click", saveProvider);
  }

  // ==================================================================
  // RUN SETTINGS  (friendly controls for the full CLI knob set)
  // ==================================================================
  // Persisted apart from the LLM config so verification preferences stick across
  // runs. Each field maps 1:1 to a web.options group/field; buildOptions()
  // serializes the panel into the request body's `options`, sending ONLY values
  // that differ from the default (so "untouched" stays "server default" — the
  // Config/CLI default — and the body stays small). Resource knobs are re-clamped
  // server-side regardless of the stepper bounds here.
  const OPT_KEY = "aprover_run_options";

  // type: bool | int | select | segment | text.  group: web.options group.
  // adv: tuck under the section's "advanced" disclosure.  dep: key whose
  // truthiness gates this control (greyed out when the parent is off).
  const RUN_SECTIONS = [
    {
      key: "run_mode", title: "Run mode",
      blurb: "a single pass, or autonomous multi-round convergence",
      fields: [
        { k: "run_mode", type: "segment", def: "verify", label: "Mode",
          opts: [["verify", "verify"], ["autonomous", "autonomous"]],
          hint: "Autonomous re-runs the sweep across rounds until it converges (directory scope)." },
        { k: "max_rounds", group: "autonomous", type: "int", def: 3, min: 1, max: 10, label: "Max rounds",
          dep: "run_mode", depVal: "autonomous", hint: "Cap on autonomous rounds before it stops." },
      ],
    },
    {
      key: "depth", title: "Verification depth",
      blurb: "how hard the solver works per function",
      fields: [
        { k: "cbmc_unwind", group: "depth", type: "int", label: "Loop unwind", def: 4, min: 1, max: 16,
          hint: "How deep loops unroll. Higher finds deeper bugs but is slower." },
        { k: "cbmc_timeout", group: "depth", type: "int", label: "CBMC timeout", unit: "s", def: 120, min: 5, max: 120,
          hint: "Per-check solver time limit." },
        { k: "cbmc_object_bits", group: "depth", type: "select", def: "auto", label: "Object bits", adv: true,
          opts: [["auto", "auto"], ["8", "8"], ["12", "12"], ["16", "16"]],
          hint: "Address-space bits. Raise for state-heavy parsers (libxml2, ASN.1)." },
        { k: "per_function_time_budget_s", group: "depth", type: "int", label: "Per-function budget", unit: "s",
          def: 1200, min: 30, max: 1800, adv: true, hint: "Total solver budget per function across all phases." },
        { k: "max_refinement_iters", group: "depth", type: "int", label: "Refinement rounds", def: 5, min: 0, max: 8,
          adv: true, hint: "Spec-refinement rounds per function." },
        { k: "max_spec_retries", group: "depth", type: "int", label: "Spec retries", def: 3, min: 0, max: 10,
          adv: true, hint: "Retries when spec generation fails." },
        { k: "dedup_max_per_type", group: "depth", type: "int", label: "Counterexamples / type", def: 3, min: 1, max: 8,
          adv: true, hint: "How many counterexamples to keep per property type." },
      ],
    },
    {
      key: "ai_layers", title: "AI layers",
      blurb: "extra LLM stages that audit and refine findings (CLI defaults)",
      fields: [
        { k: "enable_realism_check", group: "ai_layers", type: "bool", def: true, label: "Realism check",
          hint: "An LLM audits each finding to cut false positives (extra cost)." },
        { k: "enable_realism_thinking", group: "ai_layers", type: "bool", def: false, label: "Realism extended thinking",
          dep: "enable_realism_check", adv: true, hint: "Slower, higher-quality realism reasoning." },
        { k: "enable_realism_tools", group: "ai_layers", type: "bool", def: true, label: "Realism tools",
          dep: "enable_realism_check", adv: true, hint: "Tool-use augmentation for the realism check (source lookups)." },
        { k: "enable_dynamic_validation", group: "ai_layers", type: "bool", def: true, label: "Dynamic validation",
          hint: "Compile + run a reproducer to confirm crashes (needs a build env)." },
        { k: "enable_reproducer_agent", group: "ai_layers", type: "bool", def: true, label: "Reproducer agent",
          dep: "enable_dynamic_validation", adv: true, hint: "Tool-using compile → run → fix reproducer." },
        { k: "enable_flag_selection", group: "ai_layers", type: "bool", def: true, label: "Flag selection", adv: true,
          hint: "An LLM picks per-function CBMC flags (overflow / pointer checks)." },
        { k: "enable_feedback_loop", group: "ai_layers", type: "bool", def: true, label: "Feedback loop", adv: true,
          hint: "Distill false-positive patterns into learned clauses." },
        { k: "enable_spec_refiner", group: "ai_layers", type: "bool", def: true, label: "Spec refiner", adv: true,
          hint: "Realism-driven in-sweep spec refinement." },
        { k: "enable_spec_gen_tools", group: "ai_layers", type: "bool", def: true, label: "Spec-gen tools", adv: true,
          hint: "Bounded tool-use during spec generation (v2.2)." },
        { k: "enable_inlining_advisor", group: "ai_layers", type: "bool", def: true, label: "Inlining advisor", adv: true,
          hint: "An LLM promotes small callees to inline to cut stub false positives." },
        { k: "enable_global_invariants", group: "ai_layers", type: "bool", def: true, label: "Global invariants", adv: true,
          hint: "Derive g != NULL / g == K from the source's own global writes." },
        { k: "enable_soundness_gate", group: "ai_layers", type: "bool", def: false, label: "Soundness gate", adv: true,
          hint: "Block refinements not guaranteed by every caller." },
        { k: "soundness_gate_fail_closed", group: "ai_layers", type: "bool", def: false, label: "↳ fail-closed",
          dep: "enable_soundness_gate", adv: true, hint: "Keep the counterexample unless proven sound." },
      ],
    },
    {
      key: "threat", title: "Threat model",
      blurb: "what the verifier treats as attacker-controlled",
      fields: [
        { k: "threat_model", group: "threat", type: "segment", def: "security", label: "Model",
          opts: [["security", "security"], ["safety", "safety"], ["functional", "functional"]],
          hint: "Shapes CBMC baseline checks, spec prompts, and realism context." },
        { k: "threat_model_context", group: "threat", type: "text", def: "", label: "Trust-boundary note",
          hint: "Which inputs are attacker-controlled vs caller/hardware-guaranteed, in prose." },
      ],
    },
    {
      key: "harness", title: "Harness & input modeling",
      blurb: "how the harness models inputs and callees",
      fields: [
        { k: "cbmc_real_libc", group: "harness", type: "bool", def: false, label: "Real libc", langs: ["c"],
          hint: "#include the source and let CBMC preprocess — for glibc-using code (curl, OpenSSL)." },
        { k: "raw_bytes", group: "harness", type: "bool", def: false, label: "Raw bytes", langs: ["c"],
          hint: "Treat char* as raw byte buffers (wire-format parsers), not NUL-terminated strings." },
        { k: "scale_down", group: "harness", type: "bool", def: false, label: "Scale down",
          hint: "Bound ML / numerics parametric sizes (B, T, C, …) so kernels stay tractable." },
        { k: "scale_down_size", group: "harness", type: "int", def: 4, min: 1, max: 64, label: "↳ scale size",
          dep: "scale_down", adv: true, hint: "Upper bound applied to parametric-size parameters." },
        { k: "safety_only", group: "harness", type: "bool", def: false, label: "Safety-only specs", adv: true,
          hint: "Restrict postconditions to memory safety + bounds + NaN/Inf-freedom." },
        { k: "lite_mode", group: "harness", type: "bool", def: false, label: "Lite mode", adv: true,
          hint: "Skip LLM spec generation; rely on CBMC's built-in checks. Cheapest." },
        { k: "strict_dsl", group: "harness", type: "bool", def: false, label: "Strict DSL", langs: ["c"], adv: true,
          hint: "Force single-C-boolean specs (no prose) — for bounty / CVE work." },
        { k: "enable_string_copy_source_modeling", group: "harness", type: "bool", def: true, langs: ["c"], adv: true,
          label: "String-copy source modeling", hint: "Model unbounded copy sinks so overflows are caught." },
        { k: "infer_field_validity", group: "harness", type: "bool", def: false, langs: ["c"], adv: true,
          label: "Infer field validity", hint: "Disjunctive init for primitive-pointer struct fields (ML kernels)." },
        { k: "infer_struct_field_validity", group: "harness", type: "bool", def: false, langs: ["c"], adv: true,
          label: "Infer struct-field validity", hint: "Same for struct/union-pointer fields (disciplined-NULL code)." },
        { k: "infer_array_param_bounds", group: "harness", type: "bool", def: false, langs: ["c"], adv: true,
          label: "Infer array-param bounds", hint: "Size array parameters from body subscripts." },
        { k: "infer_array_param_bounds_max", group: "harness", type: "int", def: 64, min: 1, max: 256, langs: ["c"],
          adv: true, dep: "infer_array_param_bounds", label: "↳ max bound", hint: "Cap on inferred array sizes." },
        { k: "cbmc_defines", group: "harness", type: "defines", def: "", langs: ["c"], adv: true,
          label: "Preprocessor defines", hint: "Space- or comma-separated NAME or NAME=VALUE passed to CBMC's -D." },
      ],
    },
    {
      key: "spec_c", title: "Spec synthesis",
      blurb: "arithmetic semantics (single-file C)",
      fields: [
        { k: "math_ints", group: "spec_mode", type: "bool", def: false, label: "Mathematical integers", langs: ["c"],
          hint: "Assume signed arithmetic does not overflow (textbook semantics)." },
      ],
    },
    {
      key: "agentic", title: "Agentic & LLM routing",
      blurb: "tool-using agents and per-role model overrides",
      fields: [
        { k: "enable_agentic_harness_repair", group: "agentic", type: "bool", def: true, label: "Agentic harness repair",
          adv: true, hint: "On a harness build error, rebuild it with a code-reading agent." },
        { k: "enable_agentic_harness", group: "agentic", type: "bool", def: false, label: "Agentic harness gen", adv: true,
          hint: "Let an LLM build the harness from the start (falls back to deterministic)." },
        { k: "enable_split_spec_gen", group: "agentic", type: "bool", def: false, label: "Split spec generation", adv: true,
          hint: "Regenerate the precondition as a separate contract-only pass." },
        { k: "agentic_refine_rounds", group: "agentic", type: "int", def: 0, min: 0, max: 5, label: "Agentic refine rounds",
          adv: true, hint: "Hand the harness back to the agentic generator on a realism rejection, N times." },
        { k: "role_spec_gen", type: "rolemodel", role: "spec_gen", def: "", label: "Spec-gen model", adv: true,
          hint: "Route spec generation to a different model (blank = your default). Uses your key." },
        { k: "role_refinement", type: "rolemodel", role: "refinement", def: "", label: "Refinement model", adv: true,
          hint: "Route refinement to a different model (blank = your default)." },
        { k: "role_realism", type: "rolemodel", role: "realism", def: "", label: "Realism model", adv: true,
          hint: "Route the realism audit to a different model (blank = your default)." },
      ],
    },
  ];
  const OPT_FIELDS = {};
  RUN_SECTIONS.forEach((s) => s.fields.forEach((f) => { OPT_FIELDS[f.k] = f; }));

  function loadOptions() {
    try { return JSON.parse(localStorage.getItem(OPT_KEY) || "{}") || {}; }
    catch (_) { return {}; }
  }
  function saveOptions(o) { localStorage.setItem(OPT_KEY, JSON.stringify(o)); }
  function optVal(store, f) { const v = store[f.k]; return v === undefined ? f.def : v; }
  function clampInt(f, raw) {
    let n = Math.round(Number(raw));
    if (!isFinite(n)) n = f.def;
    return Math.max(f.min, Math.min(f.max, n));
  }

  // Serialize the panel into the grouped `options` request object — only values
  // that differ from the default (absent ⇒ server Config/CLI default).
  function buildOptions() {
    const store = loadOptions();
    const out = {};
    const roles = {};
    const put = (g, k, v) => { (out[g] || (out[g] = {}))[k] = v; };
    for (const f of Object.values(OPT_FIELDS)) {
      const v = optVal(store, f);
      if (f.type === "int") {
        const n = clampInt(f, v);
        if (n !== f.def) put(f.group, f.k, n);
      } else if (f.type === "bool") {
        if (!!v !== !!f.def) put(f.group, f.k, !!v);
      } else if (f.type === "select") {
        if (f.k === "cbmc_object_bits") {
          // def IS the "auto" sentinel, so `v !== f.def` already excludes auto.
          if (v !== f.def) put(f.group, f.k, parseInt(v, 10));
        } else if (v !== f.def) put(f.group, f.k, v);
      } else if (f.type === "segment") {
        if (f.k === "run_mode") {
          if (v !== f.def) out.run_mode = v;          // top-level, not a group
        } else if (v !== f.def) put(f.group, f.k, v);
      } else if (f.type === "rolemodel") {
        const t = String(v || "").trim();
        if (t) roles[f.role] = { model: t };
      } else if (f.type === "defines") {
        const list = String(v || "").split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
        if (list.length) put(f.group, f.k, list);
      } else if (f.type === "text") {
        const t = String(v || "").trim();
        if (t) put(f.group, f.k, t);
      }
    }
    // Per-role model overrides nest under agentic.llm.roles (the BYOK key is
    // injected server-side; never sent here).
    if (Object.keys(roles).length) (out.agentic || (out.agentic = {})).llm = { roles };
    return out;
  }
  function countNonDefault() {
    return Object.values(buildOptions()).reduce((n, g) => n + Object.keys(g).length, 0);
  }

  // ---- rendering ----
  function rsControl(f, store) {
    const v = optVal(store, f);
    if (f.type === "bool") {
      return "<button type='button' class='rs-toggle" + (v ? " on" : "") +
        "' role='switch' aria-checked='" + (!!v) + "' data-k='" + f.k + "'><span class='knob'></span></button>";
    }
    if (f.type === "int") {
      return "<span class='rs-step'>" +
        "<button type='button' class='rs-dec' data-k='" + f.k + "'>−</button>" +
        "<input class='rs-num' type='text' inputmode='numeric' data-k='" + f.k + "' value='" + esc(v) + "'>" +
        (f.unit ? "<span class='rs-unit'>" + esc(f.unit) + "</span>" : "") +
        "<button type='button' class='rs-inc' data-k='" + f.k + "'>+</button></span>";
    }
    if (f.type === "select") {
      return "<select class='rs-select' data-k='" + f.k + "'>" +
        f.opts.map(([val, lbl]) => "<option value='" + esc(val) + "'" +
          (String(v) === val ? " selected" : "") + ">" + esc(lbl) + "</option>").join("") + "</select>";
    }
    if (f.type === "segment") {
      return "<span class='rs-seg' data-k='" + f.k + "'>" +
        f.opts.map(([val, lbl]) => "<button type='button' class='rs-seg-b" +
          (v === val ? " on" : "") + "' data-v='" + esc(val) + "'>" + esc(lbl) + "</button>").join("") + "</span>";
    }
    if (f.type === "rolemodel" || f.type === "defines") {
      const cls = f.type === "defines" ? "rs-definput" : "rs-roleinput";
      const ph = f.type === "defines" ? "NAME or NAME=VALUE" : "default";
      return "<input class='" + cls + "' type='text' data-k='" + f.k +
        "' value='" + esc(v) + "' placeholder='" + ph + "' autocomplete='off' spellcheck='false'>";
    }
    return "";
  }
  function rsField(f, store) {
    const lg = f.langs ? " data-langs='" + f.langs.join(",") + "'" : "";
    if (f.type === "text") {
      return "<div class='rs-field rs-field-text' data-fk='" + f.k + "'" + lg + ">" +
        "<div class='rs-flabel'>" + esc(f.label) + "</div>" +
        "<textarea class='rs-text' data-k='" + f.k + "' rows='2' placeholder='optional'>" +
        esc(optVal(store, f)) + "</textarea>" +
        "<div class='rs-hint'>" + esc(f.hint) + "</div></div>";
    }
    return "<div class='rs-field' data-fk='" + f.k + "'" + lg + ">" +
      "<div class='rs-finfo'><div class='rs-flabel'>" + esc(f.label) + "</div>" +
      "<div class='rs-hint'>" + esc(f.hint) + "</div></div>" +
      "<div class='rs-control'>" + rsControl(f, store) + "</div></div>";
  }
  function rsSection(sec, store) {
    const main = sec.fields.filter((f) => !f.adv), adv = sec.fields.filter((f) => f.adv);
    let h = "<details class='rs-sec' open><summary><span class='rs-sec-t'>" + esc(sec.title) +
      "</span><span class='rs-sec-blurb'>" + esc(sec.blurb) + "</span></summary><div class='rs-sec-body'>" +
      main.map((f) => rsField(f, store)).join("");
    if (adv.length) {
      h += "<details class='rs-adv'><summary></summary><div class='rs-adv-body'>" +
        adv.map((f) => rsField(f, store)).join("") + "</div></details>";
    }
    return h + "</div></details>";
  }
  function renderRunSettings() {
    const store = loadOptions();
    $("#runset-body").innerHTML = RUN_SECTIONS.map((s) => rsSection(s, store)).join("");
    syncRsDeps();
    updateRsCount();
    applyLangGating();
  }
  // Hide language-incompatible knobs (CBMC-only modeling on a Rust/Java repo, …)
  // once the in-scope languages are known from the estimate. Unknown ⇒ show all.
  function applyLangGating() {
    const langs = (S.langs && S.langs.length) ? S.langs : null;
    $$("#runset-body .rs-sec").forEach((sec) => {
      let anyVisible = false;
      $$(".rs-field[data-fk]", sec).forEach((row) => {
        const fl = row.getAttribute("data-langs");
        const hide = !!(langs && fl && !fl.split(",").some((l) => langs.includes(l)));
        row.classList.toggle("hidden", hide);
        if (!hide) anyVisible = true;
      });
      $$(".rs-adv", sec).forEach((adv) => {
        const vis = $$(".rs-field[data-fk]", adv).some((r) => !r.classList.contains("hidden"));
        adv.classList.toggle("hidden", !vis);
      });
      sec.classList.toggle("hidden", !anyVisible);
    });
  }
  function syncRsDeps() {
    const store = loadOptions();
    $$("#runset-body .rs-field[data-fk]").forEach((row) => {
      const f = OPT_FIELDS[row.getAttribute("data-fk")];
      if (!f || !f.dep) return;
      const depv = optVal(store, OPT_FIELDS[f.dep]);
      // A value-gated dep (depVal) enables only on a specific value (e.g. max
      // rounds when mode == autonomous); else gate on plain truthiness.
      const on = (f.depVal !== undefined) ? (depv === f.depVal) : !!depv;
      row.classList.toggle("rs-disabled", !on);
      $$("input,select,button,textarea", row).forEach((el) => { el.disabled = !on; });
    });
  }
  function updateRsCount() {
    const n = countNonDefault();
    $("#runset-count").textContent = n ? (n + " changed") : "";
    $("#runset-reset").classList.toggle("hidden", n === 0);
  }

  // ---- mutate one knob → persist, refresh deps + count + estimate ----
  function setOpt(k, v) {
    const store = loadOptions(), f = OPT_FIELDS[k];
    if (f.type === "int") v = clampInt(f, v);
    else if (f.type === "bool") v = !!v;
    const blankType = (f.type === "text" || f.type === "rolemodel" || f.type === "defines");
    const isDefault = blankType ? !String(v || "").trim() : (v === f.def);
    if (isDefault) delete store[k]; else store[k] = v;
    saveOptions(store);
    syncRsDeps();
    updateRsCount();
    updateScopeSummary();   // re-price with the new knob
  }

  function initRunSettings() {
    const panel = $("#runset");
    $("#runset-toggle").addEventListener("click", (e) => {
      if (e.target.id === "runset-reset") return;   // handled below
      const open = panel.classList.toggle("open");
      $("#runset-body").classList.toggle("hidden", !open);
      $(".rs-chev", panel).textContent = open ? "▾" : "▸";
    });
    $("#runset-reset").addEventListener("click", (e) => {
      e.stopPropagation();
      saveOptions({});
      renderRunSettings();
      updateScopeSummary();
    });
    const body = $("#runset-body");
    body.addEventListener("click", (e) => {
      const tgl = e.target.closest(".rs-toggle");
      if (tgl && !tgl.disabled) {
        const k = tgl.getAttribute("data-k"), cur = !!optVal(loadOptions(), OPT_FIELDS[k]);
        tgl.classList.toggle("on", !cur);
        tgl.setAttribute("aria-checked", String(!cur));
        setOpt(k, !cur);
        return;
      }
      const seg = e.target.closest(".rs-seg-b");
      if (seg && !seg.disabled) {
        const wrap = seg.closest(".rs-seg");
        $$(".rs-seg-b", wrap).forEach((b) => b.classList.remove("on"));
        seg.classList.add("on");
        setOpt(wrap.getAttribute("data-k"), seg.getAttribute("data-v"));
        return;
      }
      const stepBtn = e.target.closest(".rs-inc, .rs-dec");
      if (stepBtn && !stepBtn.disabled) {
        const k = stepBtn.getAttribute("data-k"), f = OPT_FIELDS[k];
        const input = $(".rs-num[data-k='" + k + "']", body);
        const n = clampInt(f, (Number(input.value) || f.def) + (stepBtn.classList.contains("rs-inc") ? 1 : -1));
        input.value = n;
        setOpt(k, n);
      }
    });
    body.addEventListener("change", (e) => {
      const sel = e.target.closest(".rs-select");
      if (sel) { setOpt(sel.getAttribute("data-k"), sel.value); return; }
      const num = e.target.closest(".rs-num");
      if (num) {
        const k = num.getAttribute("data-k");
        const n = clampInt(OPT_FIELDS[k], num.value);
        num.value = n;            // reflect the clamp back to the user
        setOpt(k, n);
      }
    });
    body.addEventListener("input", (e) => {
      const txt = e.target.closest(".rs-text, .rs-roleinput, .rs-definput");
      if (txt) setOpt(txt.getAttribute("data-k"), txt.value);
    });
    renderRunSettings();
  }

  // ==================================================================
  // INIT
  // ==================================================================
  function init() {
    initConnect();
    initScope();
    initRunControls();
    initRunBrowse();
    initSettings();
    initRunSettings();
    initConfirm();
    initExport();
    loadPresets();   // fill the Settings dropdown (built-in fallback until ready)
    refreshModelTag();
    go("connect");
    // Reconnect to an in-flight/finished run if the URL carries one (page
    // refresh). restoreRun swaps to the run view on success, or clears the URL
    // and leaves us on Connect if the run is gone.
    const resume = runInUrl();
    if (resume) restoreRun(resume);
    if (!loadCfg().key) { /* prompt later on run */ }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
