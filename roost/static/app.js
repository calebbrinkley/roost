"use strict";

const $ = (id) => document.getElementById(id);
const state = { section: "messages", current: null, selectedJob: null, jobRuns: {}, conversations: [], jobs: [], offset: 0,
                busy: false, job: null, draft: false, pending: null, search: "" };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function show(id, visible) { $(id).classList.toggle("hidden", !visible); }
function refreshThemeControls() {
  const switchToLight = window.roostTheme.current() === "dark";
  const label = switchToLight ? "Switch to light mode" : "Switch to dark mode";
  for (const button of document.querySelectorAll("[data-theme-toggle]")) {
    button.setAttribute("aria-label", label);
    button.title = label;
    button.textContent = button.classList.contains("theme-icon-only")
      ? (switchToLight ? "☀" : "☾")
      : (switchToLight ? "☀ Light mode" : "☾ Dark mode");
  }
}
for (const button of document.querySelectorAll("[data-theme-toggle]")) {
  button.addEventListener("click", () => window.roostTheme.toggle());
}
window.addEventListener("roostthemechange", refreshThemeControls);
refreshThemeControls();
function notice(message) {
  $("notice").textContent = message;
  show("notice", Boolean(message));
  if (message) setTimeout(() => { if ($("notice").textContent === message) show("notice", false); }, 6000);
}

async function api(path, options = {}) {
  const response = await fetch("/api" + path, {
    credentials: "same-origin",
    headers: { "X-Roost-Request": "1", ...(options.body ? { "Content-Type": "application/json" } : {}) },
    ...options,
  });
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) {
    if (response.status === 401 && path !== "/login") { signedOut(); throw new Error("Please sign in again."); }
    throw new Error(data.error || `Request failed (${response.status}).`);
  }
  return data;
}

function signedOut() {
  show("app", false); show("login", true);
  $("app").classList.remove("chat-open");
  $("password").value = "";
}

async function signedIn() {
  show("login", false); show("app", true);
  $("login-error").textContent = "";
  setSection("messages");
  await loadConversations();
}

function setSection(section) {
  state.section = section;
  $("app").classList.toggle("section-jobs", section === "jobs");
  $("app").classList.remove("chat-open", "job-open");
  show("messages-view", section === "messages"); show("jobs-view", section === "jobs");
  $("nav-messages").classList.toggle("active", section === "messages");
  $("nav-jobs").classList.toggle("active", section === "jobs");
  $("mobile-nav-messages").classList.toggle("active", section === "messages");
  $("mobile-nav-jobs").classList.toggle("active", section === "jobs");
  show("sidebar-search", section === "messages");
  show("conversation-list", section === "messages");
  show("sidebar-jobs-list", section === "jobs");
  $("mobile-list-title").textContent = section === "jobs" ? "Jobs" : "Messages";
  $("top-action").textContent = section === "jobs" ? "+" : "✎";
  $("top-action").setAttribute("aria-label", section === "jobs" ? "New job" : "New conversation");
  $("top-action").title = section === "jobs" ? "New job" : "New conversation";
  if (section === "jobs") show("more-conversations", false);
  else renderConversations();
  if (section === "jobs") loadJobs().catch((error) => notice(error.message));
}

