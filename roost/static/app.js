"use strict";

const $ = (id) => document.getElementById(id);
const state = { shownTimes: new Set(), renderedConversation: null, section: "messages", current: null, selectedJob: null, jobRuns: Object.create(null), jobRunsError: Object.create(null), conversations: [], jobs: [],
                busy: false, jobBusy: false, sheetBusy: false, authEpoch: 0, viewEpoch: 0, jobRunsLoading: new Set(), job: null, draft: false, pending: null, search: "", jobSearch: "", jobPatches: Object.create(null), conversationOffset: 0, conversationsMore: false, loadingMore: false, refreshing: false };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function records(rows) { return rows.filter((row) => row && typeof row === "object" && !Array.isArray(row)); }

function show(id, visible) { $(id).classList.toggle("hidden", !visible); }

// The agent's name comes from the server: Roost's own setting, else Hermes's name for it, else "Agent".
// The markup holds templates for it.
function agent() { return state.agentName || "Agent"; }

function applyAgentName(name) {
  if (name) state.agentName = name;
  for (const node of document.querySelectorAll("[data-agent-text]")) node.textContent = node.dataset.agentText.replaceAll("{agent}", agent());
  for (const node of document.querySelectorAll("[data-agent-placeholder]")) node.placeholder = node.dataset.agentPlaceholder.replaceAll("{agent}", agent());
  for (const node of document.querySelectorAll("[data-agent-initial]")) node.textContent = agent().charAt(0).toUpperCase();
}

// While a reply is on its way, the label rotates through these.
const WORKING_VERBS = ["thinking", "pondering", "mulling it over", "musing", "reflecting",
                       "considering", "contemplating", "turning it over", "ruminating", "deliberating"];

function nextWorkingVerb() {
  const choices = WORKING_VERBS.filter((verb) => verb !== state.workingVerb);
  state.workingVerb = choices[Math.floor(Math.random() * choices.length)];
}

function workingLabel(ellipsis = false) {
  const label = el("span", "working-label" + (ellipsis ? " ellipsis" : ""));
  label.textContent = `${agent()} is ${state.workingVerb || "thinking"}` + (ellipsis ? "…" : "");
  return label;
}

// Hermes saves each tool round as it happens, so the newest row says whether a turn is still going:
// your message or a tool result is waiting on the agent, and a reply that calls tools has more to come.
// A turn that ended in an error, or has been quiet for ten minutes, is over.
function turnInProgress(rows) {
  const last = rows.at(-1);
  if (!last || ["error", "agent_error", "content_filter"].includes(last.finish_reason)) return false;
  if (!(Date.now() / 1000 - Number(last.timestamp) < 600)) return false;
  return last.role === "user" || last.role === "tool"
    || (last.role === "assistant" && Array.isArray(last.tool_calls) && last.tool_calls.length > 0);
}

function refreshWorkingLabels() {
  for (const label of document.querySelectorAll(".working-label")) {
    label.textContent = `${agent()} is ${state.workingVerb}` + (label.classList.contains("ellipsis") ? "…" : "");
  }
}
// Line icons in one style (24 px grid, 2 px round strokes, current text colour).
const ICONS = {
  compose: [["path", { d: "M11 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5" }], ["path", { d: "M17.5 3.5a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4 8.5-8.5z" }]],
  plus: [["path", { d: "M12 5v14M5 12h14" }]],
  gear: [["path", { d: "M10.08 4.85L10.09 2.18L13.91 2.18L13.92 4.85L15.70 5.59L17.59 3.71L20.29 6.41L18.41 8.30L19.15 10.08L21.82 10.09L21.82 13.91L19.15 13.92L18.41 15.70L20.29 17.59L17.59 20.29L15.70 18.41L13.92 19.15L13.91 21.82L10.09 21.82L10.08 19.15L8.30 18.41L6.41 20.29L3.71 17.59L5.59 15.70L4.85 13.92L2.18 13.91L2.18 10.09L4.85 10.08L5.59 8.30L3.71 6.41L6.41 3.71L8.30 5.59Z" }],
         ["circle", { cx: 12, cy: 12, r: 3 }]],
};

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2"); svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round");
  for (const [tag, attributes] of ICONS[name]) {
    const shape = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) shape.setAttribute(key, String(value));
    svg.append(shape);
  }
  return svg;
}

let noticeTimer;
function notice(message) {
  clearTimeout(noticeTimer);
  $("notice").textContent = message;
  show("notice", Boolean(message));
  if (message) noticeTimer = setTimeout(() => { if ($("notice").textContent === message) show("notice", false); }, 6000);
}

async function api(path, options = {}) {
  const epoch = state.authEpoch;
  const response = await fetch("/api" + path, {
    credentials: "same-origin",
    headers: { "X-Roost-Request": "1", ...(options.body ? { "Content-Type": "application/json" } : {}) },
    ...options,
  });
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (epoch !== state.authEpoch) throw new Error("This request belongs to an earlier sign-in.");
  if (!response.ok) {
    if (response.status === 401 && path !== "/login" && path !== "/passkeys/login") { signedOut(); throw new Error("Please sign in again."); }
    throw new Error(data.error || `Request failed (${response.status}).`);
  }
  return data;
}

function signedOut() {
  state.authEpoch++; state.viewEpoch++;
  closeStream();
  Object.assign(state, { current: null, selectedJob: null, conversations: [], jobs: [], jobRuns: Object.create(null), jobRunsError: Object.create(null),
    jobRunsLoading: new Set(), busy: false, jobBusy: false, sheetBusy: false, job: null, sheetItem: null,
    draft: false, pending: null, search: "", jobSearch: "", jobPatches: Object.create(null), conversationOffset: 0, conversationsMore: false, loadingMore: false, swipeOpenId: null, renderedConversation: null, shownTimes: new Set(), openThoughts: new Set(),
    live: null, liveNodes: null, lastRows: null });
  show("app", false); show("login", true);
  Object.assign(state, { settings: null, settingsGroup: null, settingsBusy: false, soul: undefined, soulDraft: undefined, nameDraft: undefined,
                         timeZone: "", about: null, passkeyList: null, passkeyRegister: null, removingPasskey: null,
                         push: null, removingPush: null, hermesLink: null, rotatingToken: false });
  $("app").classList.remove("chat-open", "job-open", "settings-open", "peek");
  for (const id of ["password", "message", "search", "sheet-name", "job-name", "job-prompt", "job-schedule", "job-deliver"]) $(id).value = "";
  for (const id of ["transcript", "conversation-list", "sidebar-jobs-list", "job-detail", "sidebar-settings-list", "settings-detail"]) $(id).replaceChildren();
  for (const id of ["job-dialog", "conversation-sheet", "delivery-sheet"]) if ($(id).open) $(id).close();
  for (const id of ["chat-title", "job-detail-title", "job-detail-subtitle", "sheet-title", "sheet-confirm-title", "presence"]) $(id).textContent = "";
  $("send").disabled = false;
  $("composer").classList.remove("has-text");
  notice("");
  offerPasskeySignIn();
}

// Passkeys (WebAuthn). The browser API takes and returns ArrayBuffers; Roost's API uses base64url.
function passkeysSupported() { return Boolean(window.PublicKeyCredential && window.isSecureContext && navigator.credentials); }

function fromB64url(value) {
  const text = atob(value.replaceAll("-", "+").replaceAll("_", "/") + "===".slice((value.length + 3) % 4));
  return Uint8Array.from(text, (char) => char.charCodeAt(0)).buffer;
}

