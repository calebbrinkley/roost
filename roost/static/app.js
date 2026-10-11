"use strict";

const $ = (id) => document.getElementById(id);
const state = { shownTimes: new Set(), renderedConversation: null, section: "messages", current: null, selectedJob: null, jobRuns: {}, conversations: [], jobs: [],
                busy: false, job: null, draft: false, pending: null, search: "" };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function show(id, visible) { $(id).classList.toggle("hidden", !visible); }

// While a reply is on its way, the label rotates through these instead of always saying "typing".
const WORKING_VERBS = ["thinking", "pondering", "mulling it over", "digging in", "connecting the dots",
                       "drafting", "weighing options", "putting it together", "double-checking", "typing"];

function nextWorkingVerb() {
  const choices = WORKING_VERBS.filter((verb) => verb !== state.workingVerb);
  state.workingVerb = choices[Math.floor(Math.random() * choices.length)];
}

function workingLabel(ellipsis = false) {
  const label = el("span", "working-label" + (ellipsis ? " ellipsis" : ""));
  label.textContent = `Wren is ${state.workingVerb || "thinking"}` + (ellipsis ? "…" : "");
  return label;
}

function refreshWorkingLabels() {
  for (const label of document.querySelectorAll(".working-label")) {
    label.textContent = `Wren is ${state.workingVerb}` + (label.classList.contains("ellipsis") ? "…" : "");
  }
}
// The toggle shows where a click takes you: a sun in dark mode, a moon in light mode.
function themeIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2"); svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round");
  const shapes = name === "sun"
    ? [["circle", { cx: 12, cy: 12, r: 4 }], ["path", { d: "M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" }]]
    : [["path", { d: "M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11z" }]];
  for (const [tag, attributes] of shapes) {
    const shape = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) shape.setAttribute(key, String(value));
    svg.append(shape);
  }
  return svg;
}

function refreshThemeControls() {
  const switchToLight = window.roostTheme.current() === "dark";
  const label = switchToLight ? "Switch to light mode" : "Switch to dark mode";
  for (const button of document.querySelectorAll("[data-theme-toggle]")) {
    button.setAttribute("aria-label", label);
    button.title = label;
    button.replaceChildren(themeIcon(switchToLight ? "sun" : "moon"));
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

// A message's full time for its tap-to-reveal line: "2:14 PM", "Yesterday 2:14 PM" or "Oct 9, 2:14 PM".
function messageTime(value) {
  if (!value) return "";
  const date = typeof value === "number" ? new Date(value > 1e11 ? value : value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date(), yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (date.toDateString() === now.toDateString()) return time;
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday " + time;
  const day = date.toLocaleDateString(undefined, { month: "short", day: "numeric", ...(date.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }) });
  return day + ", " + time;
}

function sessionName(item) {
  return item.title || item.name || item.preview || "Conversation";
}

// Hold a conversation (about half a second) or right-click it to rename or delete it.
let longPressed = false;
function consumeLongPress() { const was = longPressed; longPressed = false; return was; }

function holdToAct(node, act) {
  let timer = null, start = null;
  const cancel = () => { clearTimeout(timer); timer = null; };
  node.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    start = { x: event.clientX, y: event.clientY };
    longPressed = false;
    timer = setTimeout(() => { timer = null; longPressed = true; navigator.vibrate?.(10); act(); }, 500);
  });
  node.addEventListener("pointermove", (event) => {
    if (timer && Math.hypot(event.clientX - start.x, event.clientY - start.y) > 10) cancel();
  });
  for (const type of ["pointerup", "pointercancel", "pointerleave"]) node.addEventListener(type, cancel);
  node.addEventListener("contextmenu", (event) => { event.preventDefault(); cancel(); act(); });
}

// Swipe a conversation left on a phone to reveal Delete, as in Mail or Messages. Deleting is permanent
// in Hermes, so both the Delete button and a full swipe go through the confirmation sheet.
const SWIPE_REVEAL = 88;
const phoneWidth = window.matchMedia("(max-width: 700px)");

function setSwipeOpen(id) {
  state.swipeOpenId = id;
  for (const row of document.querySelectorAll(".swipe-row")) row.classList.toggle("revealed", row.dataset.id === id);
}

function confirmDeleteConversation(item) {
  openConversationSheet(item);
  $("sheet-delete").click();
}