function timeLabel(value) {
  if (!value) return "";
  const date = typeof value === "number" ? new Date(value > 1e11 ? value : value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  if (now - date < 6 * 86400000) return date.toLocaleDateString(undefined, { weekday: "short" });
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function sessionName(item) {
  return item.title || item.name || item.preview || "Conversation";
}

function renderConversations() {
  const list = $("conversation-list"); list.replaceChildren();
  const rows = state.conversations.filter((item) =>
    (sessionName(item) + " " + (item.preview || "") + " " + (item.source || "")).toLowerCase().includes(state.search));
  if (!rows.length) list.append(el("p", "empty-list", state.search ? "No matching conversations." : "No conversations yet. Start one with Wren."));
  for (const item of rows) {
    const id = item.session_id || item.id;
    if (!id) continue;
    const button = el("button", "conversation-row" + (state.current === id ? " selected" : ""));
    const copy = el("span", "conversation-copy");
    const heading = el("span", "conversation-heading");
    heading.append(el("span", "conversation-name", sessionName(item)));
    if (item.source && item.source !== "roost") heading.append(el("span", "source-tag", item.source));
    copy.append(heading, el("span", "row-preview" + (state.pending?.id === id ? " live" : ""),
                         state.pending?.id === id ? "Wren is typing…" : (item.preview || "Conversation with Wren")));
    button.append(copy);
    const meta = el("span", "row-meta");
    meta.append(el("span", "", timeLabel(item.last_active_at || item.last_active || item.updated_at || item.created_at)));
    if (state.pending?.id === id) meta.append(el("span", "unread-dot"));
    button.append(meta);
    button.addEventListener("click", () => selectConversation(id, item.source));
    list.append(button);
  }
}

async function loadConversations(more = false) {
  const offset = more ? state.offset + 100 : 0;
  const data = await api(`/conversations?offset=${offset}`);
  $("presence").textContent = "Online";
  const rows = Array.isArray(data.sessions) ? data.sessions : [];
  state.offset = offset;
  state.conversations = more ? [...state.conversations, ...rows] : rows;
  if (!state.current && !state.draft && !more && rows.length) {
    state.current = rows[0].session_id || rows[0].id;
    $("chat-title").textContent = sessionName(rows[0]);
    loadMessages(state.current).catch((error) => notice(error.message));
  }
  renderConversations();
  if (!state.current && !more) renderWelcome();
  if (data.total !== undefined) show("more-conversations", state.conversations.length < data.total);
}

function renderWelcome() {
  const transcript = $("transcript"); transcript.replaceChildren();
  const card = el("div", "welcome-card");
  card.append(el("div", "welcome-symbol", "W"), el("h2", "", "Message Wren"),
              el("p", "", "A question, an idea, or something to work through. Start wherever you like."));
  transcript.append(card);
  $("chat-title").textContent = "New conversation";
  show("origin-badge", false);
}

function messageText(message) {
  const value = message.display_content ?? message.content;
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map((part) => typeof part === "string" ? part : (part.text || part.type || "")).join("\n");
  if (value && typeof value === "object") return value.text || JSON.stringify(value);
  return "";
}

function addInline(parent, value) {
  for (const piece of value.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)) {
    if (!piece) continue;
    if (piece.startsWith("**") && piece.endsWith("**")) parent.append(el("strong", "", piece.slice(2, -2)));
    else if (piece.startsWith("`") && piece.endsWith("`")) parent.append(el("code", "", piece.slice(1, -1)));
    else parent.append(document.createTextNode(piece));
  }
}

function addPlainMarkdown(parent, value) {
  const lines = value.split("\n");
  for (let i = 0; i < lines.length;) {
    if (!lines[i].trim()) { i++; continue; }
    if (/^\s*[-*•] /.test(lines[i])) {
      const list = el("ul");
      while (i < lines.length && /^\s*[-*•] /.test(lines[i])) {
        const item = el("li"); addInline(item, lines[i].replace(/^\s*[-*•] /, "")); list.append(item); i++;
      }
      parent.append(list); continue;
    }
    if (lines[i].includes("|") && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1])) {
      const table = el("table");
      const head = el("tr");
      for (const cell of lines[i].replace(/^\||\|$/g, "").split("|")) head.append(el("th", "", cell.trim()));
      table.append(head); i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        const row = el("tr");
        for (const cell of lines[i].replace(/^\||\|$/g, "").split("|")) row.append(el("td", "", cell.trim()));
        table.append(row); i++;
      }
      parent.append(table); continue;
    }
    const paragraph = [];
    while (i < lines.length && lines[i].trim() && !/^\s*[-*•] /.test(lines[i])) paragraph.push(lines[i++]);
    const node = el("p"); addInline(node, paragraph.join("\n")); parent.append(node);
  }
}