function toB64url(buffer) {
  let text = "";
  for (const byte of new Uint8Array(buffer)) text += String.fromCharCode(byte);
  return btoa(text).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

// iOS Safari shows the Face ID prompt only straight from a tap, so challenges are fetched ahead of
// time; a tap then goes directly to the prompt. A challenge is good for five minutes on the server.
const PASSKEY_OPTIONS_MS = 240000;

function fresh(prefetched) { return prefetched && Date.now() - prefetched.at < PASSKEY_OPTIONS_MS ? prefetched.options : null; }

async function prefetch(path) {
  try { return { options: await api(path, { method: "POST" }), at: Date.now() }; } catch { return null; }
}

async function offerPasskeySignIn() {
  const offered = Boolean(state.passkeysAvailable && passkeysSupported());
  show("passkey-signin", offered);
  if (offered) state.passkeyLogin = await prefetch("/passkeys/login/options");
}

async function passkeySignIn() {
  $("login-error").textContent = "";
  $("passkey-signin").disabled = true;
  try {
    const options = fresh(state.passkeyLogin) || await api("/passkeys/login/options", { method: "POST" });
    state.passkeyLogin = null;
    const credential = await navigator.credentials.get({ publicKey: {
      challenge: fromB64url(options.challenge), rpId: options.rpId, userVerification: options.userVerification,
      allowCredentials: [], timeout: options.timeout } });
    await api("/passkeys/login", { method: "POST", body: JSON.stringify({
      id: toB64url(credential.rawId), clientDataJSON: toB64url(credential.response.clientDataJSON),
      authenticatorData: toB64url(credential.response.authenticatorData), signature: toB64url(credential.response.signature),
      userHandle: credential.response.userHandle ? toB64url(credential.response.userHandle) : null }) });
    await signedIn();
  } catch (error) {
    $("login-error").textContent = error.name === "NotAllowedError" ? "Passkey sign-in was cancelled or timed out." : error.message;
    if ($("app").classList.contains("hidden")) offerPasskeySignIn();
  } finally { $("passkey-signin").disabled = false; }
}

async function signedIn() {
  show("login", false); show("app", true);
  $("password").value = "";
  $("login-error").textContent = "";
  setSection("messages");
  refreshIdentity();
  try { await loadConversations(); } catch (error) { notice(error.message); }
  // A tapped notification opens Roost at /?c=<conversation>, /?j=<job> or /?d=<delivered message>.
  const search = globalThis.location?.search || "";
  if (search) { history.replaceState(null, "", "/"); openLink(search); }
}

function openLink(search) {
  const link = new URLSearchParams(search), conversation = link.get("c"), job = link.get("j"), delivery = link.get("d");
  if (conversation) { setSection("messages"); selectConversation(conversation); }
  else if (job) showJob(job);
  else if (delivery) openDelivery(delivery);
}

async function showJob(id) {
  setSection("jobs");
  try { await loadJobs(); } catch (error) { notice(error.message); return; }
  if (state.jobs.some((job) => (job.id || job.job_id) === id)) selectJob(id);
  else notice("That job no longer exists.");
}

// A message the agent sent on its own, kept by Roost (Hermes has no transcript of it).
async function openDelivery(id) {
  const epoch = state.authEpoch;
  try {
    const item = await api("/deliveries/" + encodeURIComponent(id));
    if (epoch !== state.authEpoch) return;
    $("delivery-title").textContent = item.title || agent();
    $("delivery-time").textContent = dateTimeLabel(item.created);
    const body = $("delivery-text"); body.replaceChildren(); addMarkdown(body, String(item.text || ""));
    if (!$("delivery-sheet").open) $("delivery-sheet").showModal();
  } catch (error) { if (epoch === state.authEpoch) notice(error.message); }
}

// The agent's name and the synced accent, as the signed-in server sees them.
async function refreshIdentity() {
  const epoch = state.authEpoch;
  try {
    const data = await api("/me");
    if (epoch !== state.authEpoch || !data.signed_in) return;
    applyAgentName(data.agent);
    window.roostTheme.setAccent(data.accent);
    applyTimeZone(data.time_zone);
  } catch { /* The defaults stay until the next sign-in or settings visit. */ }
}

function applyTimeZone(name) {
  if ((name || "") === state.timeZone) return;
  state.timeZone = name || "";
  renderConversations();
  if (state.jobs.length) renderJobs();
  if (state.current) loadMessages().catch(() => {});
}

function setSection(section) {
  state.section = section;
  $("app").classList.toggle("section-jobs", section === "jobs");
  $("app").classList.toggle("section-settings", section === "settings");
  $("app").classList.remove("chat-open", "job-open", "settings-open");
  reportPresence();
  show("messages-view", section === "messages"); show("jobs-view", section === "jobs"); show("settings-view", section === "settings");
  for (const name of ["messages", "jobs", "settings"]) {
    $("nav-" + name).classList.toggle("active", section === name);
    $("mobile-nav-" + name).classList.toggle("active", section === name);
  }
  show("sidebar-search", section !== "settings");
  show("top-action", section !== "settings");
  show("sidebar-settings-list", section === "settings");
  // Both sections share the sidebar's search field and + button; each keeps its own query.
  const jobs = section === "jobs", noun = jobs ? "jobs" : "conversations", create = jobs ? "New job" : "New conversation";
  $("search").value = jobs ? state.jobSearch : state.search;
  $("search").placeholder = $("search-label").textContent = "Search " + noun;
  $("sidebar-new").setAttribute("aria-label", create); $("sidebar-new").title = create;
  show("conversation-list", section === "messages");
  show("sidebar-jobs-list", section === "jobs");
  $("mobile-list-title").textContent = { jobs: "Jobs", settings: "Settings" }[section] || "Messages";
  $("top-action").replaceChildren(icon(section === "jobs" ? "plus" : "compose"));
  $("top-action").setAttribute("aria-label", section === "jobs" ? "New job" : "New conversation");
  $("top-action").title = section === "jobs" ? "New job" : "New conversation";
  if (section === "messages") renderConversations();
  if (section === "jobs") loadJobs().catch((error) => notice(error.message));
  if (section === "settings") loadSettings();
}

// Times show in the zone chosen in Settings, or the device's own when none is chosen (or it is unknown here).
function zone() {
  if (!state.timeZone) return {};
  try { new Intl.DateTimeFormat(undefined, { timeZone: state.timeZone }); return { timeZone: state.timeZone }; }
  catch { return {}; }
}

function parseTime(value) {
  if (!value) return null;
  const date = typeof value === "number" ? new Date(value > 1e11 ? value : value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

// The calendar day in the display zone, as YYYY-MM-DD.
function dayKey(date) { return date.toLocaleDateString("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", ...zone() }); }

function clockTime(date) { return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", ...zone() }); }

function timeLabel(value) {
  const date = parseTime(value);
  if (!date) return "";
  const now = new Date();
  if (dayKey(date) === dayKey(now)) return clockTime(date);
  if (dayKey(date) === dayKey(new Date(now - 86400000))) return "Yesterday";
  if (now - date < 6 * 86400000) return date.toLocaleDateString(undefined, { weekday: "short", ...zone() });
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", ...zone() });
}

// A message's full time for its tap-to-reveal line: "2:14 PM", "Yesterday 2:14 PM" or "Oct 9, 2:14 PM".
function messageTime(value) {
  const date = parseTime(value);
  if (!date) return "";
  const now = new Date(), time = clockTime(date);
  if (dayKey(date) === dayKey(now)) return time;
  if (dayKey(date) === dayKey(new Date(now - 86400000))) return "Yesterday " + time;
  const sameYear = dayKey(date).slice(0, 4) === dayKey(now).slice(0, 4);
  const day = date.toLocaleDateString(undefined, { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }), ...zone() });
  return day + ", " + time;
}

function sessionName(item) {
  return item.title || item.name || withoutInstructions(item.preview) || "Conversation";
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
  const end = (event) => {
    if (!drag) return;
    const { active, offset } = drag; drag = null;
    if (!active) return;
    row.classList.remove("dragging"); row.style.transform = "";
    // Swallow the click a drag may produce, but never the next real tap.
    row.dataset.swiped = "1"; setTimeout(() => delete row.dataset.swiped, 350);
    if (event.type === "touchcancel") { setSwipeOpen(null); return; }
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
  for (const view of ["sheet-actions", "sheet-instructions-form", "sheet-rename-form", "sheet-confirm"]) show(view, view === name);
}

function openConversationSheet(item) {
  if (state.sheetBusy) return;
  state.sheetItem = item;
  $("sheet-title").textContent = sessionName(item);
  $("sheet-error").textContent = ""; $("sheet-confirm-error").textContent = ""; $("sheet-instructions-error").textContent = "";
  $("sheet-export").href = `/api/conversations/${encodeURIComponent(conversationId(item))}/export`;
  sheetView("sheet-actions");
  if (!$("conversation-sheet").open) $("conversation-sheet").showModal();
  $("sheet-title").focus();
}

async function renameConversation(event) {
  event.preventDefault();
  if (state.sheetBusy) return;
  const item = state.sheetItem, id = item && conversationId(item);
  if (!id) return;
  const title = $("sheet-name").value.trim();
  const epoch = state.authEpoch;
  state.sheetBusy = true;
  try {
    const result = await api(`/conversations/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ title }) });
    if (epoch !== state.authEpoch) return;
    item.title = result.title ?? title;
    if (state.current === id) $("chat-title").textContent = sessionName(item);
    $("conversation-sheet").close(); renderConversations(); notice("Conversation renamed.");
  } catch (error) { if (epoch === state.authEpoch) $("sheet-error").textContent = error.message; }
  finally { if (epoch === state.authEpoch) state.sheetBusy = false; }
}

async function openInstructions() {
  const item = state.sheetItem, id = item && conversationId(item);
  if (!id || state.sheetBusy) return;
  const epoch = state.authEpoch, field = $("sheet-instructions-text");
  field.value = ""; field.disabled = true;
  sheetView("sheet-instructions-form");
  try {
    const data = await api(`/conversations/${encodeURIComponent(id)}/settings`);
    if (epoch !== state.authEpoch || state.sheetItem !== item) return;
    field.value = typeof data.instructions === "string" ? data.instructions : "";
    field.disabled = false; field.focus();
  } catch (error) { if (epoch === state.authEpoch) $("sheet-instructions-error").textContent = error.message; }
}

async function saveInstructions(event) {
  event.preventDefault();
  const item = state.sheetItem, id = item && conversationId(item), field = $("sheet-instructions-text");
  if (!id || state.sheetBusy || field.disabled) return;
  const epoch = state.authEpoch;
  state.sheetBusy = true;
  try {
    const data = await api(`/conversations/${encodeURIComponent(id)}/settings`, { method: "PUT", body: JSON.stringify({ instructions: field.value }) });
    if (epoch !== state.authEpoch) return;
    $("conversation-sheet").close();
    notice(data.instructions ? `Instructions saved. ${agent()} gets them with your next message.` : "Instructions cleared.");
  } catch (error) { if (epoch === state.authEpoch) $("sheet-instructions-error").textContent = error.message; }
  finally { if (epoch === state.authEpoch) state.sheetBusy = false; }
}

async function deleteConversation() {
  if (state.sheetBusy) return;
  const item = state.sheetItem, id = item && conversationId(item);
  if (!id) return;
  const epoch = state.authEpoch;
  state.sheetBusy = true;
  try {
    await api(`/conversations/${encodeURIComponent(id)}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) });
    if (epoch !== state.authEpoch) return;
    if (state.pending?.id === id) state.pending = null;
    // The server's rows shift down by one, so the next page starts one row earlier. A load
    // started before the deletion could bring the row back or reuse the old offset: drop it.
    if (state.conversations.some((row) => conversationId(row) === id)) state.conversationOffset = Math.max(0, state.conversationOffset - 1);
    state.conversationRequest = (state.conversationRequest || 0) + 1;
    state.conversations = state.conversations.filter((row) => conversationId(row) !== id);
    if (state.swipeOpenId === id) state.swipeOpenId = null;
    if (state.current === id) { state.viewEpoch++; state.current = null; $("app").classList.remove("chat-open"); renderWelcome(); }
    $("conversation-sheet").close(); renderConversations(); notice("Conversation deleted.");
  } catch (error) { if (epoch === state.authEpoch) $("sheet-confirm-error").textContent = error.message; }
  finally { if (epoch === state.authEpoch) state.sheetBusy = false; }
}