function swipeToDelete(wrapper, row, item) {
  let drag = null;
  row.addEventListener("touchstart", (event) => {
    if (!phoneWidth.matches || event.touches.length !== 1) return;
    const touch = event.touches[0];
    drag = { x: touch.clientX, y: touch.clientY, base: wrapper.classList.contains("revealed") ? -SWIPE_REVEAL : 0, active: false, offset: 0 };
  }, { passive: true });
  row.addEventListener("touchmove", (event) => {
    if (!drag) return;
    const dx = event.touches[0].clientX - drag.x, dy = event.touches[0].clientY - drag.y;
    if (!drag.active) {
      if (Math.abs(dy) > 10 && Math.abs(dy) >= Math.abs(dx)) { drag = null; return; }
      if (Math.abs(dx) < 12 || Math.abs(dx) < Math.abs(dy) * 1.5) return;
      drag.active = true; row.classList.add("dragging");
      if (state.swipeOpenId && state.swipeOpenId !== wrapper.dataset.id) setSwipeOpen(null);
    }
    event.preventDefault();
    drag.offset = Math.min(0, drag.base + dx);
    row.style.transform = `translateX(${drag.offset}px)`;
  }, { passive: false });
  const end = () => {
    if (!drag) return;
    const { active, offset } = drag; drag = null;
    if (!active) return;
    row.classList.remove("dragging"); row.style.transform = "";
    // Swallow the click a drag may produce, but never the next real tap.
    row.dataset.swiped = "1"; setTimeout(() => delete row.dataset.swiped, 350);
    if (offset < -wrapper.clientWidth * 0.6) { setSwipeOpen(null); confirmDeleteConversation(item); }
    else setSwipeOpen(offset < -SWIPE_REVEAL / 2 ? wrapper.dataset.id : null);
  };
  row.addEventListener("touchend", end);
  row.addEventListener("touchcancel", end);
}

// A tap anywhere outside the open row closes it.
document.addEventListener("touchstart", (event) => {
  if (state.swipeOpenId && !event.target.closest(".swipe-row.revealed")) setSwipeOpen(null);
}, { passive: true });

function sheetView(name) {
  for (const view of ["sheet-actions", "sheet-rename-form", "sheet-confirm"]) show(view, view === name);
}

function openConversationSheet(item) {
  state.sheetItem = item;
  $("sheet-title").textContent = sessionName(item);
  $("sheet-error").textContent = ""; $("sheet-confirm-error").textContent = "";
  sheetView("sheet-actions");
  if (!$("conversation-sheet").open) $("conversation-sheet").showModal();
  $("sheet-title").focus();
}

async function renameConversation(event) {
  event.preventDefault();
  const item = state.sheetItem, id = item && conversationId(item);
  if (!id) return;
  const title = $("sheet-name").value.trim();
  try {
    const result = await api(`/conversations/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ title }) });
    item.title = result.title ?? title;
    if (state.current === id) $("chat-title").textContent = sessionName(item);
    $("conversation-sheet").close(); renderConversations(); notice("Conversation renamed.");
  } catch (error) { $("sheet-error").textContent = error.message; }
}

async function deleteConversation() {
  const item = state.sheetItem, id = item && conversationId(item);
  if (!id) return;
  try {
    await api(`/conversations/${encodeURIComponent(id)}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) });
    state.conversations = state.conversations.filter((row) => conversationId(row) !== id);
    if (state.swipeOpenId === id) state.swipeOpenId = null;
    if (state.current === id) { state.current = null; $("app").classList.remove("chat-open"); renderWelcome(); }
    $("conversation-sheet").close(); renderConversations(); notice("Conversation deleted.");
  } catch (error) { $("sheet-confirm-error").textContent = error.message; }
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
    const preview = el("span", "row-preview" + (state.pending?.id === id ? " live" : ""));
    preview.append(state.pending?.id === id ? workingLabel(true) : (item.preview || "Conversation with Wren"));
    copy.append(heading, preview);
    button.append(copy);
    const meta = el("span", "row-meta");
    meta.append(el("span", "", timeLabel(item.last_active_at || item.last_active || item.updated_at || item.created_at)));
    if (state.pending?.id === id) meta.append(el("span", "unread-dot"));
    button.append(meta);
    const wrapper = el("div", "swipe-row" + (state.swipeOpenId === id ? " revealed" : ""));
    wrapper.dataset.id = id;
    const remove = el("button", "swipe-delete", "Delete");
    remove.type = "button";
    remove.addEventListener("click", () => { setSwipeOpen(null); confirmDeleteConversation(item); });
    button.addEventListener("click", () => {
      if (consumeLongPress() || button.dataset.swiped) { delete button.dataset.swiped; return; }
      if (state.swipeOpenId) { setSwipeOpen(null); return; }
      selectConversation(id, item.source);
    });
    holdToAct(button, () => openConversationSheet(item));
    swipeToDelete(wrapper, button, item);
    wrapper.append(remove, button);
    list.append(wrapper);
  }
}