function addMarkdown(parent, value) {
  const fence = /```[^\n]*\n([\s\S]*?)```/g;
  let last = 0;
  for (const match of value.matchAll(fence)) {
    addPlainMarkdown(parent, value.slice(last, match.index));
    const pre = el("pre"); pre.append(el("code", "", match[1].replace(/\n$/, ""))); parent.append(pre);
    last = match.index + match[0].length;
  }
  addPlainMarkdown(parent, value.slice(last));
}

function renderMessages(rows) {
  const transcript = $("transcript"); transcript.replaceChildren();
  const visible = rows.filter((row) => ["user", "assistant"].includes(row.role) && row.display_kind !== "hidden" && messageText(row));
  if (!visible.length) transcript.append(el("p", "empty-transcript", "No messages here yet."));
  for (let index = 0; index < visible.length; index++) {
    const row = visible[index];
    const wrapper = el("article", "message " + (row.role === "user" ? "from-you" : "from-wren"));
    const body = el("div", "message-body"); addMarkdown(body, messageText(row)); wrapper.append(body);
    transcript.append(wrapper);
    if (row.role === "user") transcript.append(el("div", "under right", visible.slice(index + 1).some((next) => next.role === "assistant") ? "Read" : "Delivered"));
  }
  if (state.pending?.id === state.current) {
    let ownMessage = -1;
    for (let i = visible.length - 1; i >= 0; i--) {
      if (visible[i].role === "user" && messageText(visible[i]) === state.pending.text) { ownMessage = i; break; }
    }
    if ((ownMessage >= 0 && visible.slice(ownMessage + 1).some((row) => row.role === "assistant")) || Date.now() - state.pending.since > 600000) state.pending = null;
    else {
      const typing = el("div", "typing-row");
      const dots = el("span", "typing-dots"); dots.append(el("i"), el("i"), el("i"));
      typing.append(dots, el("span", "", "Wren is typing")); transcript.append(typing);
    }
  }
  renderConversations();
  transcript.scrollTop = transcript.scrollHeight;
}

async function selectConversation(id, source) {
  state.current = id;
  state.draft = false;
  $("app").classList.add("chat-open");
  const item = state.conversations.find((row) => (row.session_id || row.id) === id);
  $("chat-title").textContent = item ? sessionName(item) : "Conversation";
  $("origin-badge").textContent = source && source !== "roost" ? source : "";
  show("origin-badge", Boolean(source && source !== "roost"));
  renderConversations();
  $("transcript").replaceChildren(el("p", "empty-transcript", "Loading conversation…"));
  try { await loadMessages(id); } catch (error) { notice(error.message); }
}

async function loadMessages(id = state.current) {
  if (!id) return;
  const data = await api(`/conversations/${encodeURIComponent(id)}`);
  if (state.current === id) renderMessages(Array.isArray(data.messages) ? data.messages : []);
}

function newConversation() {
  state.current = null;
  state.draft = true;
  $("app").classList.add("chat-open");
  renderConversations(); renderWelcome();
  $("message").focus();
}

async function submitMessage(event) {
  event.preventDefault();
  if (state.busy) return;
  const text = $("message").value.trim();
  if (!text) return;
  state.busy = true; $("send").disabled = true; $("message").value = "";
  $("composer").classList.remove("has-text");
  const oldId = state.current;
  $("transcript").append(el("p", "empty-transcript", "Sending…"));
  $("transcript").scrollTop = $("transcript").scrollHeight;
  try {
    const result = oldId
      ? await api(`/conversations/${encodeURIComponent(oldId)}/messages`, { method: "POST", body: JSON.stringify({ text }) })
      : await api("/conversations", { method: "POST", body: JSON.stringify({ text }) });
    state.current = result.session_id;
    state.draft = false;
    state.pending = { id: result.session_id, text, since: Date.now() };
    await loadConversations();
    await loadMessages();
  } catch (error) {
    $("message").value = text;
    $("composer").classList.add("has-text");
    notice(error.message);
    if (oldId) await loadMessages(oldId).catch(() => {}); else renderWelcome();
  } finally { state.busy = false; $("send").disabled = false; $("message").focus(); }
}