function renderConversations() {
  const list = $("conversation-list"); list.replaceChildren();
  const rows = state.conversations.filter((item) =>
    (sessionName(item) + " " + (withoutInstructions(item.preview) || "") + " " + (item.source || "")).toLowerCase().includes(state.search));
  if (!rows.length) list.append(el("p", "empty-list", state.search ? "No matching conversations." : `No conversations yet. Start one with ${agent()}.`));
  for (const item of rows) {
    const id = item.session_id || item.id;
    if (!id) continue;
    const button = el("button", "conversation-row" + (state.current === id ? " selected" : ""));
    const copy = el("span", "conversation-copy");
    const heading = el("span", "conversation-heading");
    heading.append(el("span", "conversation-name", sessionName(item)));
    if (item.source && item.source !== "roost") heading.append(el("span", "source-tag", item.source));
    const preview = el("span", "row-preview" + (state.pending?.id === id ? " live" : ""));
    preview.append(state.pending?.id === id ? workingLabel(true) : (withoutInstructions(item.preview) || `Conversation with ${agent()}`));
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
  if (state.loadingMore) {
    const loading = el("div", "list-loading");
    loading.setAttribute("role", "status"); loading.setAttribute("aria-label", "Loading older conversations");
    loading.append(el("span", "spinner"));
    list.append(loading);
  }
}

async function loadConversations(more = false) {
  const epoch = state.authEpoch;
  // A refresh waits out a page load; both would otherwise race for the paging offset.
  if (state.loadingMore) return;
  if (more) { state.loadingMore = true; renderConversations(); }
  const serial = state.conversationRequest = (state.conversationRequest || 0) + 1;
  try {
    // Refresh the loaded range, not just its first page: insertions would otherwise
    // push a conversation into an unfetched gap. Page offsets count raw server rows.
    const target = more ? 0 : state.conversations.length;
    let offset = more ? state.conversationOffset : 0, data, rows;
    const loaded = more ? [...state.conversations] : [];
    const known = new Set(loaded.map(conversationId));
    do {
      data = await api(`/conversations?offset=${offset}`);
      if (epoch !== state.authEpoch || serial !== state.conversationRequest) return;
      rows = Array.isArray(data.sessions) ? data.sessions : [];
      offset += rows.length;
      for (const item of rows) {
        const id = conversationId(item);
        if (typeof id === "string" && id && !known.has(id)) { known.add(id); loaded.push(item); }
      }
    } while (!more && rows.length && offset < target && offset <= 100000 &&
             (!Number.isInteger(data.total) || offset < data.total));
    $("presence").textContent = "Online";
    state.conversations = loaded;
    state.conversationOffset = offset;
    state.conversationsMore = rows.length > 0 && offset <= 100000 &&
      (!Number.isInteger(data.total) || offset < data.total);
    finishLoadingConversations(data, loaded, more);
  } finally {
    if (more && epoch === state.authEpoch) { state.loadingMore = false; renderConversations(); }
  }
  if (epoch === state.authEpoch) loadMoreIfNearEnd();
}

// Older conversations load as the list nears its end, or at once when the loaded ones don't fill it.
function loadMoreIfNearEnd() {
  const list = $("conversation-list");
  if (state.section !== "messages" || !state.conversationsMore || state.loadingMore) return;
  if (list.scrollTop + list.clientHeight < list.scrollHeight - 400) return;
  loadConversations(true).catch((error) => notice(error.message));
}

function conversationId(item) { return item?.session_id || item?.id; }

function finishLoadingConversations(data, rows, more) {
  if (!state.current && !state.draft && !more && rows.length) {
    selectConversation(conversationId(rows[0]), rows[0].source);
    // Initial selection stays in the list on phones.
    $("app").classList.remove("chat-open");
    reportPresence();
  }
  renderConversations();
  if (!state.current && !more) renderWelcome();
}

function renderWelcome() {
  show("chat-settings", false);
  const transcript = $("transcript"); transcript.replaceChildren();
  const card = el("div", "welcome-card");
  card.append(el("div", "welcome-symbol", agent().charAt(0).toUpperCase()), el("h2", "", `Message ${agent()}`),
              el("p", "", "A question, an idea, or something to work through. Start wherever you like."));
  transcript.append(card);
  $("chat-title").textContent = "New conversation";
  show("origin-badge", false);
}

// Instructions set for a conversation travel once with the next message, between these lines
// (roost/chats.py writes them); the transcript shows that message as it was typed.
const INSTRUCTIONS_BEGIN = "[Instructions for this conversation, set by the owner in Roost. Follow them until they change.]";
const INSTRUCTIONS_END = "[End of instructions]";

function withoutInstructions(text) {
  // Hermes's list previews are cut short and flattened to one line, so match loosely.
  if (typeof text !== "string" || !text.startsWith(INSTRUCTIONS_BEGIN.slice(0, 40))) return text;
  const end = text.indexOf(INSTRUCTIONS_END, INSTRUCTIONS_BEGIN.length);
  // Cut off inside the instructions: none of the message is left.
  return end < 0 ? "" : text.slice(end + INSTRUCTIONS_END.length).trimStart();
}

function messageText(message) {
  const value = message.display_content ?? message.content;
  if (typeof value === "string") return message.role === "user" ? withoutInstructions(value) : value;
  if (Array.isArray(value)) return value.map((part) => typeof part === "string" ? part : (part?.text || part?.type || "")).join("\n");
  if (value && typeof value === "object") return typeof value.text === "string" ? value.text : JSON.stringify(value);
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

// Table cells take the same bold, code and italics as paragraphs. Code blocks stay literal.
function tableCell(tag, text) {
  const cell = el(tag);
  addInline(cell, text.trim());
  return cell;
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
      for (const cell of lines[i].replace(/^\||\|$/g, "").split("|")) head.append(tableCell("th", cell));
      table.append(head); i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        const row = el("tr");
        for (const cell of lines[i].replace(/^\||\|$/g, "").split("|")) row.append(tableCell("td", cell));
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

// The same field precedence as Hermes's own desktop app.
function reasoningText(row) {
  const value = row.display_reasoning !== undefined ? row.display_reasoning
    : row.reasoning || row.reasoning_content || (typeof row.reasoning_details === "string" ? row.reasoning_details : "");
  return typeof value === "string" ? value.trim() : "";
}

// Collapsed by default; one opened stays open across the transcript's polls.
function thoughtsBlock(texts, key) {
  const details = el("details", "thoughts");
  details.open = state.openThoughts.has(key);
  details.append(el("summary", "", "Thoughts"));
  const body = el("div", "thoughts-body"); addMarkdown(body, texts.join("\n\n")); details.append(body);
  details.addEventListener("toggle", () => { if (details.open) state.openThoughts.add(key); else state.openThoughts.delete(key); });
  return details;
}

function renderMessages(rows) {
  const transcript = $("transcript");
  // Follow new messages only when already at the bottom (or opening a conversation); otherwise keep the reader's place.
  const fresh = state.renderedConversation !== state.current;
  const atBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  const keepScroll = transcript.scrollTop;
  state.renderedConversation = state.current;
  show("chat-settings", Boolean(state.current));
  if (fresh) { state.shownTimes = new Set(); state.openThoughts = new Set(); }
  transcript.replaceChildren();
  state.lastRows = rows; state.liveNodes = null;
  let all = records(rows), live = state.live?.id === state.current ? state.live : null;
  if (live) {
    // The turn being written shows live: the message it answers, then its thoughts and text so far,
    // in place of whatever steps Hermes has saved, until Hermes has saved the finished turn.
    let own = -1;
    for (let i = all.length - 1; i >= 0; i--) if (all[i].role === "user" && messageText(all[i]) === live.user) { own = i; break; }
    const saved = own >= 0 && !turnInProgress(all) && all.slice(own + 1).some((row) => row.role === "assistant" && messageText(row));
    if (live.done && (saved || Date.now() - live.ended > 15000)) { state.live = live = null; }
    else all = own >= 0 ? all.slice(0, own + 1) : [...all, { role: "user", content: live.user }];
  }
  const visible = all.filter((row) => ["user", "assistant"].includes(row.role) && row.display_kind !== "hidden" && messageText(row));
  if (!visible.length) transcript.append(el("p", "empty-transcript", "No messages here yet."));
  // Thoughts from every step since the last message shown, including steps that only called tools,
  // go in one block above the next message; a turn still going shows its thoughts so far at the end.
  const thoughtsBefore = new Map(), shownRows = new Set(visible);
  let thoughts = [], thoughtsKey = null;
  all.forEach((row, index) => {
    const text = row.role === "assistant" && row.display_kind !== "hidden" ? reasoningText(row) : "";
    if (text) { thoughts.push(text); thoughtsKey ??= String(row.id ?? `at ${index}`); }
    if (shownRows.has(row) && thoughts.length) { thoughtsBefore.set(row, [thoughts, thoughtsKey]); thoughts = []; thoughtsKey = null; }
  });
  for (let index = 0; index < visible.length; index++) {
    const row = visible[index];
    if (thoughtsBefore.has(row)) transcript.append(thoughtsBlock(...thoughtsBefore.get(row)));
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
  if (thoughts.length) transcript.append(thoughtsBlock(thoughts, thoughtsKey));
  if (live) transcript.append(...liveNodes(live));
  const working = turnInProgress(records(rows));
  if (state.pending?.id === state.current) {
    // Until Hermes saves the message just sent, the transcript cannot say whether its turn is over.
    let ownMessage = -1;
    for (let i = visible.length - 1; i >= 0; i--) {
      if (visible[i].role === "user" && messageText(visible[i]) === state.pending.text) { ownMessage = i; break; }
    }
    if ((ownMessage >= 0 && !working) || Date.now() - state.pending.since > 600000) state.pending = null;
  }
  if (live ? !live.done : working || state.pending?.id === state.current) {
    const typing = el("div", "typing-row");
    const dots = el("span", "typing-dots"); dots.append(el("i"), el("i"), el("i"));
    typing.append(dots, workingLabel()); transcript.append(typing);
  }
  renderConversations();
  transcript.scrollTop = fresh || atBottom ? transcript.scrollHeight : keepScroll;
}

async function selectConversation(id, source) {
  state.viewEpoch++;
  if (state.current !== id) leaveLive();
  state.current = id;
  state.draft = false;
  $("app").classList.add("chat-open");
  reportPresence();
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
  const epoch = state.authEpoch, view = state.viewEpoch;
  const serial = state.messageRequest = (state.messageRequest || 0) + 1;
  const data = await api(`/conversations/${encodeURIComponent(id)}`);
  // Compare with the newest response applied, not the newest request issued: polls slower
  // than the interval would otherwise discard every reply and freeze the transcript.
  if (epoch === state.authEpoch && view === state.viewEpoch && state.current === id && serial > (state.messageApplied || 0)) {
    state.messageApplied = serial;
    if (data.live === true) openStream(id);
    renderMessages(Array.isArray(data.messages) ? data.messages : []);
  }
}

// A reply being written arrives as an event stream of its thoughts and text. The transcript stays
// the record: a stream that can't open or ends early leaves the turn to polling, as before.
function openStream(id) {
  if (typeof EventSource !== "function" || state.stream?.id === id) return;
  closeStream();
  const source = new EventSource(`/api/conversations/${encodeURIComponent(id)}/stream`);
  const ours = () => state.stream?.source === source;
  state.stream = { id, source };
  source.onmessage = (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (!ours() || !data || typeof data !== "object") return;
    // Every connection, reconnections included, starts with the turn so far.
    if (data.reset) {
      if (state.live?.id !== id || state.live.started !== data.started) state.liveThoughtsOpen = true;
      state.live = { id, user: String(data.user ?? ""), started: data.started, thoughts: "", text: "", done: false };
    }
    if (state.live?.id !== id) return;
    if (typeof data.thoughts === "string") state.live.thoughts += data.thoughts;
    if (typeof data.text === "string") state.live.text += data.text;
    if (data.done) endStream(id);
    else scheduleLive();
  };
  // The browser retries a dropped connection by itself; a refused one (the turn is over) ends here.
  source.onerror = () => { if (ours() && source.readyState === 2) endStream(id); };
}

function endStream(id) {
  closeStream();
  if (state.live?.id === id) Object.assign(state.live, { done: true, ended: Date.now() });
  if (state.current === id) loadMessages(id).catch(() => {});
}

function closeStream() {
  state.stream?.source.close();
  state.stream = null;
}

// Another conversation is opening: nothing live or rendered here belongs to it.
function leaveLive() {
  closeStream();
  Object.assign(state, { live: null, liveNodes: null, lastRows: null });
}

function scheduleLive() {
  if (state.liveFrame) return;
  const frame = window.requestAnimationFrame?.bind(window) || ((callback) => setTimeout(callback, 16));
  state.liveFrame = frame(() => { state.liveFrame = 0; renderLive(); });
}

// Thoughts open while they are written (one closed stays closed for that turn), then the text.
function liveNodes(live) {
  const details = el("details", "thoughts");
  details.open = state.liveThoughtsOpen !== false;
  details.append(el("summary", "", "Thoughts"));
  const thoughts = el("div", "thoughts-body"); details.append(thoughts);
  details.addEventListener("toggle", () => { state.liveThoughtsOpen = details.open; });
  const message = el("article", "message from-wren"), body = el("div", "message-body"); message.append(body);
  state.liveNodes = { details, thoughts, message, body, shownThoughts: null, shownText: null };
  fillLive(live);
  return [details, message];
}

function fillLive(live) {
  const nodes = state.liveNodes;
  if (nodes.shownThoughts !== live.thoughts) { nodes.thoughts.replaceChildren(); addMarkdown(nodes.thoughts, live.thoughts); nodes.shownThoughts = live.thoughts; }
  if (nodes.shownText !== live.text) { nodes.body.replaceChildren(); addMarkdown(nodes.body, live.text); nodes.shownText = live.text; }
  nodes.details.classList.toggle("hidden", !live.thoughts.trim());
  nodes.message.classList.toggle("hidden", !live.text.trim());
}

// Only the live nodes change while text arrives; the rest of the transcript stays as it is.
function renderLive() {
  const live = state.live;
  if (!live || live.id !== state.current || !state.lastRows) return;
  if (!state.liveNodes) return renderMessages(state.lastRows);
  const transcript = $("transcript");
  const atBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  fillLive(live);
  if (atBottom) transcript.scrollTop = transcript.scrollHeight;
}

function newConversation() {
  state.viewEpoch++;
  leaveLive();
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
  const epoch = state.authEpoch, view = state.viewEpoch, oldId = state.current;
  const sameView = () => epoch === state.authEpoch && view === state.viewEpoch && state.current === oldId;
  let accepted = false;
  state.busy = true; $("send").disabled = true; $("message").value = "";
  $("composer").classList.remove("has-text");
  const sending = el("p", "empty-transcript", "Sending…");
  $("transcript").append(sending);
  $("transcript").scrollTop = $("transcript").scrollHeight;
  try {
    const result = oldId
      ? await api(`/conversations/${encodeURIComponent(oldId)}/messages`, { method: "POST", body: JSON.stringify({ text }) })
      : await api("/conversations", { method: "POST", body: JSON.stringify({ text }) });
    if (epoch !== state.authEpoch) return;
    accepted = true;
    if (sameView()) { state.current = result.session_id; state.draft = false; }
    state.pending = { id: result.session_id, text, since: Date.now() };
    if (state.current === result.session_id) openStream(result.session_id);
    nextWorkingVerb();
    // A failed refresh must never turn an accepted message back into an unsent draft.
    const updates = await Promise.allSettled([loadConversations(), loadMessages()]);
    if (epoch === state.authEpoch && updates.some((update) => update.status === "rejected")) {
      notice("Message sent. The conversation could not refresh; it will retry shortly.");
    }
  } catch (error) {
    if (epoch !== state.authEpoch) return;
    if (!accepted && sameView() && !$("message").value) {
      $("message").value = text;
      $("composer").classList.add("has-text");
    }
    notice(error.message);
    if (sameView()) {
      if (oldId) await loadMessages(oldId).catch(() => {}); else renderWelcome();
    }
  } finally {
    sending.remove();
    if (epoch === state.authEpoch) {
      state.busy = false; $("send").disabled = false;
      if (view === state.viewEpoch && state.section === "messages") $("message").focus();
    }
  }
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
  if (!match || Number(match[1]) > 59 || Number(match[2]) > 23 ||
      (match[3] !== "*" && (Number(match[3]) < 1 || Number(match[3]) > 31)) ||
      (match[3] !== "*" && match[4] !== "*")) return schedule;
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
  const rows = state.jobs.filter((job) =>
    [jobValue(job, "name"), jobValue(job, "prompt"), jobScheduleLabel(job)].join(" ").toLowerCase().includes(state.jobSearch));
  if (!rows.length) list.append(el("p", "empty-list", state.jobSearch ? "No matching jobs." : `No jobs yet. Add one for ${agent()} to handle on a schedule.`));
  for (const job of rows) {
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
  $("job-detail-subtitle").textContent = job ? jobScheduleLabel(job) : `Scheduled work with ${agent()}`;
  show("job-detail-actions", Boolean(job));
  if (!job) {
    const welcome = el("div", "job-welcome welcome-card");
    welcome.append(el("div", "welcome-symbol", "◷"), el("h2", "", state.jobs.length ? "Choose a job" : `Give ${agent()} a job`),
                   el("p", "", state.jobs.length ? "Select a job to see its schedule, instructions, and recent runs." : `Set up something ${agent()} can take care of on a schedule.`));
    detail.append(welcome);
    return;
  }
  const status = jobStatus(job);
  const summary = el("section", "job-detail-section job-summary");
  const top = el("div", "job-detail-top");
  top.append(el("span", "pill " + status.kind, status.label));
  const paused = jobPaused(job);
  const toggle = el("button", "job-toggle" + (paused ? " off" : ""));
  toggle.disabled = state.jobBusy;
  for (const id of ["run-selected-job", "edit-selected-job"]) $(id).disabled = state.jobBusy;
  toggle.type = "button"; toggle.setAttribute("role", "switch");
  toggle.setAttribute("aria-checked", String(!paused));
  toggle.setAttribute("aria-label", (paused ? "Resume " : "Pause ") + (jobValue(job, "name") || "job"));
  toggle.addEventListener("click", () => jobAction(job.id || job.job_id, paused ? "resume" : "pause"));
  top.append(toggle);
  summary.append(top, el("h2", "", "Last report"));
  // Hermes keeps no report on the job itself; its newest run's preview is the last report.
  const latestRun = state.jobRuns[job.id || job.job_id]?.[0];
  summary.append(el("div", "job-report", job.last_run_preview || job.last_output || job.last_report || latestRun?.preview ||
                    `No report yet. ${agent()}’s next run will appear here.`));
  detail.append(summary);
  const instructions = el("section", "job-detail-section");
  instructions.append(el("h2", "", `What ${agent()} will do`), el("p", "job-prompt", jobValue(job, "prompt") || "No instructions provided."));
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
  if (!runs) history.append(el("p", "job-history-empty", state.jobRunsError[job.id || job.job_id] ?
    "Recent runs could not load. Use Refresh to try again." : "Loading recent runs…"));
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
  if (state.jobRunsLoading.has(id)) return;
  const epoch = state.authEpoch, loading = state.jobRunsLoading;
  loading.add(id);
  try {
    const data = await api(`/jobs/${encodeURIComponent(id)}/runs`);
    if (epoch !== state.authEpoch) return;
    delete state.jobRunsError[id];
    state.jobRuns[id] = records(Array.isArray(data) ? data : (Array.isArray(data.runs) ? data.runs : []));
    if (state.selectedJob === id) renderJobDetail();
  } catch (error) {
    if (epoch === state.authEpoch) { state.jobRunsError[id] = true; if (state.selectedJob === id) renderJobDetail(); }
    throw error;
  } finally { loading.delete(id); }
}

// Hermes answers a create, edit, pause or resume with the job as saved. Show that at once, and keep
// it over the next few list refreshes: a list read straight after a change has come back without it.
const JOB_PATCH_MS = 30000;

function applyJob(job) {
  const id = job && typeof job === "object" && (job.id || job.job_id);
  if (!id) return;
  state.jobPatches[id] = { job, at: Date.now() };
  state.jobs = withJobPatches(state.jobs);
  renderJobs();
}

function withJobPatches(rows) {
  const now = Date.now(), merged = [...rows];
  for (const [id, patch] of Object.entries(state.jobPatches)) {
    if (now - patch.at > JOB_PATCH_MS) { delete state.jobPatches[id]; continue; }
    const index = merged.findIndex((job) => (job.id || job.job_id) === id);
    if (index >= 0) merged[index] = patch.job; else merged.push(patch.job);
  }
  return merged;
}

async function loadJobs() {
  const epoch = state.authEpoch;
  const serial = state.jobsRequest = (state.jobsRequest || 0) + 1;
  const data = await api("/jobs");
  if (epoch !== state.authEpoch || serial !== state.jobsRequest) return;
  state.jobs = withJobPatches(records(Array.isArray(data) ? data : (Array.isArray(data.jobs) ? data.jobs : [])));
  if (!state.selectedJob || !state.jobs.some((job) => (job.id || job.job_id) === state.selectedJob)) {
    state.selectedJob = state.jobs[0]?.id || state.jobs[0]?.job_id || null;
  }
  renderJobs();
  if (state.selectedJob) await loadJobRuns(state.selectedJob).catch((error) => notice(error.message));
}

async function jobAction(id, action) {
  if (state.jobBusy) return;
  const epoch = state.authEpoch;
  state.jobBusy = true; renderJobDetail();
  try {
    const result = await api(`/jobs/${encodeURIComponent(id)}/${action}`, { method: "POST" });
    if (epoch !== state.authEpoch) return;
    notice(action === "trigger" ? `${agent()} is running this Job.` : `Job ${action === "pause" ? "paused" : "resumed"}.`);
    if (action !== "trigger") applyJob(result);
    await loadJobs();
  } catch (error) { if (epoch === state.authEpoch) notice(error.message); }
  finally { if (epoch === state.authEpoch) { state.jobBusy = false; renderJobDetail(); } }
}

async function deleteJob(job) {
  if (state.jobBusy) return;
  const name = jobValue(job, "name") || "this Job";
  if (!window.confirm(`Delete “${name}”? This cannot be undone.`)) return;
  const epoch = state.authEpoch;
  state.jobBusy = true;
  try {
    await api(`/jobs/${encodeURIComponent(job.id || job.job_id)}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) });
    if (epoch !== state.authEpoch) return;
    delete state.jobRuns[job.id || job.job_id]; delete state.jobRunsError[job.id || job.job_id]; delete state.jobPatches[job.id || job.job_id];
    $("job-dialog").close(); state.selectedJob = null; $("app").classList.remove("job-open"); notice("Job deleted."); await loadJobs();
  } catch (error) { if (epoch === state.authEpoch) notice(error.message); }
  finally { if (epoch === state.authEpoch) { state.jobBusy = false; renderJobDetail(); } }
}

function openJob(job = null) {
  if (state.jobBusy) return;
  // A save cut short by sign-out skips its cleanup, so the form's buttons start enabled here.
  for (const button of $("job-form").querySelectorAll("button")) button.disabled = false;
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
  if (state.jobBusy) return;
  state.jobBusy = true;
  const epoch = state.authEpoch;
  const buttons = [...$("job-form").querySelectorAll("button")];
  buttons.forEach((button) => { button.disabled = true; });
  const data = { name: $("job-name").value.trim(), prompt: $("job-prompt").value.trim(),
                 schedule: $("job-schedule").value.trim(), deliver: $("job-deliver").value.trim() };
  const id = state.job && (state.job.id || state.job.job_id);
  try {
    const result = await api(id ? `/jobs/${encodeURIComponent(id)}` : "/jobs", { method: id ? "PUT" : "POST", body: JSON.stringify(data) });
    if (epoch !== state.authEpoch) return;
    $("job-dialog").close(); notice(id ? "Job updated." : "Job created.");
    applyJob(result);
    if (!id) {
      state.selectedJob = result.id || result.job_id || null;
      await loadJobs();
      if (state.selectedJob) selectJob(state.selectedJob);
    } else await loadJobs();
  } catch (error) { if (epoch === state.authEpoch) notice(error.message); }
  finally {
    if (epoch === state.authEpoch) {
      state.jobBusy = false; buttons.forEach((button) => { button.disabled = false; }); renderJobDetail();
    }
  }
}

// Settings: a list of groups in the sidebar and the selected group in the main pane, like Jobs.
function settingsGroups() {
  return [["appearance", "Appearance", "Theme and accent colour"], ["time", "Time zone", "How times are shown"],
          ["agent", agent(), "Name and instructions"], ["data", "Data", "Delete conversations or jobs"],
          ["service", "Service", `Restart Roost or ${agent()}`], ["notifications", "Notifications", "Replies and job reports on your devices"],
          ["account", "Account", "Passkeys and sign out"],
          ["about", "About", "Version, live status and credits"]];
}

async function loadSettings() {
  const epoch = state.authEpoch;
  if (!state.settingsGroup && !phoneWidth.matches) state.settingsGroup = "appearance";
  renderSettings();
  try {
    const data = await api("/settings");
    if (epoch !== state.authEpoch) return;
    state.settings = data;
    window.roostTheme.setAccent(data.accent);
    renderSettings();
  } catch (error) { if (epoch === state.authEpoch) notice(error.message); }
  if (state.settingsGroup === "agent") loadSoul();
}

function selectSettingsGroup(id) {
  state.settingsGroup = id;
  $("app").classList.add("settings-open");
  renderSettings();
  $("settings-detail").scrollTop = 0;
  if (id === "agent") loadSoul();
  if (id === "about") loadAbout();
  if (id === "account") loadPasskeys();
  if (id === "notifications") { loadPush(); loadHermesLink(); }
}

function renderSettings() {
  const list = $("sidebar-settings-list"); list.replaceChildren();
  for (const [id, label, hint] of settingsGroups()) {
    const row = el("button", "job-list-row" + (state.settingsGroup === id ? " selected" : ""));
    row.type = "button";
    if (state.settingsGroup === id) row.setAttribute("aria-current", "true");
    row.append(el("span", "job-list-name", label), el("span", "settings-row-hint", hint));
    row.addEventListener("click", () => selectSettingsGroup(id));
    list.append(row);
  }
  const group = settingsGroups().find(([id]) => id === state.settingsGroup);
  $("settings-title").textContent = group ? group[1] : "Settings";
  $("settings-subtitle").textContent = group ? group[2] : "";
  const detail = $("settings-detail"); detail.replaceChildren();
  if (!group) return;
  if (!state.settings) { detail.append(el("p", "job-history-empty", "Loading settings…")); return; }
  ({ appearance: renderAppearance, time: renderTimeSettings, agent: renderAgentSettings, data: renderDataSettings,
     service: renderServiceSettings, notifications: renderNotifications, account: renderAccount, about: renderAbout })[group[0]](detail);
}

function settingsSection(parent, heading, text) {
  const section = el("section", "job-detail-section");
  section.append(el("h2", "", heading));
  if (text) section.append(el("p", "", text));
  parent.append(section);
  return section;
}

function settingsButton(className, text, action) {
  const button = el("button", className, text);
  button.type = "button";
  button.disabled = state.settingsBusy;
  button.addEventListener("click", action);
  return button;
}

function renderAppearance(detail) {
  const theme = settingsSection(detail, "Theme", "Applies to this device. System follows the device's light or dark setting.");
  const choices = el("div", "choice-row");
  choices.setAttribute("role", "radiogroup"); choices.setAttribute("aria-label", "Theme");
  for (const [mode, label] of [["system", "System"], ["light", "Light"], ["dark", "Dark"]]) {
    const chosen = window.roostTheme.choice() === mode;
    const button = el("button", "segment" + (chosen ? " active" : ""), label);
    button.type = "button"; button.setAttribute("role", "radio"); button.setAttribute("aria-checked", String(chosen));
    button.addEventListener("click", () => { window.roostTheme.set(mode); renderSettings(); });
    choices.append(button);
  }
  theme.append(choices);
  const text = settingsSection(detail, "Text size", "Applies to this device, across Roost.");
  const sizes = el("div", "choice-row");
  sizes.setAttribute("role", "radiogroup"); sizes.setAttribute("aria-label", "Text size");
  for (const [size, label] of [["small", "Small"], ["default", "Default"], ["large", "Large"], ["larger", "Larger"]]) {
    const chosen = window.roostTheme.textSize() === size;
    const button = el("button", "segment" + (chosen ? " active" : ""), label);
    button.type = "button"; button.setAttribute("role", "radio"); button.setAttribute("aria-checked", String(chosen));
    button.addEventListener("click", () => { window.roostTheme.setTextSize(size); renderSettings(); });
    sizes.append(button);
  }
  text.append(sizes);
  const accent = settingsSection(detail, "Accent colour", "Used for your messages, buttons and highlights on every device you sign in on.");
  const swatches = el("div", "swatches");
  swatches.setAttribute("role", "radiogroup"); swatches.setAttribute("aria-label", "Accent colour");
  for (const name of state.settings.accents) {
    const label = name.charAt(0).toUpperCase() + name.slice(1);
    const swatch = el("button", "swatch swatch-" + name);
    swatch.type = "button"; swatch.title = label; swatch.disabled = state.settingsBusy;
    swatch.setAttribute("role", "radio"); swatch.setAttribute("aria-label", label);
    swatch.setAttribute("aria-checked", String(state.settings.accent === name));
    swatch.addEventListener("click", () => saveSettings({ accent: name }, label + " accent saved."));
    swatches.append(swatch);
  }
  accent.append(swatches);
}

function deviceTimeZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { return ""; }
}

function renderTimeSettings(detail) {
  const section = settingsSection(detail, "Time zone",
    `Conversation, message and job run times show in this zone on every device you sign in on. It doesn’t change when jobs run; ${agent()}’s scheduler keeps its own clock.`);
  const label = el("label", "", "Show times in"); label.htmlFor = "settings-time-zone";
  const select = el("select"); select.id = "settings-time-zone"; select.disabled = state.settingsBusy;
  const device = deviceTimeZone();
  const zones = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  const saved = state.settings.time_zone;
  for (const [value, text] of [["", "This device’s time zone" + (device ? ` (${device.replaceAll("_", " ")})` : "")],
                               ...[...new Set([...zones, ...(saved ? [saved] : [])])].map((name) => [name, name.replaceAll("_", " ")])]) {
    const option = el("option", "", text); option.value = value; option.selected = value === saved;
    select.append(option);
  }
  select.addEventListener("change", () => saveSettings({ time_zone: select.value }, "Time zone saved."));
  const example = el("p", "", "");
  const now = new Date();
  example.textContent = "Now: " + now.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit", timeZoneName: "short", ...zone() });
  section.append(label, select, example);
}

function formatBytes(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "Unknown";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
  return (index && value < 10 ? value.toFixed(1) : Math.round(value)) + " " + units[index];
}

function formatDuration(seconds) {
  if (typeof seconds !== "number") return "Unknown";
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600), minutes = Math.floor(seconds % 3600 / 60);
  return days ? `${days}d ${hours}h` : hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

async function loadAbout() {
  const epoch = state.authEpoch;
  try {
    const data = await api("/about");
    if (epoch !== state.authEpoch) return;
    state.about = { ...data, at: new Date() };
    if (state.section === "settings" && state.settingsGroup === "about") renderSettings();
  } catch (error) { if (epoch === state.authEpoch && !state.about) notice(error.message); }
}

function statList(rows) {
  const list = el("dl", "stats");
  for (const [term, value, note] of rows) {
    const item = el("div");
    const detail = el("dd", "", value);
    if (note) detail.append(" ", el("small", "", note));
    item.append(el("dt", "", term), detail);
    list.append(item);
  }
  return list;
}

function renderAbout(detail) {
  const about = state.about;
  const roost = settingsSection(detail, "Roost", null);
  if (!about) { roost.append(el("p", "", "Loading…")); return; }
  const revision = about.roost.revision;
  roost.append(statList([
    ["Version", about.roost.version],
    ["Build", revision ? revision.slice(0, 7) : "Unknown", revision ? "" : "built without commit information"],
    ["Python", about.runtime.python], ["aiohttp", about.runtime.aiohttp]]));
  if (revision) {
    const source = el("p");
    const link = el("a", "", "View this commit on GitHub");
    link.href = "https://github.com/calebbrinkley/roost/commit/" + encodeURIComponent(revision);
    link.target = "_blank"; link.rel = "noopener noreferrer";
    source.append(link);
    roost.append(source);
  }
  const stats = about.stats;
  const live = settingsSection(detail, "Live", "Roost’s own service, refreshed every few seconds while this page is open.");
  live.append(statList([
    ["Uptime", formatDuration(stats.uptime_seconds)],
    ["CPU", (stats.cpu_percent ?? 0) + "%", "of one core"],
    ["Memory", formatBytes(stats.memory_bytes), stats.memory_limit_bytes ? "of " + formatBytes(stats.memory_limit_bytes) : ""],
    ["Roost on disk", formatBytes((stats.app_bytes || 0) + (stats.state_bytes || 0)),
     "app " + formatBytes(stats.app_bytes) + " · data " + formatBytes(stats.state_bytes)],
    ["Free space", formatBytes(stats.disk_free_bytes), "of " + formatBytes(stats.disk_total_bytes) + " on the shared drive"]]));
  live.append(el("p", "", "Updated " + clockTime(about.at)));
  const hermes = about.hermes;
  const hermesSection = settingsSection(detail, agent() + " (Hermes)", null);
  hermesSection.append(statList(hermes.reachable ? [
    ["Status", hermes.gateway_running ? "Running" : (hermes.gateway_state || "Stopped")],
    ["Response time", hermes.latency_ms + " ms"],
    ["Hermes version", String(hermes.version || "Unknown"), hermes.release_date ? String(hermes.release_date) : ""],
    ["Active sessions", String(hermes.active_sessions ?? "Unknown")]] : [["Status", "Unreachable", "Roost can’t reach the Hermes dashboard"]]));
  const credits = settingsSection(detail, "Credits", null);
  const list = el("ul", "credits");
  for (const [text, href] of [
    ["Roost by calebbrinkley, MIT License", "https://github.com/calebbrinkley/roost"],
    ["Hermes Agent by Nous Research", "https://github.com/NousResearch/hermes-agent"],
    ["Space Grotesk typeface by the Space Grotesk Project Authors, SIL Open Font License 1.1", "https://github.com/floriankarsten/space-grotesk"],
    ["aiohttp by the aio-libs contributors, Apache License 2.0", "https://github.com/aio-libs/aiohttp"]]) {
    const item = el("li"), link = el("a", "", text);
    link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer";
    item.append(link); list.append(item);
  }
  credits.append(list);
}

function renderAgentSettings(detail) {
  const hermesName = state.settings.hermes_agent_name;
  const name = settingsSection(detail, "Name", hermesName ?
    `Leave empty to use the name Hermes gives it: ${hermesName}.` :
    "Hermes has no name for it, so Roost shows “Agent” when this is empty.");
  const label = el("label", "", "Name shown in Roost"); label.htmlFor = "settings-agent-name";
  const input = el("input"); input.id = "settings-agent-name"; input.maxLength = 40; input.autocomplete = "off";
  input.value = state.nameDraft ?? state.settings.agent_name; input.placeholder = hermesName || "Agent";
  input.addEventListener("input", () => { state.nameDraft = input.value; });
  const actions = el("div", "settings-actions");
  actions.append(settingsButton("primary", "Save name", async () => {
    if (await saveSettings({ agent_name: input.value.trim() }, "Name saved.")) { state.nameDraft = undefined; await refreshIdentity(); renderSettings(); }
  }));
  name.append(label, input, actions);

  const soul = settingsSection(detail, "Instructions",
    `What ${agent()} should know and how it should behave, everywhere it works: Roost, the desktop app and scheduled jobs. Hermes keeps these in its SOUL.md file.`);
  const soulLabel = el("label", "", "Instructions"); soulLabel.htmlFor = "settings-soul";
  const area = el("textarea"); area.id = "settings-soul"; area.maxLength = 16000;
  const loaded = state.soul !== undefined;
  area.value = loaded ? (state.soulDraft ?? state.soul) : ""; area.disabled = !loaded;
  area.placeholder = loaded ? `For example: Keep answers short. Call me by my first name.` : "Loading…";
  area.addEventListener("input", () => { state.soulDraft = area.value; });
  const soulActions = el("div", "settings-actions");
  const save = settingsButton("primary", "Save instructions", saveSoul);
  save.disabled = state.settingsBusy || !loaded;
  soulActions.append(save);
  soul.append(soulLabel, area, soulActions);
}

function renderDataSettings(detail) {
  for (const [kind, heading, text] of [
    ["conversations", "Delete all conversations", `Removes every conversation in Messages, whatever started it, from ${agent()}’s history for good. Job run history stays with its jobs.`],
    ["jobs", "Delete all jobs", `Removes every scheduled job and stops it running. This can’t be undone.`]]) {
    const section = settingsSection(detail, heading, text);
    const label = el("label", "", "Type DELETE to confirm"); label.htmlFor = "confirm-" + kind;
    const input = el("input"); input.id = "confirm-" + kind; input.autocomplete = "off"; input.autocapitalize = "characters";
    const actions = el("div", "settings-actions");
    const button = settingsButton("danger-button", heading, () => deleteEverything(kind, input));
    button.disabled = true;
    input.addEventListener("input", () => { button.disabled = state.settingsBusy || input.value.trim() !== "DELETE"; });
    actions.append(button);
    section.append(label, input, actions);
  }
}

function deviceName() {
  const agent = navigator.userAgent || "";
  if (/iPhone/.test(agent)) return "iPhone";
  if (/iPad/.test(agent) || (/Macintosh/.test(agent) && navigator.maxTouchPoints > 1)) return "iPad";
  if (/Macintosh/.test(agent)) return "Mac";
  if (/Android/.test(agent)) return "Android phone";
  if (/Windows/.test(agent)) return "Windows PC";
  return "This device";
}

async function loadPasskeys() {
  const epoch = state.authEpoch;
  try {
    const data = await api("/passkeys");
    if (epoch !== state.authEpoch) return;
    state.passkeyList = data.passkeys;
    state.passkeysAvailable = data.passkeys.length > 0;
    if (state.settingsGroup === "account") renderSettings();
    if (passkeysSupported()) state.passkeyRegister = await prefetch("/passkeys/register/options");
  } catch (error) { if (epoch === state.authEpoch) notice(error.message); }
}

function dateLabel(seconds) {
  return new Date(seconds * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric", ...zone() });
}

function dateTimeLabel(seconds) {
  return new Date(seconds * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", ...zone() });
}

function renderPasskeys(detail) {
  const section = settingsSection(detail, "Passkeys",
    "Sign in with Face ID, Touch ID or your device’s PIN instead of your password. Your password keeps working.");
  if (!state.passkeyList) { section.append(el("p", "", "Loading…")); return; }
  if (state.passkeyList.length) {
    const list = el("ul", "passkey-list");
    for (const passkey of state.passkeyList) {
      const item = el("li"), text = el("div");
      text.append(el("strong", "", passkey.name),
                  el("small", "", "Added " + dateLabel(passkey.created) + (passkey.last_used ? " · last used " + dateLabel(passkey.last_used) : " · not used yet")));
      const confirming = state.removingPasskey === passkey.id;
      const remove = settingsButton("secondary" + (confirming ? " confirming" : ""), confirming ? "Confirm remove" : "Remove",
        () => confirming ? removePasskey(passkey) : (state.removingPasskey = passkey.id, renderSettings()));
      remove.setAttribute("aria-label", (confirming ? "Confirm removing " : "Remove ") + passkey.name);
      item.append(text, remove);
      list.append(item);
    }
    section.append(list);
  }
  if (!passkeysSupported()) {
    section.append(el("p", "", "This browser can’t add passkeys here. Open Roost in Safari, Chrome or Edge over HTTPS."));
    return;
  }
  const label = el("label", "", "Name for a new passkey"); label.htmlFor = "passkey-name";
  const input = el("input"); input.id = "passkey-name"; input.maxLength = 40; input.autocomplete = "off";
  input.value = state.passkeyNameDraft ?? deviceName();
  input.addEventListener("input", () => { state.passkeyNameDraft = input.value; });
  const actions = el("div", "settings-actions");
  actions.append(settingsButton("primary", "Add a passkey", () => addPasskey(input.value.trim())));
  section.append(label, input, actions);
}

function addPasskey(name) {
  return withSettingsBusy(async (epoch) => {
    const options = fresh(state.passkeyRegister) || await api("/passkeys/register/options", { method: "POST" });
    state.passkeyRegister = null;
    let credential;
    const excluded = options.excludeCredentials.map((item) => ({ ...item, id: fromB64url(item.id) }));
    try {
      credential = await navigator.credentials.create({ publicKey: {
        ...options, challenge: fromB64url(options.challenge), user: { ...options.user, id: fromB64url(options.user.id) },
        excludeCredentials: excluded } });
    } catch (error) {
      // The prompt didn't finish: fetch a fresh challenge so the next tap goes straight to it.
      if (epoch === state.authEpoch) loadPasskeys();
      if (error.name === "InvalidStateError") throw new Error("This device already has a passkey for Roost.");
      if (error.name === "NotAllowedError") throw new Error("Adding the passkey was cancelled or timed out.");
      throw error;
    }
    await api("/passkeys", { method: "POST", body: JSON.stringify({
      name, id: toB64url(credential.rawId), clientDataJSON: toB64url(credential.response.clientDataJSON),
      attestationObject: toB64url(credential.response.attestationObject) }) });
    if (epoch !== state.authEpoch) return;
    state.passkeyNameDraft = undefined;
    notice(`Passkey added. Next time, sign in with ${name}.`);
    await loadPasskeys();
  });
}

function removePasskey(passkey) {
  return withSettingsBusy(async (epoch) => {
    await api("/passkeys/" + encodeURIComponent(passkey.id), { method: "DELETE", body: JSON.stringify({ confirm: true }) });
    if (epoch !== state.authEpoch) return;
    state.removingPasskey = null;
    notice(`Removed the passkey “${passkey.name}”. Also delete it from the device’s passwords to stop it being offered.`);
    await loadPasskeys();
  });
}

function renderAccount(detail) {
  renderPasskeys(detail);
  const section = settingsSection(detail, "Sign out",
    "Signs this device out of Roost. Other devices stay signed in. Sign back in with your password or a passkey.");
  const actions = el("div", "settings-actions");
  actions.append(settingsButton("secondary", "Sign out", signOut));
  section.append(actions);
}

// A failed sign-out keeps the session and says why; the session ends only when the server agrees.
async function signOut() {
  if (state.settingsBusy) return;
  state.settingsBusy = true; renderSettings();
  try { await api("/logout", { method: "POST" }); signedOut(); }
  catch (error) { notice(error.message); state.settingsBusy = false; renderSettings(); }
}

function renderServiceSettings(detail) {
  const agentSection = settingsSection(detail, `Restart ${agent()}`,
    `Restarts the Hermes gateway: its messaging platforms and the job scheduler. A reply that is being written may stop.`);
  const agentActions = el("div", "settings-actions");
  agentActions.append(settingsButton("secondary", `Restart ${agent()}`, () =>
    serviceAction("/service/restart-agent", `${agent()} is restarting. Replies may take a minute.`)));
  agentSection.append(agentActions);
  const roostSection = settingsSection(detail, "Restart Roost", "Restarts this web app. You stay signed in.");
  const roostActions = el("div", "settings-actions");
  roostActions.append(settingsButton("secondary", "Restart Roost", restartRoost));
  roostSection.append(roostActions);
}

// Notifications: the server watches for the end of each reply sent from Roost and pushes it to
// every device that turned them on, unless an open Roost has that conversation on screen.
const serviceWorker = globalThis.isSecureContext && navigator.serviceWorker
  ? navigator.serviceWorker.register("/sw.js").catch(() => null) : Promise.resolve(null);

function pushSupported() { return Boolean(globalThis.isSecureContext && navigator.serviceWorker && globalThis.PushManager && globalThis.Notification); }

// This browser's subscription, and its id on the server (a hash of the endpoint, as the server makes it).
async function thisDevicePush() {
  const registration = pushSupported() ? await serviceWorker : null;
  const subscription = registration ? await registration.pushManager.getSubscription() : null;
  if (!subscription) return null;
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(subscription.endpoint)));
  return { subscription, id: Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("").slice(0, 24) };
}

async function loadPush() {
  const epoch = state.authEpoch;
  try {
    const [data, mine] = await Promise.all([api("/push"), thisDevicePush().catch(() => null)]);
    if (epoch !== state.authEpoch) return;
    state.push = { ...data, mine: mine?.id || null };
    if (state.settingsGroup === "notifications") renderSettings();
  } catch (error) { if (epoch === state.authEpoch) notice(error.message); }
}

function renderNotifications(detail) {
  const section = settingsSection(detail, "This device", `Get a notification when ${agent()} finishes a reply you aren’t looking at.`);
  if (!state.push) { section.append(el("p", "", "Loading…")); return; }
  const on = state.push.devices.some((device) => device.id === state.push.mine);
  if (!pushSupported()) {
    section.append(el("p", "", ["iPhone", "iPad"].includes(deviceName())
      ? "On iPhone and iPad, notifications work once Roost is on the Home Screen: in Safari, tap Share, then Add to Home Screen, and open Roost from there."
      : "This browser can’t receive notifications from Roost."));
  } else if (Notification.permission === "denied" && !on) {
    section.append(el("p", "", "Notifications are blocked for Roost in this device’s settings. Allow them there, then come back."));
  } else {
    section.append(el("p", "", on ? "Notifications are on for this device." : "Notifications are off for this device."));
    const actions = el("div", "settings-actions");
    if (on) actions.append(settingsButton("primary", "Send a test", () => testPush(state.push.mine)), settingsButton("secondary", "Turn off", disablePush));
    else actions.append(settingsButton("primary", "Turn on", enablePush));
    section.append(actions);
  }
  renderHermesLink(detail);
  if (!state.push.devices.length) return;
  const devices = settingsSection(detail, "Devices", "Every device that gets Roost’s notifications.");
  const list = el("ul", "passkey-list");
  for (const device of state.push.devices) {
    const item = el("li"), text = el("div");
    text.append(el("strong", "", device.name + (device.id === state.push.mine ? " (this device)" : "")),
                el("small", "", "Added " + dateLabel(device.created) + (device.last_sent ? " · last notified " + dateLabel(device.last_sent) : " · not notified yet")));
    const confirming = state.removingPush === device.id;
    const remove = settingsButton("secondary" + (confirming ? " confirming" : ""), confirming ? "Confirm remove" : "Remove",
      () => confirming ? removePushDevice(device) : (state.removingPush = device.id, renderSettings()));
    remove.setAttribute("aria-label", (confirming ? "Confirm removing " : "Remove ") + device.name);
    item.append(text, remove);
    list.append(item);
  }
  devices.append(list);
}

async function loadHermesLink() {
  const epoch = state.authEpoch;
  try {
    const data = await api("/hermes");
    if (epoch !== state.authEpoch) return;
    state.hermesLink = data;
    if (state.settingsGroup === "notifications") renderSettings();
  } catch (error) { if (epoch === state.authEpoch) notice(error.message); }
}

// Job reports and messages the agent starts reach Roost through a plugin installed in Hermes.
function renderHermesLink(detail) {
  const link = state.hermesLink;
  const section = settingsSection(detail, "Job reports from " + agent(),
    `Install Roost’s plugin in Hermes to get ${agent()}’s job reports and the messages it sends on its own here, with a notification.`);
  if (!link) { section.append(el("p", "", "Loading…")); return; }
  section.append(el("p", "", link.last_delivery ? "Last report received " + dateTimeLabel(link.last_delivery) + "." : "Nothing received from Hermes yet."));
  const steps = el("ol", "setup-steps");
  const download = el("a", "", "Download the plugin"); download.href = "/api/hermes/plugin.zip"; download.setAttribute("download", "");
  const first = el("li"); first.append(download, " and unzip it into Hermes’s plugins folder, so it becomes ", el("code", "", "$HERMES_HOME/plugins/roost"), ".");
  const second = el("li"); second.append("Enable it: ", el("code", "", "hermes plugins enable roost"), ".");
  const third = el("li"); third.append("Add these lines to Hermes’s ", el("code", "", ".env"), ". Use the address Hermes reaches Roost at if it differs.");
  third.append(el("pre", "setup-block", `ROOST_URL=${link.url}\nROOST_DELIVERY_TOKEN=${link.token}\nROOST_HOME_CHANNEL=roost`));
  const fourth = el("li"); fourth.append("Restart Hermes’s gateway. Then set a job’s Deliver to ", el("code", "", "roost"), ".");
  steps.append(first, second, third, fourth);
  section.append(steps);
  const confirming = state.rotatingToken;
  const rotate = settingsButton("secondary" + (confirming ? " confirming" : ""), confirming ? "Confirm new token" : "New token",
    () => confirming ? rotateDeliveryToken() : (state.rotatingToken = true, renderSettings()));
  const actions = el("div", "settings-actions"); actions.append(rotate);
  section.append(actions);
}

function rotateDeliveryToken() {
  return withSettingsBusy(async (epoch) => {
    const data = await api("/hermes/token", { method: "POST", body: JSON.stringify({ confirm: true }) });
    if (epoch !== state.authEpoch) return;
    state.hermesLink = data; state.rotatingToken = false;
    notice("New token made. Update ROOST_DELIVERY_TOKEN in Hermes; the old one no longer works.");
  });
}

function enablePush() {
  return withSettingsBusy(async (epoch) => {
    // First, straight from the tap: Safari shows the permission prompt only for one.
    if (await Notification.requestPermission() !== "granted") throw new Error("Notifications weren’t allowed, so they stay off.");
    const registration = await serviceWorker;
    if (!registration) throw new Error("Roost couldn’t start its notification worker. Reload and try again.");
    await navigator.serviceWorker.ready;
    const options = { userVisibleOnly: true, applicationServerKey: fromB64url(state.push.public_key) };
    let subscription;
    try { subscription = await registration.pushManager.subscribe(options); }
    catch {
      // A subscription made for an earlier Roost key can't be reused: replace it.
      await (await registration.pushManager.getSubscription())?.unsubscribe();
      subscription = await registration.pushManager.subscribe(options);
    }
    await api("/push/devices", { method: "POST", body: JSON.stringify({ subscription: subscription.toJSON(), name: deviceName() }) });
    if (epoch !== state.authEpoch) return;
    notice("Notifications are on for this device.");
    await loadPush();
  });
}

function disablePush() {
  return withSettingsBusy(async (epoch) => {
    const mine = await thisDevicePush();
    if (mine && state.push.devices.some((device) => device.id === mine.id)) {
      await api("/push/devices/" + encodeURIComponent(mine.id), { method: "DELETE", body: JSON.stringify({ confirm: true }) });
    }
    await mine?.subscription.unsubscribe();
    if (epoch !== state.authEpoch) return;
    notice("Notifications are off for this device.");
    await loadPush();
  });
}

function removePushDevice(device) {
  return withSettingsBusy(async (epoch) => {
    await api("/push/devices/" + encodeURIComponent(device.id), { method: "DELETE", body: JSON.stringify({ confirm: true }) });
    if (epoch !== state.authEpoch) return;
    state.removingPush = null;
    notice(`${device.name} won’t get Roost’s notifications.`);
    await loadPush();
  });
}

function testPush(device) {
  return withSettingsBusy(async () => {
    await api("/push/test", { method: "POST", body: JSON.stringify({ device }) });
    notice("Test sent. It should arrive in a few seconds.");
  });
}

// Tells the server which conversation is on screen, so a reply there doesn't also notify.
// A plain fetch: it runs as the page hides, and a failure only means a notification may show.
function reportPresence() {
  if ($("app").classList.contains("hidden")) return;
  // On a phone the list and the chat take turns; only an open chat is on screen.
  const onScreen = document.visibilityState === "visible" && state.section === "messages"
    && (!phoneWidth.matches || $("app").classList.contains("chat-open"));
  const conversation = onScreen ? state.current : null;
  fetch("/api/presence", { method: "POST", keepalive: true, credentials: "same-origin",
    headers: { "X-Roost-Request": "1", "Content-Type": "application/json" }, body: JSON.stringify({ conversation }) }).catch(() => {});
}

async function withSettingsBusy(work) {
  if (state.settingsBusy) return false;
  const epoch = state.authEpoch;
  state.settingsBusy = true; renderSettings();
  try { await work(epoch); return epoch === state.authEpoch; }
  catch (error) { if (epoch === state.authEpoch) notice(error.message); return false; }
  finally { if (epoch === state.authEpoch) { state.settingsBusy = false; renderSettings(); } }
}

function saveSettings(changes, message) {
  return withSettingsBusy(async (epoch) => {
    const data = await api("/settings", { method: "PATCH", body: JSON.stringify(changes) });
    if (epoch !== state.authEpoch) return;
    state.settings = data;
    window.roostTheme.setAccent(data.accent);
    applyTimeZone(data.time_zone);
    notice(message);
  });
}

async function loadSoul() {
  if (state.soul !== undefined || state.soulLoading) return;
  const epoch = state.authEpoch;
  state.soulLoading = true;
  try {
    const data = await api("/settings/soul");
    if (epoch !== state.authEpoch) return;
    state.soul = data.content;
    if (state.settingsGroup === "agent") renderSettings();
  } catch (error) { if (epoch === state.authEpoch) notice(error.message); }
  finally { state.soulLoading = false; }
}

function saveSoul() {
  const content = $("settings-soul").value;
  return withSettingsBusy(async (epoch) => {
    await api("/settings/soul", { method: "PUT", body: JSON.stringify({ content }) });
    if (epoch !== state.authEpoch) return;
    state.soul = content; state.soulDraft = undefined;
    notice("Instructions saved.");
  });
}

function deleteEverything(kind, input) {
  if (input.value.trim() !== "DELETE") return;
  return withSettingsBusy(async (epoch) => {
    const result = await api("/" + kind, { method: "DELETE", body: JSON.stringify({ confirm: "DELETE" }) });
    if (epoch !== state.authEpoch) return;
    if (kind === "conversations") {
      Object.assign(state, { conversations: [], current: null, pending: null, conversationOffset: 0, conversationsMore: false });
      state.viewEpoch++; renderConversations(); renderWelcome();
    } else {
      Object.assign(state, { jobs: [], selectedJob: null, jobPatches: Object.create(null), jobRuns: Object.create(null), jobRunsError: Object.create(null) });
      renderJobs();
    }
    const count = Number(result.deleted) || 0;
    notice(`Deleted ${count} ${kind === "jobs" ? (count === 1 ? "job" : "jobs") : (count === 1 ? "conversation" : "conversations")}.`);
  });
}

function serviceAction(path, message) {
  return withSettingsBusy(async (epoch) => {
    await api(path, { method: "POST" });
    if (epoch === state.authEpoch) notice(message);
  });
}

function restartRoost() {
  return withSettingsBusy(async (epoch) => {
    await api("/service/restart-roost", { method: "POST" });
    if (epoch !== state.authEpoch) return;
    notice("Roost is restarting…");
    // Wait for the old process to go, then for the new one to answer.
    await new Promise((resolve) => setTimeout(resolve, 1500));
    for (let attempt = 0; attempt < 30; attempt++) {
      try { if ((await fetch("/api/health", { cache: "no-store" })).ok) { notice("Roost is back."); return; } } catch { /* still starting */ }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    notice("Roost hasn’t come back yet. Reload the page in a minute.");
  });
}

$("passkey-signin").addEventListener("click", passkeySignIn);
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/login", { method: "POST", body: JSON.stringify({ password: $("password").value }) });
    await signedIn();
  } catch (error) { $("login-error").textContent = error.message; }
});
$("nav-messages").addEventListener("click", () => setSection("messages"));
$("nav-jobs").addEventListener("click", () => setSection("jobs"));
$("mobile-nav-messages").addEventListener("click", () => setSection("messages"));
$("mobile-nav-jobs").addEventListener("click", () => setSection("jobs"));
$("nav-settings").append(icon("gear")); $("mobile-nav-settings").append(icon("gear")); $("chat-settings").append(icon("gear"));
$("nav-settings").addEventListener("click", () => setSection("settings"));
$("mobile-nav-settings").addEventListener("click", () => setSection("settings"));
$("back-to-settings").addEventListener("click", () => $("app").classList.remove("settings-open"));
$("top-action").addEventListener("click", () => state.section === "jobs" ? openJob() : newConversation());
$("sidebar-new").addEventListener("click", () => state.section === "jobs" ? openJob() : newConversation());
$("back-to-list").addEventListener("click", () => { $("app").classList.remove("chat-open"); reportPresence(); });
$("back-to-jobs").addEventListener("click", () => $("app").classList.remove("job-open"));

// Swipe right on a phone to leave a conversation or job, like iOS's back gesture (which installed
// web apps don't get). The list shows underneath while the view follows the finger.
(() => {
  const main = document.querySelector(".main"), app = $("app");
  const phone = window.matchMedia("(max-width: 700px)");
  let drag = null;
  const open = () => phone.matches && (app.classList.contains("chat-open") ||
                                       (app.classList.contains("section-jobs") && app.classList.contains("job-open")) ||
                                       (app.classList.contains("section-settings") && app.classList.contains("settings-open")));
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
      const back = app.classList.contains("chat-open") ? "back-to-list" : app.classList.contains("section-settings") ? "back-to-settings" : "back-to-jobs";
      settle("translateX(100%)", () => $(back).click());
    } else settle("translateX(0)");
  };
  main.addEventListener("touchend", end);
  main.addEventListener("touchcancel", end);
})();
$("run-selected-job").addEventListener("click", () => state.selectedJob && jobAction(state.selectedJob, "trigger"));
$("edit-selected-job").addEventListener("click", () => currentJob() && openJob(currentJob()));
$("search").addEventListener("input", () => {
  const query = $("search").value.trim().toLowerCase();
  if (state.section === "jobs") { state.jobSearch = query; renderJobs(); } else { state.search = query; renderConversations(); }
});
$("conversation-list").addEventListener("scroll", loadMoreIfNearEnd, { passive: true });

// Pull to refresh on phones: drag Messages or Jobs down from the top, past the threshold, and let go.
// The spinner holds in place while the list reloads. Sideways drags (swipe to delete) are left alone.
const PULL_THRESHOLD = 64, PULL_HOLD = 52, PULL_MAX = 110, PULL_MINIMUM_MS = 600;
const pullIndicator = $("pull-indicator"), pullSpinner = el("span", "spinner");
pullIndicator.append(pullSpinner);

function settlePull(height) {
  pullIndicator.classList.add("settling");
  pullIndicator.style.height = height + "px";
  setTimeout(() => pullIndicator.classList.remove("settling"), 260);
}

async function refreshFromPull() {
  state.refreshing = true;
  const epoch = state.authEpoch, began = Date.now();
  pullIndicator.classList.remove("armed"); pullIndicator.classList.add("refreshing");
  settlePull(PULL_HOLD);
  try { await (state.section === "jobs" ? loadJobs() : loadConversations()); }
  catch (error) { if (epoch === state.authEpoch) notice(error.message); }
  // A very fast reload still shows the spinner long enough to read as "refreshed".
  await new Promise((resolve) => setTimeout(resolve, Math.max(0, PULL_MINIMUM_MS - (Date.now() - began))));
  state.refreshing = false;
  pullIndicator.classList.remove("refreshing");
  settlePull(0);
}

function pullToRefresh(list) {
  let drag = null;
  list.addEventListener("touchstart", (event) => {
    if (!phoneWidth.matches || state.refreshing || list.scrollTop > 0 || event.touches.length !== 1) return;
    drag = { x: event.touches[0].clientX, y: event.touches[0].clientY, pull: 0, vertical: false };
  }, { passive: true });
  list.addEventListener("touchmove", (event) => {
    if (!drag) return;
    const dx = event.touches[0].clientX - drag.x, dy = event.touches[0].clientY - drag.y;
    if (!drag.vertical) {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
      if (dy <= 0 || Math.abs(dx) > Math.abs(dy)) { drag = null; return; }
      drag.vertical = true;
    }
    event.preventDefault();
    drag.pull = Math.min(PULL_MAX, Math.max(0, dy * 0.5));
    pullIndicator.style.height = drag.pull + "px";
    pullSpinner.style.transform = `rotate(${drag.pull * 4}deg)`;
    pullIndicator.classList.toggle("armed", drag.pull >= PULL_THRESHOLD);
  }, { passive: false });
  const end = (event) => {
    if (!drag) return;
    const { pull } = drag; drag = null;
    pullSpinner.style.transform = "";
    if (event.type === "touchend" && pull >= PULL_THRESHOLD) refreshFromPull();
    else { pullIndicator.classList.remove("armed"); settlePull(0); }
  };
  list.addEventListener("touchend", end);
  list.addEventListener("touchcancel", end);
}
pullToRefresh($("conversation-list"));
pullToRefresh($("sidebar-jobs-list"));
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
$("sheet-instructions").addEventListener("click", openInstructions);
$("sheet-instructions-form").addEventListener("submit", saveInstructions);
$("sheet-export").addEventListener("click", () => setTimeout(() => $("conversation-sheet").close(), 0));
$("chat-settings").addEventListener("click", () => {
  if (!state.current) return;
  openConversationSheet(state.conversations.find((row) => conversationId(row) === state.current) || { id: state.current, title: $("chat-title").textContent });
});
$("sheet-confirm-delete").addEventListener("click", deleteConversation);
for (const id of ["sheet-cancel", "sheet-instructions-cancel", "sheet-rename-cancel", "sheet-confirm-cancel"]) $(id).addEventListener("click", () => $("conversation-sheet").close());
// A tap on the dimmed backdrop (outside the sheet) closes it.
$("conversation-sheet").addEventListener("click", (event) => {
  const box = $("conversation-sheet").getBoundingClientRect();
  if (event.target === $("conversation-sheet") && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom)) $("conversation-sheet").close();
});
$("message").addEventListener("input", () => $("composer").classList.toggle("has-text", Boolean($("message").value.trim())));
$("message").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); $("composer").requestSubmit(); } });
$("job-form").addEventListener("submit", saveJob);
$("job-schedule").addEventListener("input", updateSchedulePreview);
$("close-dialog").addEventListener("click", () => $("job-dialog").close());
$("cancel-job").addEventListener("click", () => $("job-dialog").close());
$("delete-job").addEventListener("click", () => state.job && deleteJob(state.job));

setInterval(() => { if (document.querySelectorAll(".working-label").length) { nextWorkingVerb(); refreshWorkingLabels(); } }, 6000);
setInterval(() => {
  if (!$("app").classList.contains("hidden") && state.section === "settings" && state.settingsGroup === "about") loadAbout();
}, 5000);
setInterval(() => {
  if ($("app").classList.contains("hidden") || state.section !== "messages" || state.busy) return;
  if (state.current) loadMessages().catch(() => {});
}, 3000);
setInterval(() => {
  if ($("app").classList.contains("hidden")) return;
  if (state.section === "messages") loadConversations().catch(() => {});
  else if (state.section === "jobs") loadJobs().catch(() => {});
}, 20000);

// iOS reloads a home-screen app that sat in the background, often before the network (or VPN) is
// back. Until Roost answers, say so and keep trying, rather than leaving a blank page or signing out.
const boot = { done: false, running: false, timer: null, delay: 2000 };
async function start() {
  if (boot.done || boot.running) return;
  boot.running = true; clearTimeout(boot.timer);
  let data;
  try {
    data = await api("/me", { signal: AbortSignal.timeout(10000) });
  } catch {
    $("connecting").textContent = "Can’t reach Roost yet. Retrying…";
    boot.timer = setTimeout(start, boot.delay); boot.delay = Math.min(boot.delay * 2, 30000);
    return;
  } finally { boot.running = false; }
  boot.done = true; show("connecting", false);
  applyAgentName(data.agent);
  state.passkeysAvailable = Boolean(data.passkeys);
  return data.signed_in ? signedIn() : signedOut();
}
document.addEventListener("visibilitychange", () => { reportPresence(); if (document.visibilityState === "visible") { boot.delay = 2000; start(); } });
setInterval(reportPresence, 15000);
// A notification tapped while Roost is already open.
navigator.serviceWorker?.addEventListener("message", (event) => {
  if ($("app").classList.contains("hidden")) return;
  if (typeof event.data?.link === "string") openLink(event.data.link);
  else if (typeof event.data?.open === "string") openLink("?c=" + encodeURIComponent(event.data.open));
});
$("delivery-close").addEventListener("click", () => $("delivery-sheet").close());
window.addEventListener("online", () => { boot.delay = 2000; start(); });
start();