async function loadConversations(more = false) {
  const button = $("more-conversations");
  if (more) {
    if (button.disabled) return;
    button.disabled = true; button.textContent = "Loading…";
  }
  try {
    // Page from what is already loaded: Hermes may return fewer rows than Roost asks for.
    const data = await api(`/conversations?offset=${more ? state.conversations.length : 0}`);
    $("presence").textContent = "Online";
    const rows = Array.isArray(data.sessions) ? data.sessions : [];
    if (more) {
      const known = new Set(state.conversations.map(conversationId));
      state.conversations.push(...rows.filter((item) => !known.has(conversationId(item))));
    } else {
      // A refresh replaces the first page but keeps older pages already loaded with Load more.
      const fresh = new Set(rows.map(conversationId));
      state.conversations = [...rows, ...state.conversations.slice(rows.length).filter((item) => !fresh.has(conversationId(item)))];
    }
    finishLoadingConversations(data, rows, more);
  } finally {
    if (more) { button.disabled = false; button.textContent = "Load more"; }
  }
}

function conversationId(item) { return item.session_id || item.id; }

function finishLoadingConversations(data, rows, more) {
  if (!state.current && !state.draft && !more && rows.length) {
    state.current = rows[0].session_id || rows[0].id;
    $("chat-title").textContent = sessionName(rows[0]);
    loadMessages(state.current).catch((error) => notice(error.message));
  }
  renderConversations();
  if (!state.current && !more) renderWelcome();
  if (data.total !== undefined) show("more-conversations", state.conversations.length < data.total && (!more || rows.length > 0));
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

// Bold, code, then italics. An italic marker must hug its text (`2 * 3 * 4` stays as typed), and
// underscores count only at word edges, so snake_case and paths are left alone.
const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\*(?![\s*])[^*\n]*?[^\s*]\*|\*[^\s*]\*|(?<!\w)_(?![\s_])[^_\n]*?[^\s_]_(?!\w)|(?<!\w)_[^\s_]_(?!\w))/g;

function addInline(parent, value) {
  // split() with one capture group puts the matched markup at odd indexes and plain text at even ones.
  value.split(INLINE).forEach((piece, index) => {
    if (!piece) return;
    if (index % 2 === 0) parent.append(document.createTextNode(piece));
    else if (piece.startsWith("**")) parent.append(el("strong", "", piece.slice(2, -2)));
    else if (piece.startsWith("`")) parent.append(el("code", "", piece.slice(1, -1)));
    else parent.append(el("em", "", piece.slice(1, -1)));
  });
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
  const transcript = $("transcript");
  // Follow new messages only when already at the bottom (or opening a conversation); otherwise keep the reader's place.
  const fresh = state.renderedConversation !== state.current;
  const atBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  const keepScroll = transcript.scrollTop;
  state.renderedConversation = state.current;
  if (fresh) state.shownTimes = new Set();
  transcript.replaceChildren();
  const visible = rows.filter((row) => ["user", "assistant"].includes(row.role) && row.display_kind !== "hidden" && messageText(row));
  if (!visible.length) transcript.append(el("p", "empty-transcript", "No messages here yet."));
  for (let index = 0; index < visible.length; index++) {
    const row = visible[index];
    const key = String(row.id ?? index), time = messageTime(row.timestamp);
    const wrapper = el("article", "message " + (row.role === "user" ? "from-you" : "from-wren"));
    const body = el("div", "message-body"); addMarkdown(body, messageText(row)); wrapper.append(body);
    transcript.append(wrapper);
    const shown = time && state.shownTimes.has(key);
    if (row.role === "user") {
      const status = visible.slice(index + 1).some((next) => next.role === "assistant") ? "Read" : "Delivered";
      transcript.append(el("div", "under right", shown ? `${status} · ${time}` : status));
    } else if (shown) transcript.append(el("div", "under", time));
    if (time) {
      body.classList.add("has-time");
      body.addEventListener("click", (event) => {
        // Selecting text or following a link is not a request for the time.
        if (event.target.closest("a") || String(window.getSelection())) return;
        if (state.shownTimes.has(key)) state.shownTimes.delete(key); else state.shownTimes.add(key);
        renderMessages(rows);
      });
    }
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
      typing.append(dots, workingLabel()); transcript.append(typing);
    }
  }
  renderConversations();
  transcript.scrollTop = fresh || atBottom ? transcript.scrollHeight : keepScroll;
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
    nextWorkingVerb();
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