function jobValue(job, key) { return job[key] == null ? "" : String(job[key]); }

function scheduleLabel(schedule) {
  const match = /^(\d{1,2}) (\d{1,2}) (\*|\d{1,2}) \* (\*|0|1-5)$/.exec(schedule);
  if (!match) return schedule;
  const at = match[2].padStart(2, "0") + ":" + match[1].padStart(2, "0");
  if (match[3] !== "*") {
    const day = Number(match[3]);
    const suffix = day % 100 >= 11 && day % 100 <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" }[day % 10] || "th");
    return day + suffix + " of each month at " + at;
  }
  if (match[4] === "1-5") return "Weekdays at " + at;
  if (match[4] === "0") return "Sundays at " + at;
  return "Daily at " + at;
}

function updateSchedulePreview() {
  const schedule = $("job-schedule").value.trim();
  const label = scheduleLabel(schedule);
  $("job-schedule-preview").textContent = !schedule ? "Example: 0 8 * * * runs daily at 8:00." :
    label === schedule ? "This schedule will be sent to Hermes as entered." : "Runs " + label.toLowerCase() + ".";
}

function renderJobs() {
  const list = $("sidebar-jobs-list"); list.replaceChildren();
  if (!state.jobs.length) list.append(el("p", "empty-list", "No jobs yet. Add one for Wren to handle on a schedule."));
  for (const job of state.jobs) {
    const id = job.id || job.job_id;
    if (!id) continue;
    const row = el("button", "job-list-row" + (state.selectedJob === id ? " selected" : ""));
    row.type = "button";
    if (state.selectedJob === id) row.setAttribute("aria-current", "true");
    row.append(el("span", "job-list-name", jobValue(job, "name") || "Untitled job"),
               el("span", "job-list-schedule", scheduleLabel(jobValue(job, "schedule"))));
    const meta = el("span", "job-list-meta");
    const status = jobStatus(job);
    meta.append(el("span", "job-status-dot " + status.kind), el("span", "", status.label));
    row.append(meta);
    row.addEventListener("click", () => selectJob(id));
    list.append(row);
  }
  renderJobDetail();
}

function jobStatus(job) {
  if (job.paused) return { label: "Paused", kind: "paused" };
  if (job.last_status === "completed") return { label: "Ran recently", kind: "ok" };
  if (job.last_status === "needs_attention") return { label: "Needs a look", kind: "warn" };
  if (job.last_status) return { label: String(job.last_status).replaceAll("_", " "), kind: "warn" };
  return { label: "Not run yet", kind: "paused" };
}

function currentJob() {
  return state.jobs.find((job) => (job.id || job.job_id) === state.selectedJob);
}

function selectJob(id) {
  state.selectedJob = id;
  $("app").classList.add("job-open");
  renderJobs();
  $("job-detail").scrollTop = 0;
  if (!Object.hasOwn(state.jobRuns, id)) loadJobRuns(id).catch((error) => notice(error.message));
}

