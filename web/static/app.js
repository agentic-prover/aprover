// AProver chat front-end.
// - sends conversation to /chat, reads SSE events
// - drives hero/empty-state, header status indicator, phase tracker
// - paints assistant text + a live phase-by-phase progress card

const heroEl = document.getElementById("hero");
const threadEl = document.getElementById("thread");
const chatEl = document.getElementById("chat");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const statusEl = document.getElementById("agent-status");
const statusDot = statusEl.querySelector(".status-dot");
const statusText = statusEl.querySelector(".status-text");
const msgTpl = document.getElementById("msg-tpl");
const runTpl = document.getElementById("run-tpl");

const history = [];
let busy = false;
let activeRun = null;

const PHASES = ["spec", "bmc", "classify", "report"];

function setStatus(state, label) {
  statusDot.className = "status-dot " + state;
  statusText.textContent = label;
}

function setBusy(b, label) {
  busy = b;
  sendBtn.disabled = b;
  if (b) setStatus("thinking", label || "thinking");
  else setStatus("idle", "idle");
}

function hideHero() {
  if (heroEl && !heroEl.classList.contains("hidden")) heroEl.classList.add("hidden");
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function renderMarkdown(text) {
  const blocks = [];
  let i = 0;
  text = String(text).replace(/```([^\n]*)\n([\s\S]*?)```/g, (_m, lang, body) => {
    blocks.push({ lang: lang.trim(), body });
    return ` BLOCK${i++} `;
  });
  text = escapeHtml(text);
  text = text.replace(/`([^`\n]+)`/g, (_m, c) => `<code>${escapeHtml(c)}</code>`.replace(/&amp;(lt|gt|amp);/g, "&$1;"));
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  const paragraphs = text
    .split(/\n{2,}/)
    .map((p) => `<p>${p.replace(/\n/g, "<br />")}</p>`)
    .join("");
  return paragraphs.replace(/ BLOCK(\d+) /g, (_m, idx) => {
    const b = blocks[+idx];
    return `<pre><code class="lang-${escapeHtml(b.lang)}">${escapeHtml(b.body)}</code></pre>`;
  });
}

function appendMsg(role, html, opts = {}) {
  hideHero();
  const node = msgTpl.content.firstElementChild.cloneNode(true);
  node.classList.add(role);
  node.querySelector(".role").textContent = opts.label || role;
  node.querySelector(".body").innerHTML = html;
  threadEl.appendChild(node);
  scrollToBottom();
  return node;
}

function appendRunCard() {
  hideHero();
  const node = runTpl.content.firstElementChild.cloneNode(true);
  threadEl.appendChild(node);
  scrollToBottom();
  const logDisc = node.querySelector(".log-disc");
  // Open the log during a run; we'll auto-collapse once the result arrives.
  logDisc.open = true;
  activeRun = {
    rootEl: node,
    bodyEl: node.querySelector(".body"),
    logEl: node.querySelector(".run-log"),
    logDiscEl: logDisc,
    phaseEls: Array.from(node.querySelectorAll(".phase")),
    phaseLineEls: Array.from(node.querySelectorAll(".phase-line")),
    nowLineEl: node.querySelector(".now-line"),
    nowTextEl: node.querySelector(".now-text"),
    elapsedEl: node.querySelector(".run-elapsed"),
    elapsedNumEl: node.querySelector(".run-elapsed-num"),
    startedAt: performance.now(),
    timerId: null,
    currentPhase: null,
    completedPhases: new Set(),
  };
  // start elapsed timer (10Hz, tabular-nums keeps width stable)
  activeRun.timerId = setInterval(() => {
    if (!activeRun) return;
    const t = (performance.now() - activeRun.startedAt) / 1000;
    activeRun.elapsedNumEl.textContent = t.toFixed(1);
  }, 100);
  return activeRun;
}

function stopRunTimer(state) {
  if (!activeRun) return;
  if (activeRun.timerId) {
    clearInterval(activeRun.timerId);
    activeRun.timerId = null;
  }
  if (state) activeRun.elapsedEl.classList.add(state);
}

function scrollToBottom() {
  chatEl.scrollTop = chatEl.scrollHeight;
}

function setPhase(phaseName) {
  if (!activeRun) return;
  const order = PHASES.indexOf(phaseName);
  if (order < 0) return;
  activeRun.phaseEls.forEach((el) => {
    const p = el.getAttribute("data-phase");
    const idx = PHASES.indexOf(p);
    el.classList.remove("active", "done");
    if (idx < order) el.classList.add("done");
    else if (idx === order) el.classList.add("active");
  });
  // Connector lines: lines before `order` are done; line just before active flows.
  activeRun.phaseLineEls.forEach((el, i) => {
    el.classList.remove("flowing", "done");
    if (i < order - 1) el.classList.add("done");
    else if (i === order - 1) el.classList.add("flowing");
  });
  activeRun.currentPhase = phaseName;
}

function completePhase(phaseName) {
  if (!activeRun) return;
  const el = activeRun.phaseEls.find((e) => e.getAttribute("data-phase") === phaseName);
  if (!el) return;
  el.classList.remove("active");
  el.classList.add("done");
  activeRun.completedPhases.add(phaseName);
  // Line right after the just-finished phase becomes flowing if the next
  // phase hasn't kicked in yet (so the user sees motion *between* phases too).
  const idx = PHASES.indexOf(phaseName);
  const nextLine = activeRun.phaseLineEls[idx];
  if (nextLine && !nextLine.classList.contains("done")) {
    nextLine.classList.add("flowing");
  }
}

// Pull a short, human-friendly headline out of a log line so the now-line
// reads less like a debug dump and more like "what is the agent doing".
function extractActivity(message) {
  const m = String(message);
  if (/Parsing source file/.test(m)) return "parsing source";
  if (/Phase 1: Generating specs/.test(m)) return "generating function specs";
  let mm = m.match(/Processing layer (\d+): \[?([^\]\n]+)/);
  if (mm) return `spec layer ${mm[1]} · ${mm[2].split(",")[0].trim()}`;
  if (/Phase 1 complete/.test(m)) return "specs generated";
  if (/Phase 1\.5: Selecting per-function CBMC flags/.test(m))
    return "selecting CBMC flags per function";
  if (/Phase 2: Running BMC on (\d+)/.test(m)) {
    const [, n] = m.match(/Phase 2: Running BMC on (\d+)/);
    return `running CBMC on ${n} function${n === "1" ? "" : "s"}`;
  }
  if (/Phase 2 complete/.test(m)) return "bounded model checking done";
  if (/Phase 3: Validating/.test(m)) return "classifying counterexamples";
  if (/Phase 3c:/.test(m)) return "refining specs";
  mm = m.match(/Checking function '([^']+)'/);
  if (mm) return `bmc · ${mm[1]}`;
  mm = m.match(/Generating spec for '([^']+)'/i);
  if (mm) return `spec · ${mm[1]}`;
  mm = m.match(/CBMC verdict for '([^']+)':\s*verified=(\w+)/);
  if (mm) return `verdict · ${mm[1]} · ${mm[2] === "True" ? "verified" : "counterexample"}`;
  mm = m.match(/Recheck: validating counterexample for '([^']+)'/);
  if (mm) return `recheck · ${mm[1]}`;
  mm = m.match(/Validating counterexample for '([^']+)'/);
  if (mm) return `classifying · ${mm[1]}`;
  if (/REAL BUG \(Phase 3c\) confirmed in '([^']+)'/.test(m)) {
    const [, fn] = m.match(/REAL BUG \(Phase 3c\) confirmed in '([^']+)'/);
    return `real bug (refined) confirmed in ${fn}`;
  }
  if (/REAL BUG (?:\(recheck\) )?confirmed in '([^']+)'/.test(m)) {
    const [, fn] = m.match(/REAL BUG (?:\(recheck\) )?confirmed in '([^']+)'/);
    return `real bug confirmed in ${fn}`;
  }
  if (/AMC Pipeline END/.test(m)) return "wrapping up";
  return null;
}

function setNowLine(text) {
  if (!activeRun) return;
  activeRun.nowTextEl.textContent = text;
}

function inspectLogLine(message) {
  const m = String(message);
  if (/Phase 1: Generating specs/.test(m)) setPhase("spec");
  else if (/Phase 1 complete/.test(m)) completePhase("spec");
  // Phase 1.5 (flag selection) sits between spec and bmc; keep the spec→bmc
  // connector "flowing" so the UI feels alive while flags are picked.
  else if (/Phase 2: Running BMC/.test(m)) setPhase("bmc");
  else if (/Phase 2 complete/.test(m)) completePhase("bmc");
  else if (/Phase 3: Validating/.test(m)) setPhase("classify");
  // Phase 3c (refinement) loops back inside classify — don't bounce the phase,
  // just let the now-line headline change to "refining specs".
  else if (/=== AMC Pipeline END/.test(m)) {
    completePhase("classify");
    setPhase("report");
  }
  const headline = extractActivity(m);
  if (headline) setNowLine(headline);
}

function logRunLine(level, message) {
  if (!activeRun) return;
  const li = document.createElement("li");
  li.className = level || "info";
  li.textContent = message;
  activeRun.logEl.appendChild(li);
  activeRun.logEl.scrollTop = activeRun.logEl.scrollHeight;
  inspectLogLine(message);
}

function renderResult(result) {
  if (!activeRun) return;
  if (!result.ok) {
    setStatus("error", "failed");
    stopRunTimer();
    activeRun.phaseEls.forEach((e) => e.classList.remove("active"));
    activeRun.phaseLineEls.forEach((e) => e.classList.remove("flowing"));
    activeRun.nowLineEl.classList.remove("done");
    activeRun.nowLineEl.classList.add("idle");
    setNowLine("pipeline failed");
    const err = document.createElement("div");
    err.className = "bug-summary";
    err.innerHTML = `<div class="verdict bad">▲ pipeline failed</div><pre>${escapeHtml(result.error || "unknown error")}</pre>`;
    activeRun.bodyEl.appendChild(err);
    return;
  }
  completePhase("report");
  activeRun.phaseEls.forEach((e) => e.classList.remove("active"));
  activeRun.phaseLineEls.forEach((e) => {
    e.classList.remove("flowing");
    e.classList.add("done");
  });
  stopRunTimer("done");
  activeRun.nowLineEl.classList.add("done");
  setNowLine(
    !result.bugs || result.bugs.length === 0
      ? "no bugs confirmed"
      : `${result.bugs.length} bug${result.bugs.length === 1 ? "" : "s"} confirmed`
  );
  // Auto-collapse the verbose log once the result is in view.
  if (activeRun.logDiscEl) activeRun.logDiscEl.open = false;

  const wrap = document.createElement("div");
  wrap.className = "bug-summary";
  if (!result.bugs || result.bugs.length === 0) {
    wrap.innerHTML = `<div class="verdict ok">✓ no bugs confirmed</div>`;
  } else {
    const v = document.createElement("div");
    v.className = "verdict bad";
    v.textContent = `▲ ${result.bugs.length} bug${result.bugs.length === 1 ? "" : "s"} confirmed`;
    wrap.appendChild(v);
    for (const b of result.bugs) {
      const card = document.createElement("div");
      card.className = "bug-card";
      const chain = (b.call_chain || []).join(" → ");
      const reasoning = (b.reasoning || "").trim();
      card.innerHTML = `
        <div class="row">
          <span class="fn-name">${escapeHtml(b.function || "?")}</span>
          <span class="badge tier-${escapeHtml(b.confidence || "")}">${escapeHtml(b.confidence || "")}</span>
          <span class="badge">${escapeHtml(b.bug_type || "")}</span>
        </div>
        <div class="prop">${escapeHtml(b.violated_property || "")}</div>
        ${chain ? `<div class="chain">via ${escapeHtml(chain)}</div>` : ""}
        ${reasoning ? `
          <details class="bug-reason">
            <summary>why this is a bug</summary>
            <div class="reason-body">${escapeHtml(reasoning)}</div>
          </details>` : ""}
      `;
      wrap.appendChild(card);
    }
  }
  activeRun.bodyEl.appendChild(wrap);
  scrollToBottom();
}

async function streamChat() {
  setBusy(true, "thinking");

  const res = await fetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages: history }),
  });
  if (!res.ok || !res.body) {
    appendMsg("system", `network error: ${res.status} ${res.statusText}`);
    setBusy(false);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const evt = parseSSE(chunk);
      if (evt) handleEvent(evt);
    }
  }

  setBusy(false);
}

function parseSSE(chunk) {
  const lines = chunk.split("\n");
  let event = "message";
  const dataLines = [];
  for (const line of lines) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return { event, data: { raw: dataLines.join("\n") } };
  }
}

function handleEvent(evt) {
  switch (evt.event) {
    case "assistant_text": {
      appendMsg("assistant", renderMarkdown(evt.data.text || ""), { label: "aprover" });
      history.push({ role: "assistant", content: evt.data.text || "" });
      break;
    }
    case "tool_call":
      if (evt.data.name === "fetch_source") {
        const url = (evt.data.input || {}).url || "";
        appendMsg("system", `→ fetching <code>${escapeHtml(url)}</code>`, { label: "tool" });
        setStatus("working", "fetching");
      } else if (evt.data.name === "run_aprover") {
        appendRunCard();
        setStatus("working", "verifying");
      }
      break;
    case "tool_progress": {
      const d = evt.data || {};
      if (d.type === "fetch_result") {
        const note = d.ok
          ? `← fetched ${d.bytes} bytes`
          : `× fetch failed: ${escapeHtml(d.error || "")}`;
        appendMsg("system", note, { label: "tool" });
      } else if (d.type === "started") {
        if (activeRun) logRunLine("info", "pipeline started");
      } else if (d.type === "log") {
        logRunLine(d.level, d.message);
      } else if (d.type === "error") {
        logRunLine("error", d.message);
      } else if (d.type === "result") {
        renderResult(d.result || {});
        setStatus("thinking", "summarizing");
      }
      break;
    }
    case "error":
      appendMsg("system", `server error: ${escapeHtml(evt.data.message || "unknown")}`);
      setStatus("error", "error");
      break;
    case "done":
      break;
  }
}

// ---- Composer wiring ----

function autoGrow() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 220) + "px";
}

input.addEventListener("input", autoGrow);

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (busy) return;
  const text = input.value.trim();
  if (!text) return;
  appendMsg("user", renderMarkdown(text), { label: "you" });
  history.push({ role: "user", content: text });
  input.value = "";
  autoGrow();
  streamChat().catch((err) => {
    appendMsg("system", `client error: ${escapeHtml(String(err))}`);
    setBusy(false);
  });
});

// Quick-action chips: prefill the composer (or send immediately if the prompt
// is a self-contained command).
document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const prompt = chip.getAttribute("data-prompt") || "";
    input.value = prompt;
    autoGrow();
    input.focus();
    // If the prompt ends with ": " (i.e. expects user completion), don't auto-send.
    if (!/[:?\s]$/.test(prompt)) {
      form.requestSubmit();
    }
  });
});

// ---- Hero live-preview demo ----
// Drives a looping fake run on the hero so visitors see the agent in motion
// before they ever type anything. Hover pauses; click loads the snippet into
// the composer so they can run it for real.

const DEMO_SOURCE = `#include <stdint.h>
int add(int a, int b) {
    return a + b;
}`;

function startHeroDemo() {
  const demoEl = document.getElementById("hero-demo");
  if (!demoEl) return;

  const PHASES = ["spec", "bmc", "classify", "report"];
  const phaseEls = Array.from(demoEl.querySelectorAll(".demo-phase"));
  const lineEls = Array.from(demoEl.querySelectorAll(".phase-line"));
  const nowEl = demoEl.querySelector(".demo-now");
  const nowText = demoEl.querySelector(".demo-now-text");
  const resultEl = demoEl.querySelector(".demo-result");
  const caretEl = demoEl.querySelector(".demo-caret");

  // The caret hops between source lines so the user sees the agent "reading"
  // the code while specs are being generated. Line offsets are tuned to
  // line-height 1.7 × 12.5px ≈ 21px, with a head offset that lines the caret
  // up with the file header.
  const CARET_TOP_BASE = 56; // px — top of first line after the file header
  const LINE_H = 21.25;

  const script = [
    { phase: "spec", text: "parsing source…", ms: 1100, line: 0 },
    { phase: "spec", text: "generating spec for add()", ms: 1500, line: 1 },
    { phase: "spec", text: "spec ready · int, int → bounded add", ms: 1000, line: 1, done: ["spec"] },
    { phase: "bmc",  text: "running CBMC on add()", ms: 1500, line: 2 },
    { phase: "bmc",  text: "counterexample · a = INT_MAX, b = 1", ms: 1700, line: 2, done: ["spec"] },
    { phase: "classify", text: "validating counterexample", ms: 1500, line: 2, done: ["spec","bmc"] },
    { phase: "classify", text: "real bug · signed integer overflow", ms: 1400, line: 2, done: ["spec","bmc"] },
    { phase: "report", text: "bug report written", ms: 2600, line: 2, done: ["spec","bmc","classify"], showResult: true, finished: true },
    { reset: true, ms: 1400 },
  ];

  function applyStep(step) {
    if (step.reset) {
      phaseEls.forEach((e) => e.classList.remove("active", "done"));
      lineEls.forEach((e) => e.classList.remove("flowing", "done"));
      resultEl.hidden = true;
      nowEl.classList.remove("done");
      nowText.textContent = "ready · paste C to run for real";
      if (caretEl) caretEl.classList.remove("visible");
      return;
    }
    const order = PHASES.indexOf(step.phase);
    const doneSet = new Set(step.done || []);
    phaseEls.forEach((el) => {
      const p = el.getAttribute("data-phase");
      el.classList.remove("active", "done");
      if (doneSet.has(p)) el.classList.add("done");
      else if (p === step.phase && !step.finished) el.classList.add("active");
      else if (p === step.phase && step.finished) el.classList.add("done");
    });
    lineEls.forEach((el, i) => {
      el.classList.remove("flowing", "done");
      const prevPhase = PHASES[i];
      if (doneSet.has(prevPhase) && doneSet.has(PHASES[i + 1])) el.classList.add("done");
      else if (doneSet.has(prevPhase)) el.classList.add("flowing");
    });
    nowText.textContent = step.text;
    nowEl.classList.toggle("done", !!step.finished);
    resultEl.hidden = !step.showResult;
    if (caretEl) {
      if (typeof step.line === "number") {
        caretEl.style.top = CARET_TOP_BASE + step.line * LINE_H + "px";
        caretEl.classList.add("visible");
      } else {
        caretEl.classList.remove("visible");
      }
    }
  }

  let idx = 0;
  let timerId = null;
  let paused = false;

  function tick() {
    if (!demoEl.isConnected) return;
    if (document.getElementById("hero")?.classList.contains("hidden")) return;
    if (paused) {
      timerId = setTimeout(tick, 300);
      return;
    }
    const step = script[idx];
    applyStep(step);
    idx = (idx + 1) % script.length;
    timerId = setTimeout(tick, step.ms);
  }
  tick();

  demoEl.addEventListener("mouseenter", () => {
    paused = true;
    demoEl.classList.add("paused");
  });
  demoEl.addEventListener("mouseleave", () => {
    paused = false;
    demoEl.classList.remove("paused");
  });

  function loadDemoIntoComposer() {
    input.value = `verify this:\n\n\`\`\`c\n${DEMO_SOURCE}\n\`\`\``;
    autoGrow();
    input.focus();
  }
  demoEl.addEventListener("click", loadDemoIntoComposer);
  demoEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      loadDemoIntoComposer();
    }
  });
}

startHeroDemo();

// Initial state: hero shown, idle status.
setStatus("idle", "idle");