// Hermes stores the schedule as {kind, expr | run_at, display}; the editable form is expr or run_at.
function jobSchedule(job) {
  const schedule = job.schedule;
  if (schedule && typeof schedule === "object") return String(schedule.expr || schedule.run_at || job.schedule_display || schedule.display || "");
  return jobValue(job, "schedule") || jobValue(job, "schedule_display");
}

function jobScheduleLabel(job) {
  const schedule = jobSchedule(job);
  const label = scheduleLabel(schedule);
  return label !== schedule ? label : (jobValue(job, "schedule_display") || schedule);
}

// Hermes treats `enabled` as authoritative; a disabled job is paused.
function jobPaused(job) { return job.paused === true || job.enabled === false; }

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
               el("span", "job-list-schedule", jobScheduleLabel(job)));
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
  if (jobPaused(job)) return { label: "Paused", kind: "paused" };
  if (["claimed", "running"].includes(job.latest_execution?.status)) return { label: "Running now", kind: "ok" };
  if (["ok", "completed"].includes(job.last_status)) return { label: "Ran recently", kind: "ok" };
  if (job.last_status === "delivery_failed") return { label: "Couldn’t deliver", kind: "warn" };
  if (["blocked_config", "needs_attention"].includes(job.last_status)) return { label: "Needs a look", kind: "warn" };
  if (job.last_status === "error") return { label: "Failed", kind: "warn" };
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
  $("job-detail-subtitle").textContent = job ? jobScheduleLabel(job) : "Scheduled work with Wren";
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
  const paused = jobPaused(job);
  const toggle = el("button", "job-toggle" + (paused ? " off" : ""));
  toggle.type = "button"; toggle.setAttribute("role", "switch");
  toggle.setAttribute("aria-checked", String(!paused));
  toggle.setAttribute("aria-label", (paused ? "Resume " : "Pause ") + (jobValue(job, "name") || "job"));
  toggle.addEventListener("click", () => jobAction(job.id || job.job_id, paused ? "resume" : "pause"));
  top.append(toggle);
  summary.append(top, el("h2", "", "Last report"));
  // Hermes keeps no report on the job itself; its newest run's preview is the last report.
  const latestRun = state.jobRuns[job.id || job.job_id]?.[0];
  summary.append(el("div", "job-report", job.last_run_preview || job.last_output || job.last_report || latestRun?.preview ||
                    "No report yet. Wren’s next run will appear here."));
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
  $("job-schedule").value = job ? jobSchedule(job) : "";
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
$("new-conversation").addEventListener("click", newConversation);
$("new-job").addEventListener("click", () => openJob());
$("back-to-list").addEventListener("click", () => $("app").classList.remove("chat-open"));
$("back-to-jobs").addEventListener("click", () => $("app").classList.remove("job-open"));