function renderJobDetail() {
  const job = currentJob();
  const detail = $("job-detail");
  const scrollTop = detail.scrollTop;
  detail.replaceChildren();
  $("job-detail-title").textContent = job ? jobValue(job, "name") || "Untitled job" : "Jobs";
  $("job-detail-subtitle").textContent = job ? scheduleLabel(jobValue(job, "schedule")) : "Scheduled work with Wren";
  show("job-detail-actions", Boolean(job));
  if (!job) {
    const welcome = el("div", "job-welcome welcome-card");
    welcome.append(el("div", "welcome-symbol", "◷"), el("h2", "", state.jobs.length ? "Choose a job" : "Give Wren a job"),
                   el("p", "", state.jobs.length ? "Select a job to see its schedule, instructions, and recent runs." : "Set up something Wren can take care of on a schedule."));
    detail.append(welcome);
    return;
  }
  const status = jobStatus(job);
  const summary = el("section", "job-detail-section job-summary");
  const top = el("div", "job-detail-top");
  top.append(el("span", "pill " + status.kind, status.label));
  const toggle = el("button", "job-toggle" + (job.paused ? " off" : ""));
  toggle.type = "button"; toggle.setAttribute("role", "switch");
  toggle.setAttribute("aria-checked", String(!job.paused));
  toggle.setAttribute("aria-label", (job.paused ? "Resume " : "Pause ") + (jobValue(job, "name") || "job"));
  toggle.addEventListener("click", () => jobAction(job.id || job.job_id, job.paused ? "resume" : "pause"));
  top.append(toggle);
  summary.append(top, el("h2", "", "Last report"));
  summary.append(el("div", "job-report", job.last_run_preview || job.last_output || job.last_report || "No report yet. Wren’s next run will appear here."));
  detail.append(summary);
  const instructions = el("section", "job-detail-section");
  instructions.append(el("h2", "", "What Wren will do"), el("p", "job-prompt", jobValue(job, "prompt") || "No instructions provided."));
  detail.append(instructions);
  const history = el("section", "job-detail-section job-history");
  const historyHeading = el("div", "job-history-heading");
  const refresh = el("button", "text-button", "Refresh");
  refresh.type = "button";
  refresh.setAttribute("aria-label", "Refresh recent runs");
  refresh.addEventListener("click", () => loadJobRuns(job.id || job.job_id).catch((error) => notice(error.message)));
  historyHeading.append(el("h2", "", "Recent runs"), refresh);
  history.append(historyHeading);
  const runs = state.jobRuns[job.id || job.job_id];
  if (!runs) history.append(el("p", "job-history-empty", "Loading recent runs…"));
  else if (!runs.length) history.append(el("p", "job-history-empty", "No runs yet."));
  else for (const run of runs) {
    const row = el("article", "run-row");
    const heading = el("div", "run-heading");
    heading.append(el("strong", "", run.title || run.status || run.state || "Run"),
                   el("span", "", timeLabel(run.started_at || run.created_at || run.timestamp)));
    row.append(heading);
    const report = run.output || run.result || run.summary || run.report || run.preview || "";
    if (report) row.append(el("pre", "", typeof report === "string" ? report : JSON.stringify(report, null, 2)));
    if (run.id && run.source !== "cron_output") {
      const open = el("button", "", "Open conversation");
      open.addEventListener("click", () => { setSection("messages"); selectConversation(run.id, run.source); });
      row.append(open);
    }
    history.append(row);
  }
  detail.append(history);
  detail.scrollTop = scrollTop;
}

async function loadJobRuns(id) {
  const data = await api(`/jobs/${encodeURIComponent(id)}/runs`);
  state.jobRuns[id] = Array.isArray(data) ? data : (data.runs || []);
  if (state.selectedJob === id) renderJobDetail();
}

async function loadJobs() {
  const data = await api("/jobs");
  state.jobs = Array.isArray(data) ? data : (Array.isArray(data.jobs) ? data.jobs : []);
  if (!state.selectedJob || !state.jobs.some((job) => (job.id || job.job_id) === state.selectedJob)) {
    state.selectedJob = state.jobs[0]?.id || state.jobs[0]?.job_id || null;
  }
  renderJobs();
  if (state.selectedJob && !Object.hasOwn(state.jobRuns, state.selectedJob)) loadJobRuns(state.selectedJob).catch((error) => notice(error.message));
}

async function jobAction(id, action) {
  try {
    await api(`/jobs/${encodeURIComponent(id)}/${action}`, { method: "POST" });
    notice(action === "trigger" ? "Wren is running this Job." : `Job ${action === "pause" ? "paused" : "resumed"}.`);
    await loadJobs();
    if (action === "trigger") { delete state.jobRuns[id]; await loadJobRuns(id); }
  } catch (error) { notice(error.message); }
}

async function deleteJob(job) {
  const name = jobValue(job, "name") || "this Job";
  if (!window.confirm(`Delete “${name}”? This cannot be undone.`)) return;
  try {
    await api(`/jobs/${encodeURIComponent(job.id || job.job_id)}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) });
    $("job-dialog").close(); state.selectedJob = null; $("app").classList.remove("job-open"); notice("Job deleted."); await loadJobs();
  } catch (error) { notice(error.message); }
}

function openJob(job = null) {
  state.job = job;
  $("job-dialog-title").textContent = job ? "Edit job" : "New job";
  $("job-name").value = job ? jobValue(job, "name") : "";
  $("job-prompt").value = job ? jobValue(job, "prompt") : "";
  $("job-schedule").value = job ? jobValue(job, "schedule") : "";
  updateSchedulePreview();
  $("job-deliver").value = job ? (jobValue(job, "deliver") || "local") : "local";
  show("job-management", Boolean(job));
  $("job-dialog").showModal();
}

async function saveJob(event) {
  event.preventDefault();
  const data = { name: $("job-name").value.trim(), prompt: $("job-prompt").value.trim(),
                 schedule: $("job-schedule").value.trim(), deliver: $("job-deliver").value.trim() };
  const id = state.job && (state.job.id || state.job.job_id);
  try {
    const result = await api(id ? `/jobs/${encodeURIComponent(id)}` : "/jobs", { method: id ? "PUT" : "POST", body: JSON.stringify(data) });
    $("job-dialog").close(); notice(id ? "Job updated." : "Job created.");
    if (!id) {
      state.selectedJob = result.id || result.job_id || null;
      await loadJobs();
      if (state.selectedJob) selectJob(state.selectedJob);
    } else await loadJobs();
  } catch (error) { notice(error.message); }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/login", { method: "POST", body: JSON.stringify({ password: $("password").value }) });
    await signedIn();
  } catch (error) { $("login-error").textContent = error.message; }
});
$("logout").addEventListener("click", async () => { try { await api("/logout", { method: "POST" }); } finally { signedOut(); } });
$("nav-messages").addEventListener("click", () => setSection("messages"));
$("nav-jobs").addEventListener("click", () => setSection("jobs"));
$("mobile-nav-messages").addEventListener("click", () => setSection("messages"));
$("mobile-nav-jobs").addEventListener("click", () => setSection("jobs"));
$("top-action").addEventListener("click", () => state.section === "jobs" ? openJob() : newConversation());
$("new-from-chat").addEventListener("click", newConversation);
$("new-job").addEventListener("click", () => openJob());
$("back-to-list").addEventListener("click", () => $("app").classList.remove("chat-open"));
$("back-to-jobs").addEventListener("click", () => $("app").classList.remove("job-open"));
$("run-selected-job").addEventListener("click", () => state.selectedJob && jobAction(state.selectedJob, "trigger"));
$("edit-selected-job").addEventListener("click", () => currentJob() && openJob(currentJob()));
$("search").addEventListener("input", () => { state.search = $("search").value.trim().toLowerCase(); renderConversations(); });
$("more-conversations").addEventListener("click", () => loadConversations(true).catch((error) => notice(error.message)));
$("composer").addEventListener("submit", submitMessage);
$("message").addEventListener("input", () => $("composer").classList.toggle("has-text", Boolean($("message").value.trim())));
$("message").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("composer").requestSubmit(); } });
$("job-form").addEventListener("submit", saveJob);
$("job-schedule").addEventListener("input", updateSchedulePreview);
$("close-dialog").addEventListener("click", () => $("job-dialog").close());
$("cancel-job").addEventListener("click", () => $("job-dialog").close());
$("delete-job").addEventListener("click", () => state.job && deleteJob(state.job));

setInterval(() => {
  if ($("app").classList.contains("hidden") || state.section !== "messages" || state.busy) return;
  if (state.current) loadMessages().catch(() => {});
}, 3000);
setInterval(() => {
  if ($("app").classList.contains("hidden")) return;
  if (state.section === "messages") loadConversations().catch(() => {});
  else loadJobs().catch(() => {});
}, 20000);

api("/me").then((data) => data.signed_in ? signedIn() : signedOut()).catch(() => signedOut());