// Swipe right on a phone to leave a conversation or job, like iOS's back gesture (which installed
// web apps don't get). The list shows underneath while the view follows the finger.
(() => {
  const main = document.querySelector(".main"), app = $("app");
  const phone = window.matchMedia("(max-width: 700px)");
  let drag = null;
  const open = () => phone.matches && (app.classList.contains("chat-open") ||
                                       (app.classList.contains("section-jobs") && app.classList.contains("job-open")));
  const scrollsSideways = (node) => {
    for (; node && node !== main; node = node.parentElement) if (node.scrollWidth > node.clientWidth + 1 && getComputedStyle(node).overflowX !== "visible") return true;
    return false;
  };
  const settle = (to, done) => {
    main.classList.add("settling");
    main.style.transform = to;
    let finished = false;
    const finish = () => {
      if (finished) return; finished = true;
      main.classList.remove("settling", "swiping"); app.classList.remove("peek"); main.style.transform = "";
      if (done) done();
    };
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return finish();
    main.addEventListener("transitionend", finish, { once: true });
    setTimeout(finish, 320);  // in case no transition runs
  };
  main.addEventListener("touchstart", (event) => {
    if (!open() || event.touches.length !== 1 || event.target.closest("textarea, input, dialog") || scrollsSideways(event.target)) return;
    const touch = event.touches[0];
    drag = { x: touch.clientX, y: touch.clientY, time: Date.now(), active: false };
  }, { passive: true });
  main.addEventListener("touchmove", (event) => {
    if (!drag) return;
    const dx = event.touches[0].clientX - drag.x, dy = event.touches[0].clientY - drag.y;
    if (!drag.active) {
      if (Math.abs(dy) > 10 && Math.abs(dy) >= Math.abs(dx)) { drag = null; return; }
      if (dx < 12 || dx < Math.abs(dy) * 1.5) return;
      drag.active = true; app.classList.add("peek"); main.classList.add("swiping");
    }
    event.preventDefault();
    main.style.transform = `translateX(${Math.max(0, dx)}px)`;
  }, { passive: false });
  const end = (event) => {
    if (!drag) return;
    const { x, time, active } = drag; drag = null;
    if (!active) return;
    const dx = (event.changedTouches[0]?.clientX ?? x) - x;
    const flung = dx / Math.max(1, Date.now() - time) > 0.5 && dx > 40;
    if (event.type === "touchend" && (dx > main.clientWidth * 0.35 || flung)) {
      settle("translateX(100%)", () => (app.classList.contains("chat-open") ? $("back-to-list") : $("back-to-jobs")).click());
    } else settle("translateX(0)");
  };
  main.addEventListener("touchend", end);
  main.addEventListener("touchcancel", end);
})();
$("run-selected-job").addEventListener("click", () => state.selectedJob && jobAction(state.selectedJob, "trigger"));
$("edit-selected-job").addEventListener("click", () => currentJob() && openJob(currentJob()));
$("search").addEventListener("input", () => { state.search = $("search").value.trim().toLowerCase(); renderConversations(); });
$("more-conversations").addEventListener("click", () => loadConversations(true).catch((error) => notice(error.message)));
$("composer").addEventListener("submit", submitMessage);
$("sheet-rename").addEventListener("click", () => {
  $("sheet-name").value = state.sheetItem?.title || ""; $("sheet-name").placeholder = sessionName(state.sheetItem || {});
  sheetView("sheet-rename-form"); $("sheet-name").focus();
});
$("sheet-delete").addEventListener("click", () => {
  $("sheet-confirm-title").textContent = `Delete “${sessionName(state.sheetItem || {})}”?`;
  sheetView("sheet-confirm"); $("sheet-confirm-title").focus();
});
$("sheet-rename-form").addEventListener("submit", renameConversation);
$("sheet-confirm-delete").addEventListener("click", deleteConversation);
for (const id of ["sheet-cancel", "sheet-rename-cancel", "sheet-confirm-cancel"]) $(id).addEventListener("click", () => $("conversation-sheet").close());
// A tap on the dimmed backdrop (outside the sheet) closes it.
$("conversation-sheet").addEventListener("click", (event) => {
  const box = $("conversation-sheet").getBoundingClientRect();
  if (event.target === $("conversation-sheet") && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom)) $("conversation-sheet").close();
});
$("message").addEventListener("input", () => $("composer").classList.toggle("has-text", Boolean($("message").value.trim())));
$("message").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("composer").requestSubmit(); } });
$("job-form").addEventListener("submit", saveJob);
$("job-schedule").addEventListener("input", updateSchedulePreview);
$("close-dialog").addEventListener("click", () => $("job-dialog").close());
$("cancel-job").addEventListener("click", () => $("job-dialog").close());
$("delete-job").addEventListener("click", () => state.job && deleteJob(state.job));

setInterval(() => { if (state.pending) { nextWorkingVerb(); refreshWorkingLabels(); } }, 2600);
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
