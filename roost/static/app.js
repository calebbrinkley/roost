"use strict";

const $ = (id) => document.getElementById(id);
// Every field of `state`, by area, with its value at sign-in. signedOut() starts each area again from
// here, so nothing from one sign-in reaches the next; KEPT is what deliberately survives a sign-out.
// A test fails if app.js writes a field that is not declared here.
const SLICES = {
  conversations: () => ({ conversations: [], conversationOffset: 0, conversationsMore: false, loadingMore: false, refreshing: false,
                          search: "", searchResults: null, searchLoading: false, searchError: "", swipeOpenId: null, sheetItem: null, sheetBusy: false, listSignature: null }),
  conversation: () => ({ current: null, currentPersona: null, rememberSource: null, rememberBusy: false, model: null, noWeb: false, webForce: false, webAvailable: false, editTurn: null, controlBusy: false, context: null, draft: false, draftSeed: undefined, reasoning: undefined, busy: false, sentMessages: [],
                         composerDrafts: null, composerKey: undefined, expandedUserMessages: new Set(),
                         lastRows: null, messageHistory: null, renderedConversation: null, messageSignature: null, lastMessagePoll: null, idleSince: null,
                         shownTimes: new Set(), openThoughts: new Set(), selectionDeferred: false, latestContent: "" }),
  turn: () => ({ turn: null, working: false, live: null, liveNodes: null, liveThoughtsOpen: undefined }),
  approvals: () => ({ approvals: [], approvalBusy: null, approvalNotice: "", approvalReady: false, approvalFocus: null,
                      approvalFocusConversation: null, approvalLoading: null }),
  voice: () => ({ voiceDraft: false, voiceReply: null, playing: null }),
  jobs: () => ({ jobs: [], selectedJob: null, job: null, jobBusy: false, jobSearch: "", jobRuns: Object.create(null),
                 jobRunsError: Object.create(null), jobRunsLoading: new Set(), jobPatches: Object.create(null) }),
  settings: () => ({ settings: null, settingsGroup: null, settingsBusy: false, timeZone: "", nameDraft: undefined, soul: undefined,
                     soulDraft: undefined, soulLoading: false, about: null, backend: "hermes", models: [], defaultModel: null, primary: null, personaData: null, memoryData: null, editPersona: null, editMemory: null, openModel: null, modelsError: "", modelsLoading: false }),
  account: () => ({ passkeyList: null, passkeyRegister: null, passkeyNameDraft: undefined, removingPasskey: null,
                    push: null, removingPush: null, hermesLink: null, rotatingToken: false }),
};
const KEPT = () => ({
  // Sign-ins and views only count up; an answer to an earlier one is ignored.
  authEpoch: 0, viewEpoch: 0,
  // Newest-request counters: an older answer never replaces a newer one.
  conversationRequest: 0, messageRequest: 0, messageApplied: 0, jobsRequest: 0, approvalRevision: 0, sentKey: 0,
  // The device and the sign-in page: not private, and needed before the next sign-in.
  section: "messages", agentName: "", passkeysAvailable: false, passkeyLogin: null,
  workingVerb: undefined, player: null, stream: null, liveFrame: 0,
});
const freshSlices = () => Object.assign({}, ...Object.values(SLICES).map((slice) => slice()));
const state = { ...KEPT(), ...freshSlices() };
const navigation = { pending: "", signingIn: false };

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
function agent() {
  if (isNative()) return state.currentPersona || state.personaData?.personas.find(item => item.id === state.personaData.default_persona_id)?.name || state.agentName || "Assistant";
  return state.agentName || "Agent";
}

function replyName(row) { return isNative() ? row.persona_name || agent() : agent(); }

function applyAgentName(name) {
  if (name) state.agentName = name;
  for (const node of document.querySelectorAll("[data-agent-text]")) node.textContent = node.dataset.agentText.replaceAll("{agent}", agent());
  for (const node of document.querySelectorAll("[data-agent-placeholder]")) node.placeholder = node.dataset.agentPlaceholder.replaceAll("{agent}", agent());
  for (const node of document.querySelectorAll("[data-agent-initial]")) node.textContent = agent().charAt(0).toUpperCase();
  for (const node of document.querySelectorAll("[data-agent-title]")) node.title = node.dataset.agentTitle.replaceAll("{agent}", agent());
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
  label.textContent = (state.approvals?.length ? `${agent()} is waiting for your approval` : `${agent()} is ${state.workingVerb || "thinking"}`) + (ellipsis ? "…" : "");
  return label;
}

function refreshWorkingLabels() {
  for (const label of document.querySelectorAll(".working-label")) {
    label.textContent = (state.approvals?.length ? `${agent()} is waiting for your approval` : `${agent()} is ${state.workingVerb}`) + (label.classList.contains("ellipsis") ? "…" : "");
  }
}
// Line icons in one style (24 px grid, 2 px round strokes, current text colour).
const ICONS = {
  compose: [["path", { d: "M11 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5" }], ["path", { d: "M17.5 3.5a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4 8.5-8.5z" }]],
  plus: [["path", { d: "M12 5v14M5 12h14" }]],
  copy: [["rect", { x: 8, y: 8, width: 12, height: 12, rx: 2 }], ["path", { d: "M16 8V4H4v12h4" }]],
  down: [["path", { d: "M12 4v16m-6-6 6 6 6-6" }]],
  speaker: [["path", { d: "M11 5 6 9H3v6h3l5 4V5z" }], ["path", { d: "M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13" }]],
  stop: [["rect", { x: 7, y: 7, width: 10, height: 10, rx: 1.5 }]],
  gear: [["path", { d: "M10.08 4.85L10.09 2.18L13.91 2.18L13.92 4.85L15.70 5.59L17.59 3.71L20.29 6.41L18.41 8.30L19.15 10.08L21.82 10.09L21.82 13.91L19.15 13.92L18.41 15.70L20.29 17.59L17.59 20.29L15.70 18.41L13.92 19.15L13.91 21.82L10.09 21.82L10.08 19.15L8.30 18.41L6.41 20.29L3.71 17.59L5.59 15.70L4.85 13.92L2.18 13.91L2.18 10.09L4.85 10.08L5.59 8.30L3.71 6.41L6.41 3.71L8.30 5.59Z" }],
         ["circle", { cx: 12, cy: 12, r: 3 }]],
  search: [["circle", { cx: 11, cy: 11, r: 7 }], ["path", { d: "m20 20-3.5-3.5" }]],
  more: [["circle", { cx: 5, cy: 12, r: 1.6, fill: "currentColor", stroke: "none" }], ["circle", { cx: 12, cy: 12, r: 1.6, fill: "currentColor", stroke: "none" }],
         ["circle", { cx: 19, cy: 12, r: 1.6, fill: "currentColor", stroke: "none" }]],
  warning: [["path", { d: "M12 3 2.5 20h19z" }], ["path", { d: "M12 10v4M12 17v.01" }]],
  palette: [["circle", { cx: 12, cy: 12, r: 9 }], ["circle", { cx: 8.5, cy: 10, r: 1 }], ["circle", { cx: 12, cy: 7.5, r: 1 }], ["circle", { cx: 15.5, cy: 10, r: 1 }],
            ["path", { d: "M12 21a3 3 0 0 1 0-6h2a3 3 0 0 0 3-3" }]],
  globe: [["circle", { cx: 12, cy: 12, r: 9 }], ["path", { d: "M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" }]],
  bell: [["path", { d: "M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9" }], ["path", { d: "M10.3 21a1.9 1.9 0 0 0 3.4 0" }]],
  key: [["circle", { cx: 8, cy: 15, r: 4 }], ["path", { d: "m10.8 12.2 9.2-9.2M17 6l3 3M14 9l2 2" }]],
  data: [["ellipse", { cx: 12, cy: 6, rx: 8, ry: 3 }], ["path", { d: "M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" }]],
  server: [["rect", { x: 3, y: 4, width: 18, height: 7, rx: 2 }], ["rect", { x: 3, y: 13, width: 18, height: 7, rx: 2 }], ["path", { d: "M7 7.5h.01M7 16.5h.01" }]],
  info: [["circle", { cx: 12, cy: 12, r: 9 }], ["path", { d: "M12 11v6M12 7.5v.01" }]],
  persona: [["circle", { cx: 12, cy: 8, r: 4 }], ["path", { d: "M4 21c0-4.4 3.6-7 8-7s8 2.6 8 7" }]],
  memory: [["path", { d: "M6 3h12v18l-6-4.5L6 21z" }]],
};

function svgShapes(svg, shapes) {
  for (const [tag, attributes] of shapes) {
    const shape = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) shape.setAttribute(key, String(value));
    svg.append(shape);
  }
  return svg;
}

function lineSvg(strokeWidth = 2) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", String(strokeWidth)); svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round");
  return svg;
}

function icon(name) { return svgShapes(lineSvg(), ICONS[name]); }

// Each conversation keeps one colour and one friendly face, both picked from a hash of its id.
const DOT = { fill: "currentColor", stroke: "none" };
const FACE_EYES = [
  [["circle", { cx: 9, cy: 10, r: 1.3, ...DOT }], ["circle", { cx: 15, cy: 10, r: 1.3, ...DOT }]],
  [["path", { d: "M7.5 10.5q1.5-2 3 0M13.5 10.5q1.5-2 3 0" }]],
  [["circle", { cx: 9, cy: 10, r: 1.3, ...DOT }], ["path", { d: "M13.5 10.5q1.5-2 3 0" }]],
  [["ellipse", { cx: 9, cy: 10, rx: 1, ry: 1.6, ...DOT }], ["ellipse", { cx: 15, cy: 10, rx: 1, ry: 1.6, ...DOT }]],
];
const FACE_MOUTHS = [
  [["path", { d: "M8.5 14.5q3.5 3 7 0" }]],
  [["path", { d: "M9.5 14.2h5q-.6 2.6-2.5 2.6t-2.5-2.6z", fill: "currentColor" }]],
  [["path", { d: "M10 15q2 1.5 4 0" }]],
  [["path", { d: "M9 15q1.5 1.5 3 0q1.5 1.5 3 0" }]],
];
const FACE_CHEEKS = [["circle", { cx: 7, cy: 13.5, r: 1.2, fill: "#fff", opacity: .35, stroke: "none" }],
                     ["circle", { cx: 17, cy: 13.5, r: 1.2, fill: "#fff", opacity: .35, stroke: "none" }]];
const AVATAR_HUES = 8;

// FNV-1a: small, stable and well spread, so neighbouring ids get different faces.
function faceSeed(id) {
  let hash = 0x811c9dc5;
  for (const char of String(id || "")) hash = Math.imul(hash ^ char.codePointAt(0), 0x01000193) >>> 0;
  return hash;
}

function avatar(id, className = "") {
  const seed = faceSeed(id), node = el("span", `av hue-${seed % AVATAR_HUES + 1}${className ? " " + className : ""}`);
  node.setAttribute("aria-hidden", "true");
  const face = svgShapes(lineSvg(1.8), [...FACE_EYES[(seed >>> 3) % 4], ...FACE_MOUTHS[(seed >>> 5) % 4], ...((seed >>> 7) % 2 ? FACE_CHEEKS : [])]);
  face.setAttribute("class", "face");
  node.append(face);
  return node;
}

function setChatAvatar(id) { $("chat-avatar").replaceChildren(avatar(id, "sm")); }

// A new conversation has no id until its first message, so its face comes from a fresh draft seed.
function newDraftSeed() { state.draftSeed = "draft:" + Date.now() + ":" + Math.random(); }

let noticeTimer;
function notice(message) {
  clearTimeout(noticeTimer);
  $("notice").textContent = message;
  show("notice", Boolean(message));
  if (message) noticeTimer = setTimeout(() => { if ($("notice").textContent === message) show("notice", false); }, 6000);
}

// What an async step checks before it touches the page or state again: still the same sign-in, and for
// a conversation also the same view of the same conversation. A sign-out or a switch ends every scope.
function signInScope() {
  const epoch = state.authEpoch;
  return { alive: () => epoch === state.authEpoch };
}
function viewScope(id = state.current) {
  const signedIn = signInScope(), view = state.viewEpoch;
  return { alive: () => signedIn.alive() && view === state.viewEpoch && state.current === id };
}
// An error is news only while the step that met it is still current.
function report(error, scope) { if (scope.alive()) notice(error.message); }

const READ_TIMEOUT_MS = 15000, SEND_TIMEOUT_MS = 50000;
const PENDING_CHECK_MS = 5000, PENDING_CHECK_LIMIT_MS = 120000;

// Answers only the sign-in that asked: a response that arrives after a sign-out rejects instead.
async function api(path, options = {}) {
  const scope = signInScope();
  const ms = !options.method || options.method === "GET" ? READ_TIMEOUT_MS
    : options.method === "POST" && /^\/conversations(?:\/[^/]+\/messages)?$/.test(path) ? SEND_TIMEOUT_MS : 0;
  let signal = options.signal, cleanup = () => {};
  if (ms) {
    const timer = AbortSignal.timeout(ms);
    if (!signal) signal = timer;
    else if (AbortSignal.any) signal = AbortSignal.any([signal, timer]);
    else {
      const controller = new AbortController(), caller = signal;
      const abort = (event) => controller.abort(event.target.reason);
      for (const source of [caller, timer]) {
        if (source.aborted) controller.abort(source.reason);
        else source.addEventListener("abort", abort, { once: true });
      }
      cleanup = () => { for (const source of [caller, timer]) source.removeEventListener("abort", abort); };
      signal = controller.signal;
    }
  }
  try {
    const response = await fetch("/api" + path, {
      credentials: "same-origin",
      headers: { "X-Roost-Request": "1", ...(options.body ? { "Content-Type": "application/json" } : {}) },
      ...options,
      signal,
    });
    let data;
    try { data = await response.json(); } catch (error) { if (signal?.aborted) throw error; data = {}; }
    if (!scope.alive()) throw new Error("This request belongs to an earlier sign-in.");
    if (!response.ok) {
      if (response.status === 401 && path !== "/login" && path !== "/passkeys/login") { signedOut(); throw new Error("Please sign in again."); }
      const error = new Error(data.error || `Request failed (${response.status}).`);
      error.status = response.status; error.pending = data.pending === true; throw error;
    }
    return data;
  } finally { cleanup(); }
}

function signedOut(clearLink = true) {
  state.authEpoch++; state.viewEpoch++;
  if (clearLink) {
    navigation.pending = "";
    if (globalThis.location?.search) history.replaceState(null, "", "/");
  }
  navigation.signingIn = false;
  $("presence").classList.remove("reconnecting");
  closeStream();
  stopRecording(true); state.voiceDraft = false; state.player?.pause();
  state.sentMessages.forEach(stopSendCheck);
  try { localStorage.removeItem("roost.composer-drafts"); } catch { /* Storage may be unavailable. */ }
  Object.assign(state, freshSlices());
  updateLatest();
  renderApprovals();
  show("app", false); show("login", true);
  $("app").classList.remove("chat-open", "job-open", "settings-open", "peek");
  for (const id of ["password", "message", "search", "sheet-name", "remember-text", "job-name", "job-prompt", "job-schedule", "job-deliver"]) $(id).value = "";
  for (const id of ["transcript", "conversation-list", "sidebar-jobs-list", "job-detail", "sidebar-settings-list", "settings-detail"]) $(id).replaceChildren();
  for (const id of ["job-dialog", "conversation-sheet", "delivery-sheet", "remember-sheet", "summary-sheet"]) if ($(id).open) $(id).close();
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
  const scope = signInScope();
  navigation.signingIn = true;
  show("login", false); show("app", true);
  $("password").value = "";
  $("login-error").textContent = "";
  setSection("messages");
  const identity = refreshIdentity();
  if (identity) await identity;
  if (!scope.alive()) return;
  try { await loadConversations(); if (scope.alive()) setConnection(true); } catch (error) { if (scope.alive()) { setConnection(false); notice(error.message); } }
  if (!scope.alive()) return;
  navigation.signingIn = false;
  // A tapped notification opens Roost at /?c=<conversation>, /?j=<job> or /?d=<delivered message>.
  const search = navigation.pending || globalThis.location?.search || "";
  navigation.pending = "";
  if (search) { if (globalThis.location?.search) history.replaceState(null, "", "/"); openLink(search); }
}

function notificationLink(link) {
  if ($("app").classList.contains("hidden") || navigation.signingIn) navigation.pending = link;
  else openLink(link);
}

function setConnection(online) {
  $("presence").textContent = online ? "Online" : "Reconnecting…";
  $("presence").classList.toggle("reconnecting", !online);
  placePresence();
}

function placePresence() {
  const app = $("app"), section = state.section;
  const list = phoneWidth.matches && !app.classList.contains({ messages: "chat-open", jobs: "job-open", settings: "settings-open" }[section]);
  const slot = $("presence").classList.contains("reconnecting") ?
    (list ? "mobile-list-actions" : { messages: "chat-actions", jobs: "jobs-header", settings: "settings-header" }[section]) : "chat-actions";
  $(slot).append($("presence"));
}

async function refreshConnection(load) {
  const scope = signInScope();
  try { await load(); if (scope.alive()) setConnection(true); }
  catch { if (scope.alive() && !$("app").classList.contains("hidden")) setConnection(false); }
}

function refreshOnResume() {
  if ($("app").classList.contains("hidden") || navigation.signingIn) return;
  resetMessagePolling();
  return refreshConnection(() => Promise.all([loadConversations(),
    state.section === "jobs" ? loadJobs() : state.current ? loadMessages() : Promise.resolve()]));
}

function openLink(search) {
  const link = new URLSearchParams(search); state.approvalFocus = link.get("a");
  const conversation = link.get("c"), job = link.get("j"), delivery = link.get("d");
  state.approvalFocusConversation = conversation;
  if (conversation) { setSection("messages"); selectConversation(conversation); }
  else if (job && !isNative()) showJob(job);
  else if (delivery && !isNative()) openDelivery(delivery);
}

async function showJob(id) {
  setSection("jobs");
  try { await loadJobs(); } catch (error) { notice(error.message); return; }
  if (state.jobs.some((job) => job.id === id)) selectJob(id);
  else notice("That job no longer exists.");
}

// A message the agent sent on its own, kept by Roost (Hermes has no transcript of it).
async function openDelivery(id) {
  const scope = signInScope();
  try {
    const item = await api("/deliveries/" + encodeURIComponent(id));
    $("delivery-title").textContent = item.title || agent();
    $("delivery-time").textContent = dateTimeLabel(item.created);
    const body = $("delivery-text"); body.replaceChildren(); addMarkdown(body, String(item.text || ""));
    if (!$("delivery-sheet").open) $("delivery-sheet").showModal();
  } catch (error) { report(error, scope); }
}

// The agent's name and the synced accent, as the signed-in server sees them.
async function refreshIdentity() {
  try {
    const data = await api("/me");
    if (!data.signed_in) return;
    applyServerSettings(data);
  } catch { /* The defaults stay until the next sign-in or settings visit. */ }
}

// /me, /settings and a saved change all answer with the name shown, the accent and the time zone.
function isNative() { return state.backend === "native"; }

function backendSurfaces(backend) {
  return { jobs: backend !== "native", soul: backend !== "native", voice: backend !== "native",
    approvals: backend !== "native", hermes: backend !== "native" };
}

function applyServerSettings(data) {
  state.backend = data.backend || "hermes";
  const surfaces = backendSurfaces(state.backend);
  for (const id of ["nav-jobs", "mobile-nav-jobs"]) show(id, surfaces.jobs);
  show("voice-button", surfaces.voice);
  $("search").maxLength = isNative() ? 200 : 524288;
  $("app").classList.toggle("native", isNative());
  if (isNative()) loadModels().catch(error => notice(error.message));
  state.agentName = data.agent || "";
  applyAgentName(data.agent);
  window.roostTheme.setAccent(data.accent);
  applyTimeZone(data.time_zone);
}

function applyTimeZone(name) {
  if ((name || "") === state.timeZone) return;
  state.timeZone = name || "";
  renderConversations();
  if (state.jobs.length) renderJobs();
  if (state.current) loadMessages().catch(() => {});
}

function setSection(section) {
  if (isNative() && section === "jobs") section = "messages";
  if (section !== "messages") stopRecording(true);
  state.section = section;
  $("app").classList.toggle("section-jobs", section === "jobs");
  $("app").classList.toggle("section-settings", section === "settings");
  $("app").classList.remove("chat-open", "job-open", "settings-open");
  placePresence();
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
  $("sidebar-new").replaceChildren(icon(jobs ? "plus" : "compose"));
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

function clockTime(date) { return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", hour12: true, ...zone() }); }

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
// On touch screens, focusing the message field opens the on-screen keyboard; only the owner's tap should do that.
const touchScreen = window.matchMedia("(pointer: coarse)");

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
  $("sheet-instructions").textContent = isNative() ? "Chat settings" : "Instructions";
  $("sheet-error").textContent = ""; $("sheet-confirm-error").textContent = ""; $("sheet-instructions-error").textContent = "";
  $("sheet-export").href = `/api/conversations/${encodeURIComponent(item.id)}/export`;
  sheetView("sheet-actions");
  if (!$("conversation-sheet").open) $("conversation-sheet").showModal();
  $("sheet-title").focus();
}

async function renameConversation(event) {
  event.preventDefault();
  if (state.sheetBusy) return;
  const item = state.sheetItem, id = item && item.id;
  if (!id) return;
  const title = $("sheet-name").value.trim();
  const scope = signInScope();
  state.sheetBusy = true;
  try {
    const result = await api(`/conversations/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ title }) });
    item.title = result.title ?? title;
    if (state.current === id) $("chat-title").textContent = sessionName(item);
    $("conversation-sheet").close(); renderConversations(); notice("Conversation renamed.");
  } catch (error) { if (scope.alive()) $("sheet-error").textContent = error.message; }
  finally { if (scope.alive()) state.sheetBusy = false; }
}

async function openInstructions() {
  const item = state.sheetItem, id = item && item.id;
  if (!id || state.sheetBusy) return;
  const scope = signInScope(), field = $("sheet-instructions-text");
  field.value = ""; field.disabled = true;
  sheetView("sheet-instructions-form");
  show("sheet-native-settings", isNative());
  show("sheet-hermes-note", !isNative());
  try {
    if (isNative()) await Promise.all([loadModels(), loadPersonas()]);
    const data = await api(`/conversations/${encodeURIComponent(id)}/settings`);
    if (state.sheetItem !== item) return;
    field.value = typeof data.instructions === "string" ? data.instructions : "";
    if (isNative()) {
      fillModelPicker($("sheet-model"), data.model);
      fillPersonaPicker($("sheet-persona"), data.persona_id);
      $("sheet-reasoning").value = data.reasoning || "none";
      $("sheet-no-web").checked = Boolean(data.no_web);
      updateSheetThinking();
    }
    field.disabled = false; field.focus();
  } catch (error) { if (scope.alive()) $("sheet-instructions-error").textContent = error.message; }
}

async function saveInstructions(event) {
  event.preventDefault();
  const item = state.sheetItem, id = item && item.id, field = $("sheet-instructions-text");
  if (!id || state.sheetBusy || field.disabled) return;
  const scope = signInScope();
  state.sheetBusy = true;
  try {
    const data = await api(`/conversations/${encodeURIComponent(id)}/settings`, { method: "PUT", body: JSON.stringify({ instructions: field.value, ...(isNative() ? {model: $("sheet-model").value || null, reasoning: $("sheet-reasoning").value, no_web: $("sheet-no-web").checked, persona_id: $("sheet-persona").value || null} : {}) }) });
    if (isNative() && state.current === id) { state.model = data.model; state.noWeb = Boolean(data.no_web); showReasoning(data.reasoning); await loadMessages(id); }
    $("conversation-sheet").close();
    notice(data.instructions ? `Instructions saved. ${agent()} gets them with your next message.` : "Instructions cleared.");
  } catch (error) { if (scope.alive()) $("sheet-instructions-error").textContent = error.message; }
  finally { if (scope.alive()) state.sheetBusy = false; }
}

async function deleteConversation() {
  if (state.sheetBusy) return;
  const item = state.sheetItem, id = item && item.id;
  if (!id) return;
  const scope = signInScope();
  state.sheetBusy = true;
  try {
    await api(`/conversations/${encodeURIComponent(id)}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) });
    state.sentMessages.filter((sent) => sent.id === id).forEach(stopSendCheck);
    state.sentMessages = state.sentMessages.filter((sent) => sent.id !== id);
    saveComposer("", id);
    // The server's rows shift down by one, so the next page starts one row earlier. A load
    // started before the deletion could bring the row back or reuse the old offset: drop it.
    if (state.conversations.some((row) => row.id === id)) state.conversationOffset = Math.max(0, state.conversationOffset - 1);
    state.conversationRequest = state.conversationRequest + 1;
    state.conversations = state.conversations.filter((row) => row.id !== id);
    if (state.swipeOpenId === id) state.swipeOpenId = null;
    if (state.current === id) { stopRecording(true); state.voiceDraft = false; state.viewEpoch++; state.current = null; restoreComposer(); $("app").classList.remove("chat-open"); renderWelcome(); }
    $("conversation-sheet").close(); renderConversations(); notice("Conversation deleted.");
  } catch (error) { if (scope.alive()) $("sheet-confirm-error").textContent = error.message; }
  finally { if (scope.alive()) state.sheetBusy = false; }
}

function renderConversations() {
  const signature = JSON.stringify([state.conversations, state.current, state.search, state.working, state.loadingMore,
    state.searchResults, state.searchLoading, state.searchError, state.swipeOpenId, state.approvals.length > 0, state.timeZone, agent(), dayKey(new Date())]);
  if (signature === state.listSignature) return;
  state.listSignature = signature;
  const list = $("conversation-list"); list.replaceChildren();
  const searching = isNative() && Boolean(state.search);
  const rows = searching ? state.searchResults || [] : state.conversations.filter((item) =>
    (sessionName(item) + " " + (withoutInstructions(item.preview) || "") + " " + (item.source || "")).toLowerCase().includes(state.search));
  if (searching && state.searchLoading) list.append(el("p", "empty-list", "Searching…"));
  else if (searching && state.searchError) { const error = el("p", "empty-list", state.searchError); error.setAttribute("role", "alert"); list.append(error); }
  else if (!rows.length) list.append(el("p", "empty-list", state.search ? "No matching conversations." : `No conversations yet. Start one with ${agent()}.`));
  const today = dayKey(new Date());
  let group = null;
  for (const item of rows) {
    const id = item.id;
    if (!id) continue;
    const when = item.last_active_at || item.last_active || item.updated_at || item.created_at, date = parseTime(when);
    const label = date && dayKey(date) === today ? "Today" : "Earlier";
    if (label !== group) { group = label; list.append(el("div", "group-label", label)); }
    const button = el("button", "conversation-row" + (state.current === id ? " selected" : ""));
    button.append(avatar(id));
    const copy = el("span", "conversation-copy");
    const heading = el("span", "conversation-heading");
    heading.append(el("span", "conversation-name", sessionName(item)));
    if (item.source && item.source !== "roost") heading.append(el("span", "source-tag", item.source));
    // The server marks conversations it is following; the open one also knows what this device just sent.
    const working = item.working === true || (id === state.current && state.working);
    const preview = el("span", "row-preview" + (working ? " live" : ""));
    preview.append(searching ? item.snippet || item.preview || "" : working ? workingLabel(true) : (withoutInstructions(item.preview) || `Conversation with ${agent()}`));
    copy.append(heading, preview);
    button.append(copy);
    const meta = el("span", "row-meta");
    meta.append(el("span", "", timeLabel(when)));
    if (working) meta.append(el("span", "unread-dot"));
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
  const scope = signInScope();
  // A refresh waits out a page load; both would otherwise race for the paging offset.
  if (state.loadingMore) return;
  if (more) { state.loadingMore = true; renderConversations(); }
  const serial = state.conversationRequest = state.conversationRequest + 1;
  try {
    // Refresh the loaded range, not just its first page: insertions would otherwise
    // push a conversation into an unfetched gap. Page offsets count raw server rows.
    const target = more ? 0 : state.conversations.length;
    let offset = more ? state.conversationOffset : 0, data, rows;
    const loaded = more ? [...state.conversations] : [];
    const known = new Set(loaded.map((row) => row.id));
    do {
      data = await api(`/conversations?offset=${offset}`);
      if (serial !== state.conversationRequest) return;
      rows = data.sessions;
      offset += rows.length;
      for (const item of rows) {
        const id = item.id;
        if (typeof id === "string" && id && !known.has(id)) { known.add(id); loaded.push(item); }
      }
    } while (!more && rows.length && offset < target && offset <= 100000 &&
             (!Number.isInteger(data.total) || offset < data.total));
    state.conversations = loaded;
    state.conversationOffset = offset;
    state.conversationsMore = rows.length > 0 && offset <= 100000 &&
      (!Number.isInteger(data.total) || offset < data.total);
    finishLoadingConversations(data, loaded, more);
  } finally {
    if (more && scope.alive()) { state.loadingMore = false; renderConversations(); }
  }
  if (scope.alive()) loadMoreIfNearEnd();
}

// Older conversations load as the list nears its end, or at once when the loaded ones don't fill it.
function loadMoreIfNearEnd() {
  const list = $("conversation-list");
  if ((isNative() && state.search) || state.section !== "messages" || !state.conversationsMore || state.loadingMore) return;
  if (list.scrollTop + list.clientHeight < list.scrollHeight - 400) return;
  loadConversations(true).catch((error) => notice(error.message));
}


function finishLoadingConversations(data, rows, more) {
  if (!state.current && !state.draft && !more && rows.length) {
    selectConversation(rows[0].id, rows[0].source);
    // Initial selection stays in the list on phones.
    $("app").classList.remove("chat-open");
    reportPresence();
  }
  // A native chat's automatic title can arrive after the chat was opened.
  const open = isNative() && state.current ? rows.find((row) => row.id === state.current) : null;
  if (open) $("chat-title").textContent = sessionName(open);
  renderConversations();
  if (!state.current && !more) renderWelcome();
}

function renderWelcome() {
  state.messageHistory = null; state.lastRows = null; state.messageSignature = null;
  if (isNative()) { state.currentPersona = null; applyAgentName(); state.live = null; state.turn = null; state.model = null; state.noWeb = false; state.webForce = false; state.context = null; updateNativeComposer(); renderContext(); }
  if (state.composerKey === undefined) restoreComposer();
  if (state.sentMessages.some((item) => item.id === null && item.failed)) {
    $("chat-title").textContent = "New conversation";
    show("origin-badge", false);
    renderMessages([]); return;
  }
  show("chat-settings", false);
  show("jump-latest", false); $("jump-latest").classList.remove("unread");
  const transcript = $("transcript"); transcript.replaceChildren();
  if (!state.draftSeed) newDraftSeed();
  setChatAvatar(state.draftSeed);
  const card = el("div", "welcome-card");
  const starters = el("div", "starters");
  for (const [title, hint, text] of (isNative() ? NATIVE_STARTERS : STARTERS)) {
    const starter = el("button", "starter");
    starter.type = "button";
    starter.append(el("b", "", title), el("span", "", hint));
    starter.addEventListener("click", () => {
      $("message").value = text;
      saveComposer();
      $("composer").classList.add("has-text");
      $("message").focus();
    });
    starters.append(starter);
  }
  card.append(avatar(state.draftSeed, "xl"), el("h2", "", agent()), starters);
  transcript.append(card);
  $("chat-title").textContent = "New conversation";
  show("origin-badge", false);
}

// Suggestions on a new conversation's welcome screen: a tap puts the message in the box to edit or send.
const NATIVE_STARTERS = [
  ["Think it through", "A fresh perspective", "Help me think through a decision. Ask me what I’m weighing up."],
  ["Make a plan", "One step at a time", "Help me break a project into a practical plan."],
];
const STARTERS = [
  ["Plan my week", "From your calendar and notes", "Help me plan my week, using my calendar and notes if you can see them."],
  ["Set up a job", "A briefing every weekday at 8", "Set up a job that sends me a short briefing every weekday at 8."],
  ["Summarise a note", "Anything in your notes folder", "Summarise a note from my notes folder. Ask me which one."],
  ["Check on the server", "Disk, services and backups", "Check on the server: disk space, services and backups."],
];

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

function sourceLink(source, label) {
  if (!source || typeof source.url !== "string" || !/^https?:\/\/[^/\s@]+(?:[/?#]|$)/i.test(source.url)) return null;
  const link = el("a", "citation", label); link.href = source.url; link.target = "_blank"; link.rel = "noopener noreferrer";
  return link;
}

function addCitations(parent, text, sources) {
  let at = 0;
  for (const match of text.matchAll(/\[(\d+)\]/g)) {
    parent.append(document.createTextNode(text.slice(at, match.index)));
    const source = sources.find(item => item.n === Number(match[1]));
    parent.append(sourceLink(source, match[0]) || document.createTextNode(match[0]));
    at = match.index + match[0].length;
  }
  parent.append(document.createTextNode(text.slice(at)));
}

function addReply(parent, text, sources = []) {
  addMarkdown(parent, text, sources);
  if (!sources.length) return;
  const list = el("div", "reply-sources");
  for (const source of sources) {
    const link = sourceLink(source, `[${source.n}] ${source.title || source.url}`);
    if (link) list.append(link);
  }
  parent.append(list);
}

function addInline(parent, value, sources = []) {
  // split() with one capture group puts the matched markup at odd indexes and plain text at even ones.
  value.split(INLINE).forEach((piece, index) => {
    if (!piece) return;
    if (index % 2 === 0) { if (sources.length) addCitations(parent, piece, sources); else parent.append(document.createTextNode(piece)); }
    else if (piece.startsWith("**")) { const node = el("strong"); if (sources.length) addCitations(node, piece.slice(2, -2), sources); else node.textContent = piece.slice(2, -2); parent.append(node); }
    else if (piece.startsWith("`")) parent.append(el("code", "", piece.slice(1, -1)));
    else { const node = el("em"); if (sources.length) addCitations(node, piece.slice(1, -1), sources); else node.textContent = piece.slice(1, -1); parent.append(node); }
  });
}

// Table cells take the same bold, code and italics as paragraphs. Code blocks stay literal.
function tableCell(tag, text, sources) {
  const cell = el(tag);
  addInline(cell, text.trim(), sources);
  return cell;
}

function addPlainMarkdown(parent, value, sources) {
  const lines = value.split("\n");
  for (let i = 0; i < lines.length;) {
    if (!lines[i].trim()) { i++; continue; }
    if (/^\s*[-*•] /.test(lines[i])) {
      const list = el("ul");
      while (i < lines.length && /^\s*[-*•] /.test(lines[i])) {
        const item = el("li"); addInline(item, lines[i].replace(/^\s*[-*•] /, ""), sources); list.append(item); i++;
      }
      parent.append(list); continue;
    }
    if (lines[i].includes("|") && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1])) {
      const table = el("table");
      const head = el("tr");
      for (const cell of lines[i].replace(/^\||\|$/g, "").split("|")) head.append(tableCell("th", cell, sources));
      table.append(head); i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        const row = el("tr");
        for (const cell of lines[i].replace(/^\||\|$/g, "").split("|")) row.append(tableCell("td", cell, sources));
        table.append(row); i++;
      }
      parent.append(table); continue;
    }
    const paragraph = [];
    while (i < lines.length && lines[i].trim() && !/^\s*[-*•] /.test(lines[i])) paragraph.push(lines[i++]);
    const node = el("p"); addInline(node, paragraph.join("\n"), sources); parent.append(node);
  }
}

function addMarkdown(parent, value, sources = []) {
  const fence = /```[^\n]*\n([\s\S]*?)```/g;
  let last = 0;
  for (const match of value.matchAll(fence)) {
    addPlainMarkdown(parent, value.slice(last, match.index), sources);
    const code = match[1].replace(/\n$/, "");
    const block = el("div", "code-block"), pre = el("pre"); pre.append(el("code", "", code));
    block.append(copyButton(code, "Copy code"), pre); parent.append(block);
    last = match.index + match[0].length;
  }
  addPlainMarkdown(parent, value.slice(last), sources);
}

function textLines(text) { return text.split(/\r\n|\r|\n/); }
function longText(text) { return text.length >= 800 || textLines(text).length >= 8; }

function addUserMessage(body, text, key) {
  if (!longText(text)) { addMarkdown(body, text); return; }
  const content = el("div"), button = el("button", "paste-toggle");
  button.type = "button";
  const render = () => {
    const expanded = state.expandedUserMessages.has(key);
    content.replaceChildren();
    addMarkdown(content, expanded ? text : textLines(text).slice(0, 3).join("\n").slice(0, 400) + "…");
    button.textContent = expanded ? "Show less" : `Show all ${textLines(text).length} lines`;
    button.setAttribute("aria-expanded", String(expanded));
  };
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    if (state.expandedUserMessages.has(key)) state.expandedUserMessages.delete(key);
    else state.expandedUserMessages.add(key);
    render();
  });
  render(); body.append(content, button);
}

function copyButton(text, label) {
  const button = el("button", "play-reply copy-button");
  button.type = "button"; button.title = label; button.setAttribute("aria-label", label);
  button.setAttribute("aria-live", "polite");
  button.append(icon("copy"));
  button.addEventListener("click", async (event) => {
    event.stopPropagation();
    button.disabled = true;
    try { await navigator.clipboard.writeText(text); button.textContent = "Copied"; }
    catch { button.textContent = "Copy failed"; }
    button.setAttribute("aria-label", button.textContent);
    setTimeout(() => {
      button.replaceChildren(icon("copy")); button.setAttribute("aria-label", label); button.disabled = false;
    }, 2000);
  });
  return button;
}

// The same field precedence as Hermes's own desktop app.
function reasoningText(row) {
  const value = row.display_reasoning !== undefined ? row.display_reasoning
    : row.reasoning || row.reasoning_content || (typeof row.reasoning_details === "string" ? row.reasoning_details : "");
  return typeof value === "string" ? value.trim() : "";
}

// Native traces use the same compact Thoughts layout for saved and live steps.
function fillThoughts(body, items) {
  body.replaceChildren();
  for (const [index, item] of items.entries()) {
    if (typeof item === "string" || item.kind === "reasoning") { addMarkdown(body, typeof item === "string" ? item : item.text); continue; }
    if (item.kind === "memories") {
      const used = el("details", "used-memories");
      const key = `memories:${item.turn_id}`; used.open = state.openThoughts.has(key);
      used.append(el("summary", "", `Used ${item.facts.length} ${item.facts.length === 1 ? "memory" : "memories"}`));
      const list = el("ul", "used-memories-list");
      for (const fact of item.facts) list.append(el("li", "", fact.text));
      used.append(list);
      used.addEventListener("toggle", () => { if (used.open) state.openThoughts.add(key); else state.openThoughts.delete(key); });
      body.append(used); continue;
    }
    if (item.name === "remember") {
      const tool = el("div", "thought-tool memory-step");
      let result = {}; try { result = JSON.parse(item.result || "{}"); } catch { /* A partial result has no saved fact yet. */ }
      let fact = item.fact;
      try { fact ??= JSON.parse(item.arguments).fact; } catch { /* Keep incomplete arguments literal. */ }
      const label = item.forgotten ? `Forgotten: ${fact}` : item.fact_id ? `Remembered: ${fact}` : item.status === "running" ? `Remembering: ${fact || "…"}` : `Memory not saved: ${result.error || item.status}`;
      tool.append(el("div", "tool-call tool-call-plain", label));
      if (item.fact_id && !item.forgotten) {
        const remove = el("button", "send-action native-action memory-forget", "×"); remove.type = "button"; remove.setAttribute("aria-label", "Forget this memory"); remove.title = "Forget this memory";
        remove.addEventListener("click", async () => {
          remove.disabled = true;
          try { await api(`/memory/${item.fact_id}`, {method: "DELETE"}); state.memoryData = null; item.forgotten = true; fillThoughts(body, items); }
          catch (error) { remove.disabled = false; notice(error.message); }
        });
        tool.append(remove);
      }
      body.append(tool); continue;
    }
    if (item.name === "summarize") {
      const label = item.status === "running" ? "Summarizing earlier messages…" : item.status === "completed" ? "Summarized earlier messages" : item.status === "error" ? "Couldn't summarize earlier messages" : "Summarizing earlier messages was interrupted";
      const tool = el("div", "thought-tool"); tool.append(el("div", "tool-call tool-call-plain", label)); body.append(tool); continue;
    }
    let argument = item.arguments;
    try { const args = JSON.parse(argument); argument = args.query ?? args.url ?? argument; } catch { /* Show malformed arguments literally. */ }
    const tool = el("div", "thought-tool");
    const value = String(argument).replace(/\s+/g, " ").slice(0, 400);
    let label = `${item.name} · ${value} · ${item.status}`, link = null;
    if (item.name === "web_search") {
      const action = item.status === "running" ? "Searching the web" : item.status === "completed" ? "Searched the web" : item.status === "error" ? "Search failed" : "Search interrupted";
      label = `${action} for "${value}"`;
    } else if (item.name === "read_page") {
      let host = value;
      try { host = new URL(argument).host || value; } catch { /* Show malformed URLs literally. */ }
      const action = item.status === "running" ? "Reading" : item.status === "completed" ? "Read" : item.status === "error" ? "Read failed for" : "Read interrupted for";
      label = `${action} ${host}`;
      link = sourceLink({url: argument}, argument);
    }
    const heading = el("div", "tool-call" + (["web_search", "read_page"].includes(item.name) ? " tool-call-plain" : ""), label);
    if (link) heading.append(document.createTextNode(" · "), link);
    tool.append(heading);
    const output = el("details", "tool-output"), key = `tool:${item.id}:${index}`;
    output.open = state.openThoughts.has(key); output.append(el("summary", "", "Output"));
    const text = item.result === undefined ? "Waiting for output…" : item.result || "No output.";
    output.append(el("pre", "", text.slice(0, 16000) + (text.length >= 16000 ? "\n[Output trimmed for display.]" : "")));
    output.addEventListener("toggle", () => { if (output.open) state.openThoughts.add(key); else state.openThoughts.delete(key); });
    tool.append(output); body.append(tool);
  }
}

function nativeThoughts(row) {
  return [...(Array.isArray(row.memories) && row.memories.length ? [{kind: "memories", turn_id: row.turn_id, facts: row.memories}] : []), ...(row.trace || [])];
}

function rememberPrefill(row, body) {
  const selection = window.getSelection?.();
  const selected = selection && !selection.isCollapsed && body.contains(selection.anchorNode) && body.contains(selection.focusNode) ? String(selection) : "";
  return (selected || messageText(row)).slice(0, 500);
}

function appendRemember(parent, row, body) {
  if (!row.turn_id || !["completed", "failed", "interrupted"].includes(row.state)) return;
  const button = el("button", "send-action native-action", "Remember…"); button.type = "button";
  button.addEventListener("pointerdown", event => event.preventDefault());
  button.addEventListener("click", () => {
    state.rememberSource = {source_message_id: row.id, conversation_id: state.current};
    $("remember-text").value = rememberPrefill(row, body); $("remember-error").textContent = "";
    $("remember-sheet").showModal(); $("remember-text").focus();
  });
  parent.append(button);
}

async function saveRemember(event) {
  event.preventDefault();
  if (state.rememberBusy || !state.rememberSource) return;
  const scope = signInScope(), data = {text: $("remember-text").value, ...state.rememberSource};
  state.rememberBusy = true; $("remember-save").disabled = true;
  try {
    const result = await api("/memory", {method: "POST", body: JSON.stringify(data)});
    if (!scope.alive()) return;
    state.memoryData = result; $("remember-sheet").close();
    notice(result.used_tokens > result.budget_tokens ? "Memory saved. Over budget; model saves are paused." : "Memory saved.");
  } catch (error) { if (scope.alive()) $("remember-error").textContent = error.message; }
  finally { if (scope.alive()) { state.rememberBusy = false; $("remember-save").disabled = false; } }
}

// Collapsed by default; one opened stays open across the transcript's polls.
function thoughtsBlock(texts, key) {
  const details = el("details", "thoughts");
  details.open = state.openThoughts.has(key);
  details.append(el("summary", "", "Thoughts"));
  const body = el("div", "thoughts-body"); fillThoughts(body, texts); details.append(body);
  details.addEventListener("toggle", () => { if (details.open) state.openThoughts.add(key); else state.openThoughts.delete(key); });
  return details;
}

function replyUsage(row) {
  const usage = row.roost_usage || {};
  const count = (value) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
  const input = count(usage.input_tokens) ? usage.input_tokens.toLocaleString() : "—";
  const output = count(usage.output_tokens) ? usage.output_tokens.toLocaleString() : "—";
  if (row.turn_id != null) {
    const measured = value => typeof value === "number" && Number.isFinite(value) && value >= 0;
    const rate = measured(usage.tokens_per_second) ? usage.tokens_per_second.toFixed(1) : "—";
    const first = measured(usage.time_to_first_token) ? usage.time_to_first_token.toFixed(2) + " s" : "—";
    return `${input} in / ${output} out · ${rate} TPS · ${first} to first token`;
  }
  const duration = usage.model_seconds;
  const rate = count(usage.output_tokens) && typeof duration === "number" && Number.isFinite(duration) && duration > 0
    ? usage.output_tokens / duration : null;
  return `${input} in / ${output} out · ${rate !== null && Number.isFinite(rate) ? rate.toFixed(1) : "—"} TPS`;
}

function selectingTranscript() {
  const selection = window.getSelection?.();
  return Boolean(selection && !selection.isCollapsed && selection.rangeCount &&
    selection.getRangeAt(0).intersectsNode($("transcript")));
}

// Polling and streaming keep collecting content while the browser owns a text selection.
document.addEventListener("selectionchange", () => {
  if (state.selectionDeferred && !selectingTranscript() && state.current && state.lastRows) {
    renderMessages(state.lastRows);
  }
});

// Hermes can acknowledge a busy-session prompt before it becomes a durable transcript row.
// Returns the rows with this device's unsaved messages added, the sent messages still unsaved, and
// `savedAs`: for each sent message Hermes saved just now, its key and the id of the row that holds it.
function withSentMessages(rows, sent, current) {
  let pending = sent;
  const savedAs = {};
  for (const row of rows) {
    if (row.role !== "user" || typeof row.id !== "number") continue;
    if (row.client_request_id) {
      const matched = pending.find(item => item.id === current && item.request_id === row.client_request_id);
      if (matched) { savedAs[matched.key] = row.id; stopSendCheck(matched); pending = pending.filter(item => item !== matched); }
      continue;
    }
    const candidates = pending.filter((item) => (!item.failed || item.pending) && item.id === current && row.id > item.after);
    const saved = messageText(row);
    let combined = "";
    for (let index = 0; index < candidates.length; index++) {
      combined += (index ? "\n\n" : "") + candidates[index].text;
      if (combined === saved) {
        // Hermes can merge a queued burst into one user row. Consume exactly that prefix once.
        const matched = new Set(candidates.slice(0, index + 1));
        for (const item of matched) { savedAs[item.key] = row.id; stopSendCheck(item); }
        pending = pending.filter((item) => !matched.has(item));
        // Keep item identity: its check timer follows it until it is saved or discarded.
        for (const item of pending) if ((!item.failed || item.pending) && item.id === current) item.after = Math.max(item.after, row.id);
        break;
      }
      if (!saved.startsWith(combined)) break;
    }
  }
  return { sent: pending, savedAs, rows: [...rows, ...pending.filter((item) => item.id === current).map((item) => ({
    role: "user", content: item.text, timestamp: item.since / 1000, id: item.key, local_sent: !item.failed, local_failed: item.failed, local_pending: item.pending,
  }))] };
}

// What a conversation shows: Hermes's saved rows, this device's messages Hermes has not saved yet, and
// the turn being written live in place of the steps Hermes has saved for it. The server says whether the
// agent is working and where the live turn begins (`after`, paired with the turn by `started`), so
// nothing here matches messages by their text. `server` is the latest conversation response's
// { working, after, started, at }, or {} before there is one.
function conversationView({ rows, sent, current, live, server }) {
  const added = withSentMessages(rows, sent, current);
  let all = added.rows, liveAfter = null;
  const after = live ? live.after ?? (server.started != null && server.started === live.started ? server.after : null) : null;
  if (live?.turn_id != null) {
    const own = all.find(row => row.role === "user" && row.turn_id === live.turn_id);
    const saved = all.find(row => row.role === "assistant" && row.turn_id === live.turn_id);
    if (!own || live.done || (saved && (saved.revision > live.revision || ["completed", "failed", "interrupted"].includes(saved.state)))) live = null;
    else {
      liveAfter = own;
      all = all.filter(row => row.role !== "assistant" || row.turn_id !== live.turn_id);
    }
  } else if (live) {
    const own = after == null ? -1 : all.findIndex((row) => row.role === "user" && typeof row.id === "number" && row.id > after);
    const nextUser = own >= 0 ? all.findIndex((row, index) => index > own && row.role === "user") : -1;
    const last = own >= 0 ? all.slice(own + 1, nextUser >= 0 ? nextUser : undefined).at(-1) : null;
    const answered = last?.role === "assistant" && !(Array.isArray(last.tool_calls) && last.tool_calls.length) && Boolean(messageText(last));
    if (live.done && (answered || !server.working)) live = null;
    else if (own >= 0) {
      liveAfter = all[own];
      // Replace only this turn's saved steps. Later questions and replies stay in order.
      all = [...all.slice(0, own + 1), ...(nextUser >= 0 ? all.slice(nextUser) : [])];
    } else {
      // Not saved yet: the message just sent from here, or the one the stream says it answers.
      liveAfter = all.findLast((row) => row.local_sent) || { role: "user", content: live.user };
      if (!liveAfter.local_sent) all = [...all, liveAfter];
    }
  }
  const unsaved = added.sent.some((item) => !item.failed && item.id === current && item.since > (server.at ?? 0));
  return { rows: all, sent: added.sent, savedAs: added.savedAs, live, liveAfter, after,
           working: Boolean(live && !live.done) || Boolean(server.working) || unsaved };
}

// A quiet note where a summary takes over; tapping it (or "Read summary") opens the read-only sheet.
function openSummary(summary) {
  $("summary-text").textContent = summary.text || "";
  const written = messageTime(summary.created_at);
  $("summary-written").textContent = written ? `Written ${written}` : "";
  $("summary-sheet").showModal(); $("summary-title").focus();
}

function summaryDivider(summary) {
  const divider = el("div", "summary-divider");
  divider.append(el("span", "summary-divider-text", "Earlier messages summarized"));
  const read = el("button", "summary-read", "Read summary"); read.type = "button";
  divider.append(read);
  divider.addEventListener("click", () => openSummary(summary));
  return divider;
}

function currentSummaries() {
  const history = state.messageHistory;
  return history?.id === state.current && Array.isArray(history.summaries) ? history.summaries : [];
}

// Each divider follows the last loaded message at or before its boundary; none when that history is not paged in.
function summaryDividers(visible) {
  const placed = new Map();
  for (const summary of currentSummaries()) {
    if (!summary || !Number.isFinite(summary.through_ordinal)) continue;
    let after = null;
    for (const row of visible) if (row.ordinal != null && row.ordinal <= summary.through_ordinal) after = row;
    if (after) placed.set(after, [...(placed.get(after) || []), summary]);
  }
  return placed;
}

function renderMessages(rows) {
  const transcript = $("transcript");
  state.lastRows = rows;
  if (state.editTurn) { updateNativeComposer(); return; }
  if (state.renderedConversation === state.current && selectingTranscript()) {
    state.selectionDeferred = true;
    return;
  }
  state.selectionDeferred = false;
  const prepend = state.messageHistory?.prepend === true;
  const fresh = state.renderedConversation !== state.current;
  if (fresh) state.expandedUserMessages.clear();
  if (fresh) { state.shownTimes = new Set(); state.openThoughts = new Set(); }
  const view = conversationView({ rows: records(rows), sent: state.sentMessages, current: state.current,
    live: state.live?.id === state.current ? state.live : null, server: state.turn?.id === state.current ? state.turn : {} });
  state.sentMessages = view.sent;
  if (state.live?.id === state.current && !view.live) state.live = null;
  // A turn's start never moves: keep it, so later responses (once the turn is over) still place it.
  if (view.live && view.after != null) view.live.after = view.after;
  state.working = view.working;
  if (!isNative()) readVoiceReply(view);
  updateNativeComposer();
  renderContext();
  const all = view.rows, live = view.live, liveAfter = view.liveAfter;
  const signature = JSON.stringify([state.current, state.viewEpoch,
    all.map((row, index) => [row, index]).filter(([row]) => ["user", "assistant"].includes(row.role) && row.display_kind !== "hidden")
      .map(([row, index]) => [row.id ?? `at ${index}`, row.role, messageText(row), reasoningText(row), row.trace, row.sources, row.memories, row.persona_name, row.timestamp, row.roost_usage, row.state, row.revision,
        row.local_sent, row.local_failed, row.local_pending]),
    live, view.working, view.after, state.turn?.after, state.turn?.started,
    view.sent.filter((item) => item.id === state.current).map((item) => [item.key, item.failed, item.pending, item.retrying]),
    state.messageHistory?.hasOlder, state.messageHistory?.loading, currentSummaries(),
    [...state.shownTimes], [...state.openThoughts], state.liveThoughtsOpen !== false, state.playing,
    state.approvals, state.reasoning, state.timeZone, agent(), dayKey(new Date())]);
  // Reconciliation above still runs on every poll; Markdown and DOM work only follow a view change.
  if (prepend) state.messageHistory.prepend = false;
  if (signature === state.messageSignature) return;
  state.messageSignature = signature;
  // Follow new messages only when already at the bottom (or opening a conversation); otherwise keep the reader's place.
  const atBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  const keepScroll = transcript.scrollTop, keepHeight = transcript.scrollHeight;
  state.renderedConversation = state.current;
  show("chat-settings", Boolean(state.current));
  transcript.replaceChildren();
  state.liveNodes = null;
  if (state.messageHistory?.id === state.current && state.messageHistory.hasOlder) {
    const button = el("button", "secondary load-earlier", "Load earlier messages");
    button.id = "load-earlier"; button.type = "button";
    button.disabled = state.messageHistory.loading;
    button.addEventListener("click", loadEarlierMessages);
    transcript.append(button);
  }
  const visible = all.filter((row) => ["user", "assistant"].includes(row.role) && row.display_kind !== "hidden" && (messageText(row) || (isNative() && ["failed", "interrupted"].includes(row.state))));
  const content = JSON.stringify([visible.filter((row) => row.role === "assistant").map((row) => [row.id, messageText(row)]), live?.text || ""]);
  const changed = !fresh && !prepend && content !== state.latestContent;
  state.latestContent = content;
  if (fresh) $("jump-latest").classList.remove("unread");
  if (!visible.length) transcript.append(el("p", "empty-transcript", "No messages here yet."));
  // Thoughts from every step since the last message shown, including steps that only called tools,
  // go in one block above the next message; a turn still going shows its thoughts so far at the end.
  const thoughtsBefore = new Map(), shownRows = new Set(visible);
  let thoughts = [], thoughtsKey = null;
  all.forEach((row, index) => {
    const text = row.role === "assistant" && row.display_kind !== "hidden" ? reasoningText(row) : "";
    const items = isNative() && Array.isArray(row.trace) ? nativeThoughts(row) : text ? [text] : [];
    if (items.length) { thoughts.push(...items); thoughtsKey ??= String(row.id ?? `at ${index}`); }
    if (shownRows.has(row) && thoughts.length) { thoughtsBefore.set(row, [thoughts, thoughtsKey]); thoughts = []; thoughtsKey = null; }
  });
  const dividers = summaryDividers(visible);
  for (let index = 0; index < visible.length; index++) {
    const row = visible[index];
    if (thoughtsBefore.has(row)) transcript.append(thoughtsBlock(...thoughtsBefore.get(row)));
    const key = String(row.id ?? index), time = messageTime(row.timestamp);
    const wrapper = el("article", "message " + (row.role === "user" ? "from-you" : "from-agent"));
    const body = el("div", "message-body");
    if (row.role === "user") addUserMessage(body, messageText(row), key);
    else addReply(body, messageText(row), isNative() ? row.sources || [] : []);
    if (isNative() && row.role === "assistant") wrapper.append(el("div", "persona-label", replyName(row)));
    wrapper.append(body);
    transcript.append(wrapper);
    const shown = time && state.shownTimes.has(key);
    if (row.role === "user") {
      const status = isNative() && row.state === "queued" ? "Queued" : row.local_pending ? "Sending…" : row.local_failed ? "Not sent" : row.local_sent ? "Sent" : visible.slice(index + 1).some((next) => next.role === "assistant") ? "Read" : "Delivered";
      const under = el("div", "under right" + (row.local_failed ? " failed-send" : ""));
      under.append(el("span", "", shown ? `${status} - ${time}` : status));
      if (row.local_failed) {
        const item = state.sentMessages.find((item) => item.key === row.id);
        const retry = el("button", "send-action", item.retrying ? "Checking…" : item.pending ? "Check again" : "Retry");
        const remove = el("button", "send-action", "Delete");
        retry.type = remove.type = "button"; retry.disabled = remove.disabled = Boolean(item.retrying);
        retry.addEventListener("click", () => retryMessage(item));
        remove.addEventListener("click", () => {
          stopSendCheck(item);
          state.sentMessages = state.sentMessages.filter((sent) => sent !== item);
          if (state.current) renderMessages(state.lastRows || []); else renderWelcome();
        });
        under.append(retry, remove);
      }
      if (isNative()) { appendTurnControls(under, row, all, body); appendRemember(under, row, body); }
      transcript.append(under);
    } else {
      const under = el("div", "under reply-under");
      if (!isNative() && row.id != null) under.append(playButton(row.id));
      under.append(copyButton(messageText(row), "Copy reply"));
      if (isNative() && ["interrupted", "failed"].includes(row.state)) under.append(el("span", "", row.state === "failed" ? "Failed" : "Interrupted"));
      if (isNative()) { appendTurnControls(under, row, all, body); appendRemember(under, row, body); }
      if (shown || (isNative() && row.state === "completed")) {
        const details = el("span", "reply-usage", `${shown ? time + " · " : ""}${replyUsage(row)}`);
        details.title = isNative() ? "Measured output tokens per second and time to first token. — means no recorded measurement." : "Whole reply: input/output tokens across completed model calls, including thinking and tool rounds. TPS is output tokens per model-call second, including prompt processing; tool execution is excluded. — means no recorded measurement.";
        under.append(details);
      }
      if (under.children.length) transcript.append(under);
    }
    if (time) {
      body.classList.add("has-time");
      body.addEventListener("click", (event) => {
        // Selecting text or following a link is not a request for the time.
        if (event.target.closest("a") || String(window.getSelection())) return;
        if (state.shownTimes.has(key)) state.shownTimes.delete(key); else state.shownTimes.add(key);
        renderMessages(rows);
      });
    }
    for (const summary of dividers.get(row) || []) transcript.append(summaryDivider(summary));
    if (row === liveAfter) transcript.append(...liveNodes(live));
  }
  if (thoughts.length) transcript.append(thoughtsBlock(thoughts, thoughtsKey));
  if (view.working) {
    const typing = el("div", "typing-row");
    const dots = el("span", "typing-dots"); dots.append(el("i"), el("i"), el("i"));
    typing.append(dots, workingLabel()); transcript.append(typing);
  }
  renderConversations();
  transcript.scrollTop = prepend ? keepScroll + transcript.scrollHeight - keepHeight : fresh || atBottom ? transcript.scrollHeight : keepScroll;
  updateLatest(changed);
}

function updateLatest(changed = false) {
  const transcript = $("transcript"), button = $("jump-latest");
  const away = Boolean(state.current) && transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight >= 80;
  show("jump-latest", away);
  if (!away) button.classList.remove("unread");
  else if (changed) button.classList.add("unread");
  button.setAttribute("aria-label", button.classList.contains("unread") ? "Jump to latest, new reply" : "Jump to latest");
}

async function selectConversation(id, source) {
  if (state.composerKey !== undefined) saveComposer();
  state.viewEpoch++;
  resetMessagePolling();
  stopRecording(true);
  if (state.current !== id) { state.voiceDraft = false; leaveLive(); }
  state.messageHistory = null; state.lastRows = null;
  state.current = id; state.currentPersona = null; state.model = null; state.noWeb = false; state.webForce = false; state.editTurn = null; state.controlBusy = false; state.context = null;
  restoreComposer();
  state.draft = false;
  $("app").classList.add("chat-open");
  placePresence();
  reportPresence();
  const item = [...state.conversations, ...(state.searchResults || [])].find((row) => row.id === id);
  $("chat-title").textContent = item ? sessionName(item) : "Conversation";
  setChatAvatar(id);
  $("origin-badge").textContent = source && source !== "roost" ? source : "";
  show("origin-badge", Boolean(source && source !== "roost"));
  renderConversations();
  $("transcript").replaceChildren(el("p", "empty-transcript", "Loading conversation…"));
  show("jump-latest", false);
  showReasoning("default"); loadReasoning(id);
  try { await loadMessages(id); } catch (error) {
    if (state.current === id && state.sentMessages.some((item) => item.id === id && item.failed)) renderMessages([]);
    else if (isNative() && state.current === id) {
      const retry = el("button", "secondary", "Try again"); retry.type = "button";
      retry.addEventListener("click", () => selectConversation(id, source));
      const message = el("p", "empty-transcript", "Couldn’t load this conversation."); message.setAttribute("role", "alert");
      $("transcript").replaceChildren(message, retry);
    }
    notice(error.message);
  }
}

async function loadMessages(id = state.current) {
  if (!id) return;
  state.lastMessagePoll = Date.now();
  const scope = viewScope(id);
  const serial = state.messageRequest = state.messageRequest + 1, at = Date.now();
  const data = await api(`/conversations/${encodeURIComponent(id)}`);
  // Compare with the newest response applied, not the newest request issued: polls slower
  // than the interval would otherwise discard every reply and freeze the transcript.
  if (scope.alive() && serial > state.messageApplied) {
    state.messageApplied = serial;
    state.turn = { id, working: data.working === true, after: typeof data.after === "number" ? data.after : null,
                   started: typeof data.started === "number" ? data.started : null, turn_id: data.turn_id, at };
    if (isNative()) { state.context = data.context; state.currentPersona = data.persona_name || null; applyAgentName(); }
    if (data.live === true) openStream(id);
    else if (!state.turn.working && state.stream?.id === id && state.stream.healthy) {
      state.stream.healthy = false; resetMessagePolling();
    }
    let history = state.messageHistory;
    if (history?.id !== id) history = state.messageHistory = { id, rows: [], loaded: false, loading: false };
    history.rows = history.loaded && (!isNative() || data.has_older) ? mergeMessages(history.rows, data.messages, isNative()) : data.messages;
    if (!history.loaded) history.hasOlder = data.has_older === true;
    if (isNative()) history.summaries = Array.isArray(data.summaries) ? data.summaries : [];
    renderMessages(history.rows);
    messagePollDelay();
    if (!isNative()) loadApprovals(id);
  }
}

function messageOrder(a, b) {
  return a.ordinal != null && b.ordinal != null
    ? a.ordinal - b.ordinal || (a.role === b.role ? a.id - b.id : a.role === "user" ? -1 : 1)
    : a.id - b.id;
}

function mergeMessages(older, newer, snapshot = false) {
  const rows = new Map();
  // A native tail is authoritative: cancelled or edited rows must disappear, even after paging.
  const boundary = newer[0];
  const retained = snapshot ? older.filter(row => boundary && (row.ordinal < boundary.ordinal || (row.turn_id != null && row.turn_id === boundary.turn_id && messageOrder(row, boundary) < 0)) && !newer.some(next => next.id === row.id)) : older;
  for (const row of records([...retained, ...newer])) rows.set(String(row.id), row);
  return [...rows.values()].sort(messageOrder);
}

async function loadEarlierMessages() {
  const history = state.messageHistory, id = state.current, scope = viewScope(id);
  if (history?.id !== id || !history.hasOlder || history.loading || selectingTranscript()) return;
  const before = history.rows[0]?.id;
  if (!Number.isSafeInteger(before)) return;
  history.loading = true;
  $("load-earlier").disabled = true;
  try {
    const data = await api(`/conversations/${encodeURIComponent(id)}?before=${before}&limit=500`);
    if (!scope.alive() || state.messageHistory !== history) return;
    history.rows = mergeMessages(data.messages, history.rows);
    history.hasOlder = data.has_older === true;
    history.loaded = true;
    history.loading = false;
    history.prepend = true;
    renderMessages(history.rows);
  } catch (error) { report(error, scope); }
  finally {
    history.loading = false;
    if (scope.alive() && state.messageHistory === history) $("load-earlier").disabled = false;
  }
}

// Approvals belong to the exact Hermes request. A notification link only opens the card.
function setApprovals(items) {
  state.approvalRevision = state.approvalRevision + 1;
  const previous = new Set((state.approvals || []).map(item => item.request_id));
  state.approvals = items.filter(item => item && typeof item.request_id === "string");
  if (state.approvals.some(item => !previous.has(item.request_id))) state.approvalNotice = "";
  state.approvalReady = true;
  refreshWorkingLabels(); renderApprovals();
}

async function loadApprovals(id = state.current) {
  if (isNative() || !id || state.approvalLoading?.id === id) return;
  const scope = viewScope(id), revision = state.approvalRevision;
  const loading = {id}; state.approvalLoading = loading;
  try {
    const data = await api(`/conversations/${encodeURIComponent(id)}/approvals`);
    if (scope.alive() && revision === state.approvalRevision) {
      setApprovals(Array.isArray(data.approvals) ? data.approvals : []);
    }
  } catch {
    if (scope.alive()) { state.approvalReady = false; renderApprovals(); }
  } finally { if (state.approvalLoading === loading) state.approvalLoading = null; }
}

function renderApprovals() {
  const panel = $("approvals"), items = isNative() ? [] : state.approvals || [];
  const signature = JSON.stringify([items, state.approvalBusy, state.approvalReady, state.approvalNotice, state.approvalFocus]);
  if (panel.dataset.content === signature) return;
  panel.dataset.content = signature; panel.replaceChildren();
  for (const item of items) {
    const card = el("article", "approval-card");
    const heading = el("div", "approval-heading");
    heading.append(icon("warning"), el("strong", "", item.command ? `${agent()} wants to run a command` : `${agent()} needs your approval`));
    card.append(heading);
    if (item.description) card.append(el("p", "approval-summary", item.description));
    if (item.command) card.append(el("pre", "approval-command", item.command));
    const buttons = el("div", "approval-actions");
    for (const [choice, label] of [["once", "Allow once"], ["deny", "Deny"]]) {
      if (!Array.isArray(item.choices) || !item.choices.includes(choice)) continue;
      const button = el("button", choice === "once" ? "primary" : "secondary", label);
      button.type = "button"; button.disabled = Boolean(state.approvalBusy) || !state.approvalReady;
      button.addEventListener("click", () => decideApproval(item.request_id, choice));
      buttons.append(button);
    }
    card.append(buttons); panel.append(card);
  }
  if (items.length && !state.approvalReady) panel.append(el("p", "approval-status", "Reconnecting. Decisions are unavailable until Roost can check this request."));
  if (state.approvalNotice) panel.append(el("p", "approval-status", state.approvalNotice));
  else if (state.approvalFocus && state.approvalFocusConversation === state.current && state.approvalReady && !items.some(item => item.request_id === state.approvalFocus)) {
    panel.append(el("p", "approval-status", "That request is no longer waiting for approval."));
  }
  show("approvals", panel.children.length > 0);
}

async function decideApproval(requestId, choice) {
  if (state.approvalBusy || !state.approvalReady || !["once", "deny"].includes(choice)) return;
  const id = state.current, scope = viewScope(id);
  state.approvalBusy = requestId; state.approvalNotice = ""; renderApprovals();
  try {
    await api(`/conversations/${encodeURIComponent(id)}/approvals`, {
      method: "POST", body: JSON.stringify({request_id: requestId, choice})});
    if (scope.alive()) {
      state.approvalRevision = state.approvalRevision + 1;
      state.approvals = state.approvals.filter(item => item.request_id !== requestId);
      state.approvalNotice = choice === "once" ? "Allowed once." : "Denied.";
    }
  } catch (error) {
    if (scope.alive()) {
      state.approvalNotice = error.message; state.approvalReady = false;
    }
  } finally {
    if (scope.alive()) {
      state.approvalBusy = null; renderApprovals(); loadApprovals(id);
    }
  }
}

// A reply being written arrives as an event stream of its thoughts and text. The transcript stays
// the record: a stream that can't open or ends early leaves the turn to polling, as before.
function openStream(id) {
  if (typeof EventSource !== "function" || state.stream?.id === id) return;
  closeStream();
  const source = new EventSource(`/api/conversations/${encodeURIComponent(id)}/stream`);
  state.lastMessagePoll = Date.now();
  const ours = () => state.stream?.source === source;
  state.stream = { id, source, healthy: false };
  source.onopen = () => { if (ours()) { state.stream.healthy = true; state.idleSince = null; } };
  source.onmessage = (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (!ours() || !data || typeof data !== "object") return;
    if (isNative() && data.reset && data.turn_id !== state.live?.turn_id) loadMessages(id).catch(() => {});
    // Every connection, reconnections included, starts with the turn so far.
    if (data.reset) {
      if (state.live?.id !== id || state.live.started !== data.started || state.live.turn_id !== data.turn_id) {
        state.liveThoughtsOpen = !window.roostTheme.collapseThinking();
        state.liveNodes = null;
      }
      state.live = applyStreamFrame(null, data, id);
    }
    if (isNative() && Array.isArray(data.summaries) && state.messageHistory?.id === id) state.messageHistory.summaries = data.summaries;
    if (state.live?.id !== id) return;
    state.stream.healthy = true; state.idleSince = null;
    state.live = applyStreamFrame(state.live, data, id);
    if (!isNative() && Array.isArray(data.approvals)) setApprovals(data.approvals);
    if (data.done) endStream(id);
    else if (isNative()) renderMessages(state.lastRows || []);
    else scheduleLive();
  };
  // The browser retries a dropped connection by itself; a refused one (the turn is over) ends here.
  source.onerror = () => {
    if (!ours()) return;
    state.stream.healthy = false; resetMessagePolling();
    if (source.readyState === 2) endStream(id);
  };
}

function applyStreamFrame(previous, data, id) {
  const live = data.reset || !previous ? {id, user: String(data.user ?? ""), started: data.started,
    turn_id: data.turn_id, revision: data.revision, state: data.state, thoughts: "", text: "", done: false} : {...previous};
  if (typeof data.thoughts === "string") live.thoughts += data.thoughts;
  if (typeof data.text === "string") live.text += data.text;
  if (Array.isArray(data.trace)) live.trace = data.trace;
  if (Array.isArray(data.sources)) live.sources = data.sources;
  if (Array.isArray(data.memories)) live.memories = data.memories;
  if (typeof data.persona_name === "string") live.persona_name = data.persona_name;
  live.done = Boolean(data.done);
  return live;
}

function endStream(id) {
  closeStream();
  if (state.live?.id === id) state.live.done = true;
  if (state.current === id) loadMessages(id).catch(() => {});
}

function closeStream() {
  state.stream?.source.close();
  state.stream = null;
  resetMessagePolling();
}

function resetMessagePolling() { state.lastMessagePoll = null; state.idleSince = null; }

function messagePollDelay() {
  const active = state.busy || state.working || (state.turn?.id === state.current && state.turn.working) ||
    (state.live?.id === state.current && !state.live.done) ||
    state.sentMessages.some((item) => item.id === state.current && (!item.failed || item.pending || item.retrying));
  if (active) state.idleSince = null;
  else state.idleSince ??= Date.now();
  if (state.stream?.id === state.current && state.stream.healthy && state.stream.source.readyState === 1) return 15000;
  return state.idleSince !== null && Date.now() - state.idleSince > 60000 ? 10000 : 3000;
}

// Another conversation is opening: nothing live or rendered here belongs to it.
function leaveLive() {
  stopRecording(true); state.voiceDraft = false;
  closeStream();
  Object.assign(state, { approvals: [], approvalBusy: null, approvalNotice: "", approvalReady: false, approvalLoading: null, selectionDeferred: false, live: null, liveNodes: null, lastRows: null });
  renderApprovals();
}

function scheduleLive() {
  if (state.liveFrame) return;
  const frame = window.requestAnimationFrame?.bind(window) || ((callback) => setTimeout(callback, 16));
  state.liveFrame = frame(() => { state.liveFrame = 0; renderLive(); });
}

// Each turn starts with the device preference; a manual toggle lasts for that turn.
function liveNodes(live) {
  const details = el("details", "thoughts");
  details.open = state.liveThoughtsOpen !== false;
  details.append(el("summary", "", "Thoughts"));
  const thoughts = el("div", "thoughts-body"); details.append(thoughts);
  details.addEventListener("toggle", () => { state.liveThoughtsOpen = details.open; });
  const message = el("article", "message from-agent"), body = el("div", "message-body"); message.append(body);
  state.liveNodes = { details, thoughts, message, body, shownThoughts: null, shownText: null };
  fillLive(live);
  return [details, message];
}

function fillLive(live) {
  const nodes = state.liveNodes;
  const items = isNative() && Array.isArray(live.trace) ? nativeThoughts(live) : live.thoughts.trim() ? [live.thoughts] : [];
  const signature = JSON.stringify(items), reply = JSON.stringify([live.text, live.sources, live.persona_name]);
  if (nodes.shownThoughts !== signature) { fillThoughts(nodes.thoughts, items); nodes.shownThoughts = signature; }
  if (nodes.shownText !== reply) { nodes.body.replaceChildren(); if (isNative()) nodes.body.append(el("div", "persona-label", replyName(live))); addReply(nodes.body, live.text, live.sources || []); nodes.shownText = reply; }
  nodes.details.classList.toggle("hidden", !items.length);
  nodes.message.classList.toggle("hidden", !live.text.trim());
}

// Only the live nodes change while text arrives; the rest of the transcript stays as it is.
function renderLive() {
  const live = state.live;
  if (!live || live.id !== state.current || !state.lastRows) return;
  if (selectingTranscript()) { state.selectionDeferred = true; return; }
  if (!state.liveNodes) return renderMessages(state.lastRows);
  const transcript = $("transcript");
  const atBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  const changed = state.liveNodes.shownText !== JSON.stringify([live.text, live.sources, live.persona_name]);
  fillLive(live);
  if (atBottom) transcript.scrollTop = transcript.scrollHeight;
  updateLatest(changed);
}

// How hard the model thinks in the open conversation; a new one starts at Hermes's own setting.
function showReasoning(level) {
  state.reasoning = level;
  $("reasoning").value = level;
  $("reasoning-chip").classList.toggle("chosen", level !== "default");
  if (isNative()) updateThinking();
}

async function loadReasoning(id) {
  const scope = viewScope(id);
  try {
    const data = await api(`/conversations/${encodeURIComponent(id)}/settings`);
    if (scope.alive() && typeof data.reasoning === "string") { state.model = data.model || null; state.noWeb = Boolean(data.no_web); showReasoning(data.reasoning); }
  } catch { /* The chip keeps Default; the next send still uses what Roost stored. */ }
}

async function changeReasoning() {
  const level = $("reasoning").value, previous = state.reasoning || "default", id = state.current;
  showReasoning(level);
  // A new conversation sends its choice with the first message.
  if (!id) return;
  const scope = signInScope();
  try {
    const data = await api(`/conversations/${encodeURIComponent(id)}/settings`, { method: "PUT", body: JSON.stringify({ reasoning: level }) });
    if (scope.alive() && state.current === id) showReasoning(data.reasoning);
  } catch (error) {
    if (!scope.alive()) return;
    if (state.current === id) showReasoning(previous);
    notice(error.message);
  }
}

function newConversation() {
  if (state.composerKey !== undefined) saveComposer();
  state.viewEpoch++;
  leaveLive();
  state.current = null; state.model = null; state.noWeb = false; state.webForce = false; state.editTurn = null; state.controlBusy = false; state.context = null;
  restoreComposer();
  state.draft = true;
  newDraftSeed();
  showReasoning(isNative() ? "none" : "default");
  updateNativeComposer(); renderContext();
  $("app").classList.add("chat-open");
  placePresence();
  renderConversations(); renderWelcome();
  if (!touchScreen.matches) $("message").focus();
}

async function submitMessage(event) {
  event.preventDefault();
  if (state.busy || voice.take) return;
  const text = expandPastes($("message").value).trim();
  if (text.length > 16000) { notice("The message is too long. Shorten the draft before sending."); return; }
  // Sending a voice draft is the tap that lets its reply play by itself later (Safari wants one).
  // Not the microphone tap: on iOS, audio starting while the microphone opens cuts the recording short.
  if (isNative() && !text && (runningTurn() || queuedTurn())) { await stopNativeReply(); return; }
  if (text && state.voiceDraft) unlockPlayer();
  if (text) await sendText(text, { voice: Boolean(state.voiceDraft) });
}

// Drafts and their folded pastes survive page eviction; blocked storage only loses persistence.
function composerDrafts() {
  if (state.composerDrafts === null) {
    let rows;
    try { rows = JSON.parse(localStorage.getItem("roost.composer-drafts")); } catch { /* Use memory only. */ }
    state.composerDrafts = Array.isArray(rows) ? rows.filter((row) => row && typeof row.id === "string" &&
      typeof row.text === "string" && row.text.length <= 16000).slice(-50) : [];
  }
  return state.composerDrafts;
}

function draftPastes() {
  const row = composerDrafts().find((row) => row.id === (state.current || "new"));
  return Array.isArray(row?.pastes) ? row.pastes.filter((paste) => paste && typeof paste.token === "string" &&
    typeof paste.text === "string" && paste.text.length <= 16000 && row.text.includes(paste.token)) : [];
}

function expandPastes(text, pastes = draftPastes()) {
  const values = new Map(pastes.map((paste) => [paste.token, paste.text]));
  return text.replace(/\[Pasted text #\d+ · \d+ lines\]/g, (token) => values.get(token) ?? token);
}

function pasteText(event) {
  const text = event.clipboardData?.getData("text/plain") || "";
  if (!longText(text)) return;
  event.preventDefault();
  const input = $("message"), start = input.selectionStart, end = input.selectionEnd;
  const pastes = draftPastes();
  let number = 1, token;
  do { token = `[Pasted text #${number++} · ${textLines(text).length} lines]`; }
  while (input.value.includes(`[Pasted text #${number - 1} ·`));
  const value = input.value.slice(0, start) + token + input.value.slice(end);
  pastes.push({ token, text });
  if (value.length > 16000 || expandPastes(value, pastes).length > 16000) { notice("The paste won’t fit in this message. Shorten the draft and try again."); return; }
  input.setRangeText(token, start, end, "end");
  saveComposer(input.value, state.current, pastes);
  $("composer").classList.add("has-text");
}

function saveComposer(text = $("message").value, id = state.current, pastes) {
  const key = id || "new";
  pastes ??= composerDrafts().find((row) => row.id === key)?.pastes;
  state.composerDrafts = composerDrafts().filter((row) => row.id !== key);
  if (text) {
    const row = { id: key, text: text.slice(0, 16000) };
    if (Array.isArray(pastes)) row.pastes = pastes.filter((paste) => paste && typeof paste.token === "string" && row.text.includes(paste.token));
    state.composerDrafts.push(row);
  }
  state.composerDrafts = state.composerDrafts.slice(-50);
  try { localStorage.setItem("roost.composer-drafts", JSON.stringify(state.composerDrafts)); } catch { /* Use memory only. */ }
  if (id === state.current) state.composerKey = id || "new";
}

function restoreComposer() {
  state.composerKey = state.current || "new";
  $("message").value = composerDrafts().find((row) => row.id === state.composerKey)?.text || "";
  $("composer").classList.toggle("has-text", Boolean($("message").value.trim()));
}

// One cryptographic identity per send, independent of its text and all later retries.
function sendId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function stopSendCheck(item) { clearTimeout(item.checkTimer); item.checkTimer = null; }

function checkSendLater(item) {
  stopSendCheck(item);
  const remaining = PENDING_CHECK_LIMIT_MS - (Date.now() - item.since), scope = signInScope();
  if (!item.pending || !state.sentMessages.includes(item) || remaining <= 0) return;
  item.checkTimer = setTimeout(async () => {
    item.checkTimer = null;
    if (!scope.alive() || !item.pending || !state.sentMessages.includes(item) || Date.now() - item.since >= PENDING_CHECK_LIMIT_MS) return;
    try { await retryMessage(item, true); }
    finally { if (scope.alive()) checkSendLater(item); }
  }, Math.min(PENDING_CHECK_MS, remaining));
}

async function retryMessage(item, automatic = false) {
  if (!item || item.retrying || state.busy || voice.take) return;
  stopSendCheck(item);
  const scope = signInScope();
  item.retrying = true;
  if (state.current === item.id) renderMessages(state.lastRows || []);
  try {
    if (!state.sentMessages.includes(item)) return;
    if (item.voice && !automatic) unlockPlayer();
    await sendText(item.text, { voice: item.voice, retry: item });
  } catch (error) { report(error, scope); }
  finally {
    if (scope.alive()) {
      item.retrying = false;
      if (state.current === item.id) renderMessages(state.lastRows || []);
    }
  }
}

function acceptMessage(item, id, here) {
  stopSendCheck(item);
  const oldId = item.id;
  if (!oldId) {
    const draft = composerDrafts().find((row) => row.id === "new");
    saveComposer("", null);
    if (draft) saveComposer(draft.text, id, draft.pastes);
    for (const sent of state.sentMessages) if (sent.id === null) sent.id = id;
  }
  if (here.alive()) {
    if (!oldId) { state.current = id; saveComposer(); }
    state.draft = false;
  }
  item.id = id; item.failed = false; item.pending = false;
  if (item.voice) state.voiceReply = { id, key: item.key, row: null, since: item.since };
  if (state.current === id) {
    closeStream(); state.live = null; state.liveNodes = null; openStream(id);
    nextWorkingVerb(); renderMessages(state.lastRows || []);
  }
  const scope = signInScope();
  // Refreshing can take its own timeout; the composer is already free for the next message.
  Promise.allSettled([loadConversations(), loadMessages()]).then((updates) => {
    if (scope.alive() && updates.some((update) => update.status === "rejected")) {
      notice("Message sent. The conversation could not refresh; it will retry shortly.");
    }
  });
}

// Sends the composer or one failed row, without consuming a newer draft on Retry.
async function sendText(text, { voice = false, retry = null } = {}) {
  if (state.busy) return;
  resetMessagePolling();
  const oldId = retry ? retry.id : state.current, scope = signInScope(), here = viewScope(oldId);
  const after = Math.max(0, ...records(state.lastRows || []).filter((row) => row.role === "user" && typeof row.id === "number").map((row) => row.id));
  const item = retry || { id: oldId, text, voice, after, key: `sent-${state.sentKey = state.sentKey + 1}`,
    request_id: sendId(), model: state.model, web: isNative() && state.webForce ? "force" : undefined, reasoning: state.reasoning || (isNative() ? "none" : "default"), since: Date.now() };
  if (!retry) {
    state.webForce = false; updateWebToggle();
    state.voiceDraft = false; $("message").value = ""; saveComposer();
    $("composer").classList.remove("has-text");
  }
  state.busy = true; $("send").disabled = true;
  const sending = el("p", "empty-transcript", "Sending…");
  $("transcript").append(sending);
  $("transcript").scrollTop = $("transcript").scrollHeight;
  let result, failure;
  try {
    result = oldId
      ? await api(`/conversations/${encodeURIComponent(oldId)}/messages`, { method: "POST", body: JSON.stringify({ text, request_id: item.request_id, ...(isNative() && item.web ? {web: item.web} : {}) }) })
      : await api("/conversations", { method: "POST", body: JSON.stringify({ text, reasoning: item.reasoning, request_id: item.request_id, ...(isNative() ? {model: item.model || null, ...(item.web ? {web: item.web} : {})} : {}) }) });
  } catch (error) { failure = error; }
  finally {
    if (scope.alive()) { state.busy = false; $("send").disabled = false; }
  }
  try {
    if (!scope.alive()) return;
    if (retry && !state.sentMessages.includes(retry)) return;
    if (!retry) state.sentMessages.push(item);
    if (result) {
      acceptMessage(item, result.session_id, here);
    } else {
      item.failed = true;
      item.pending = Boolean(failure.pending || failure.status >= 500 || ["TimeoutError", "AbortError", "TypeError"].includes(failure.name));
      report(failure, scope);
      if (state.current === oldId) renderMessages(state.lastRows || []);
      checkSendLater(item);
    }
  } finally { sending.remove(); }
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
  const hour = Number(match[2]), at = (hour % 12 || 12) + ":" + match[1].padStart(2, "0") + (hour < 12 ? " AM" : " PM");
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
  $("job-schedule-preview").textContent = !schedule ? "Example: 0 8 * * * runs daily at 8:00 AM." :
    label === schedule ? "This schedule will be sent to Hermes as entered." : "Runs " + label.toLowerCase() + ".";
}

function renderJobs() {
  const list = $("sidebar-jobs-list"); list.replaceChildren();
  const rows = state.jobs.filter((job) =>
    [jobValue(job, "name"), jobValue(job, "prompt"), jobScheduleLabel(job)].join(" ").toLowerCase().includes(state.jobSearch));
  if (!rows.length) list.append(el("p", "empty-list", state.jobSearch ? "No matching jobs." : `No jobs yet. Add one for ${agent()} to handle on a schedule.`));
  for (const job of rows) {
    const id = job.id;
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
  return state.jobs.find((job) => job.id === state.selectedJob);
}

function selectJob(id) {
  state.selectedJob = id;
  $("app").classList.add("job-open");
  placePresence();
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
  const stateTag = el("span", "job-state");
  stateTag.append(el("span", "pill " + status.kind, status.label));
  top.append(stateTag);
  const paused = jobPaused(job);
  const toggle = el("button", "job-toggle" + (paused ? " off" : ""));
  toggle.disabled = state.jobBusy;
  for (const id of ["run-selected-job", "edit-selected-job"]) $(id).disabled = state.jobBusy;
  toggle.type = "button"; toggle.setAttribute("role", "switch");
  toggle.setAttribute("aria-checked", String(!paused));
  toggle.setAttribute("aria-label", (paused ? "Resume " : "Pause ") + (jobValue(job, "name") || "job"));
  toggle.addEventListener("click", () => jobAction(job.id, paused ? "resume" : "pause"));
  const enabled = el("label", "job-enabled");
  enabled.append(el("span", "", paused ? "Paused" : "Enabled"), toggle);
  top.append(enabled);
  summary.append(top);
  // Hermes keeps no report on the job itself; its newest run's preview is the last report.
  const latestRun = state.jobRuns[job.id]?.[0];
  const facts = [["Last run", timeLabel(job.last_run_at || latestRun?.started_at || latestRun?.created_at || latestRun?.timestamp) || "Not yet"],
                 ["Delivery", jobValue(job, "deliver") === "local" ? "Saved in Hermes" : jobValue(job, "deliver") || "Saved in Hermes"]];
  const stats = el("dl", "stats");
  for (const [term, value] of facts) { const item = el("div"); item.append(el("dt", "", term), el("dd", "", value)); stats.append(item); }
  summary.append(stats);
  detail.append(summary);
  const report = el("section", "job-detail-section");
  report.append(el("h2", "", "Last report"),
                el("div", "job-report", job.last_run_preview || job.last_output || job.last_report || latestRun?.preview ||
                   `No report yet. ${agent()}’s next run will appear here.`));
  detail.append(report);
  const instructions = el("section", "job-detail-section");
  instructions.append(el("h2", "", `What ${agent()} will do`), el("p", "job-prompt", jobValue(job, "prompt") || "No instructions provided."));
  detail.append(instructions);
  const history = el("section", "job-detail-section job-history");
  const historyHeading = el("div", "job-history-heading");
  const refresh = el("button", "text-button", "Refresh");
  refresh.type = "button";
  refresh.setAttribute("aria-label", "Refresh recent runs");
  refresh.addEventListener("click", () => loadJobRuns(job.id).catch((error) => notice(error.message)));
  historyHeading.append(el("h2", "", "Recent runs"), refresh);
  history.append(historyHeading);
  const runs = state.jobRuns[job.id];
  if (!runs) history.append(el("p", "job-history-empty", state.jobRunsError[job.id] ?
    "Recent runs could not load. Use Refresh to try again." : "Loading recent runs…"));
  else if (!runs.length) history.append(el("p", "job-history-empty", "No runs yet."));
  else for (const run of runs) {
    const row = el("article", "run-row");
    const heading = el("div", "run-heading");
    const title = el("span", "run-title");
    title.append(el("strong", "", run.title || run.status || run.state || "Run"));
    heading.append(title, el("span", "", timeLabel(run.started_at || run.created_at || run.timestamp)));
    row.append(heading);
    const report = run.output || run.result || run.summary || run.report || run.preview || "";
    if (report) row.append(el("pre", "", typeof report === "string" ? report : JSON.stringify(report, null, 2)));
    if (run.id && run.source !== "cron_output") {
      const open = el("button", "text-button", "Open conversation");
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
  const scope = signInScope(), loading = state.jobRunsLoading;
  loading.add(id);
  try {
    const data = await api(`/jobs/${encodeURIComponent(id)}/runs`);
    delete state.jobRunsError[id];
    state.jobRuns[id] = records(data.runs);
    if (state.selectedJob === id) renderJobDetail();
  } catch (error) {
    if (scope.alive()) { state.jobRunsError[id] = true; if (state.selectedJob === id) renderJobDetail(); }
    throw error;
  } finally { loading.delete(id); }
}

// Hermes answers a create, edit, pause or resume with the job as saved. Show that at once, and keep
// it over the next few list refreshes: a list read straight after a change has come back without it.
const JOB_PATCH_MS = 30000;

function applyJob(job) {
  const id = job && typeof job === "object" && job.id;
  if (!id) return;
  state.jobPatches[id] = { job, at: Date.now() };
  state.jobs = withJobPatches(state.jobs);
  renderJobs();
}

function withJobPatches(rows) {
  const now = Date.now(), merged = [...rows];
  for (const [id, patch] of Object.entries(state.jobPatches)) {
    if (now - patch.at > JOB_PATCH_MS) { delete state.jobPatches[id]; continue; }
    const index = merged.findIndex((job) => job.id === id);
    if (index >= 0) merged[index] = patch.job; else merged.push(patch.job);
  }
  return merged;
}

async function loadJobs() {
  const serial = state.jobsRequest = state.jobsRequest + 1;
  const data = await api("/jobs");
  if (serial !== state.jobsRequest) return;
  state.jobs = withJobPatches(records(data.jobs));
  if (!state.selectedJob || !state.jobs.some((job) => job.id === state.selectedJob)) {
    state.selectedJob = state.jobs[0]?.id || null;
  }
  renderJobs();
  if (state.selectedJob) await loadJobRuns(state.selectedJob).catch((error) => notice(error.message));
}

async function jobAction(id, action) {
  if (state.jobBusy) return;
  const scope = signInScope();
  state.jobBusy = true; renderJobDetail();
  try {
    const result = await api(`/jobs/${encodeURIComponent(id)}/${action}`, { method: "POST" });
    notice(action === "trigger" ? `${agent()} is running this Job.` : `Job ${action === "pause" ? "paused" : "resumed"}.`);
    if (action !== "trigger") applyJob(result);
    await loadJobs();
  } catch (error) { report(error, scope); }
  finally { if (scope.alive()) { state.jobBusy = false; renderJobDetail(); } }
}

async function deleteJob(job) {
  if (state.jobBusy) return;
  const name = jobValue(job, "name") || "this Job";
  if (!window.confirm(`Delete “${name}”? This cannot be undone.`)) return;
  const scope = signInScope();
  state.jobBusy = true;
  try {
    await api(`/jobs/${encodeURIComponent(job.id)}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) });
    delete state.jobRuns[job.id]; delete state.jobRunsError[job.id]; delete state.jobPatches[job.id];
    $("job-dialog").close(); state.selectedJob = null; $("app").classList.remove("job-open"); notice("Job deleted."); await loadJobs();
  } catch (error) { report(error, scope); }
  finally { if (scope.alive()) { state.jobBusy = false; renderJobDetail(); } }
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
  const scope = signInScope();
  const buttons = [...$("job-form").querySelectorAll("button")];
  buttons.forEach((button) => { button.disabled = true; });
  const data = { name: $("job-name").value.trim(), prompt: $("job-prompt").value.trim(),
                 schedule: $("job-schedule").value.trim(), deliver: $("job-deliver").value.trim() };
  const id = state.job?.id;
  try {
    const result = await api(id ? `/jobs/${encodeURIComponent(id)}` : "/jobs", { method: id ? "PUT" : "POST", body: JSON.stringify(data) });
    $("job-dialog").close(); notice(id ? "Job updated." : "Job created.");
    applyJob(result);
    if (!id) {
      state.selectedJob = result.id || null;
      await loadJobs();
      if (state.selectedJob) selectJob(state.selectedJob);
    } else await loadJobs();
  } catch (error) { report(error, scope); }
  finally {
    if (scope.alive()) {
      state.jobBusy = false; buttons.forEach((button) => { button.disabled = false; }); renderJobDetail();
    }
  }
}

// Settings: a list of groups in the sidebar and the selected group in the main pane, like Jobs.
function thinkingVisible(models, selected, defaultModel) {
  const model = models.find(model => model.id === (selected || defaultModel));
  return Boolean(model && model.thinking_mode !== "unsupported");
}

async function loadModels() {
  const data = await api("/models");
  state.models = data.models || []; state.defaultModel = data.default_model; state.webAvailable = Boolean(data.web_available);
  if (isNative()) updateThinking();
}

function updateWebToggle() {
  const model = state.models.find(item => item.id === (state.model || state.defaultModel));
  const available = isNative() && state.webAvailable && model && model.tools !== false && !state.noWeb;
  if (!available) state.webForce = false;
  show("web-toggle", Boolean(available));
  $("web-toggle").setAttribute("aria-pressed", String(state.webForce));
}

function updateThinking() {
  updateWebToggle();
  show("reasoning-chip", thinkingVisible(state.models, state.model, state.defaultModel));
  $("reasoning").querySelectorAll('option[value="default"]').forEach(option => { option.hidden = true; });
}

function fillModelPicker(select, selected) {
  select.replaceChildren();
  const option = el("option", "", "Server default"); option.value = ""; select.append(option);
  for (const model of state.models) {
    const item = el("option", "", model.display_name); item.value = model.id; select.append(item);
  }
  // Keep a disabled model override visible until the owner chooses a replacement.
  if (selected && !state.models.some(model => model.id === selected)) {
    const item = el("option", "", selected + " (unavailable)"); item.value = selected; item.disabled = true; select.append(item);
  }
  select.value = selected || "";
}

function updateSheetThinking() {
  show("sheet-thinking", thinkingVisible(state.models, $("sheet-model").value, state.defaultModel));
}

function turnControls(row, rows) {
  const settled = ["completed", "interrupted", "failed"].includes(row.state);
  return { queue: row.role === "user" && row.state === "queued", edit: row.role === "user" && settled,
    regenerate: row.role === "assistant" && settled && rows.at(-1)?.turn_id === row.turn_id };
}

function runningTurn() {
  return (state.live && !state.live.done && state.live.state !== "queued" ? state.live.turn_id : null) ||
    state.lastRows?.find(row => row.state === "running")?.turn_id;
}

function queuedTurn() {
  return state.lastRows?.find(row => row.state === "queued")?.turn_id ||
    (state.live?.state === "queued" && !state.live.done ? state.live.turn_id : null);
}

function stopNativeReply() {
  const running = runningTurn(), queued = queuedTurn();
  if (running || queued) return nativeControl(running ? "stop" : "cancel", running || queued);
}

function updateNativeComposer() {
  updateWebToggle();
  if (!isNative()) return;
  const running = Boolean(runningTurn() || queuedTurn()), send = $("send");
  send.type = running ? "button" : "submit";
  send.textContent = running ? "Stop" : "↑";
  send.setAttribute("aria-label", running ? "Stop reply" : "Send message");
  send.classList.toggle("stopping", running);
  send.disabled = state.busy || state.controlBusy;
  show("queue-send", running && Boolean($("message").value.trim()));
  $("queue-send").disabled = state.busy;
}

async function nativeControl(action, turnId, extra = {}) {
  if (state.controlBusy) return;
  const id = state.current, scope = viewScope(id);
  state.controlBusy = true; updateNativeComposer();
  try {
    await api(`/conversations/${encodeURIComponent(id)}/turns/${action}`, {
      method: "POST", body: JSON.stringify({turn_id: turnId, ...extra})});
    if (scope.alive()) {
      state.editTurn = null; state.live = null; closeStream(); state.messageHistory = null;
      await loadMessages(id);
    }
  } catch (error) {
    report(error, scope);
    // Reconcile an uncertain result before the owner attempts another control.
    if (scope.alive()) { state.editTurn = null; await loadMessages(id).catch(() => {}); }
  } finally {
    if (scope.alive()) { state.controlBusy = false; state.messageSignature = null; renderMessages(state.lastRows || []); }
  }
}

function appendTurnControls(under, row, rows, body) {
  const controls = turnControls(row, rows);
  const button = (text, label, action) => {
    const node = el("button", "send-action native-action", text);
    node.type = "button"; node.disabled = state.controlBusy; node.setAttribute("aria-label", label);
    node.addEventListener("click", event => { event.stopPropagation(); action(); }); under.append(node);
  };
  if (controls.queue) {
    button("×", "Cancel queued message", () => nativeControl("cancel", row.turn_id));
    button("↑", "Send queued message now", () => nativeControl("send-now", row.turn_id));
  }
  if (controls.edit) button("Edit", "Edit message", () => {
    state.editTurn = row.turn_id; body.classList.add("editing");
    const form = el("form", "inline-edit"), input = el("textarea");
    input.value = messageText(row); input.maxLength = 16000; input.required = true; input.rows = 4;
    input.setAttribute("aria-label", "Edit message");
    const later = rows.some(next => next.turn_id !== row.turn_id && next.ordinal > row.ordinal);
    const warning = el("p", "sheet-note", later ? "Saving drops all later messages, including the queue." : "Saving sends this message again.");
    const actions = el("div", "settings-actions"), cancel = el("button", "secondary", "Cancel"), save = el("button", "primary", "Save and resend");
    cancel.type = "button"; save.type = "submit";
    cancel.addEventListener("click", () => { state.editTurn = null; state.messageSignature = null; renderMessages(state.lastRows || []); });
    form.addEventListener("click", event => event.stopPropagation());
    form.addEventListener("submit", event => {
      event.preventDefault(); const text = input.value.trim(); if (!text) return;
      save.disabled = cancel.disabled = true;
      nativeControl("edit", row.turn_id, {text, request_id: sendId()});
    });
    actions.append(cancel, save); form.append(input, warning, actions); body.replaceChildren(form); input.focus();
  });
  if (controls.regenerate) button(row.state === "failed" ? "Retry" : "Regenerate", row.state === "failed" ? "Retry reply" : "Regenerate reply", () => nativeControl("regenerate", row.turn_id));
}

function renderContext() {
  const context = state.context, meter = $("context-meter");
  const visible = isNative() && context?.prompt_tokens != null && context?.context_tokens != null;
  show("context-meter", visible);
  if (!visible) return;
  meter.replaceChildren();
  const label = `${context.prompt_tokens.toLocaleString()} / ${context.context_tokens.toLocaleString()} tokens`;
  const progress = el("meter"); progress.min = 0; progress.max = context.context_tokens; progress.value = context.prompt_tokens;
  progress.setAttribute("aria-label", "Context: " + label);
  meter.append(el("span", "", "Context · " + label), progress);
}

let searchTimer;
function queueSearch() {
  clearTimeout(searchTimer);
  state.searchResults = null; state.searchError = ""; state.searchLoading = Boolean(state.search);
  const query = state.search, scope = signInScope();
  if (!query) return;
  searchTimer = setTimeout(async () => {
    try {
      const data = await api("/search?q=" + encodeURIComponent(query));
      if (scope.alive() && state.search === query) state.searchResults = data.sessions;
    } catch (error) { if (scope.alive() && state.search === query) state.searchError = error.message; }
    finally { if (scope.alive() && state.search === query) { state.searchLoading = false; renderConversations(); } }
  }, 250);
}

async function loadPrimary() {
  state.modelsLoading = true; state.modelsError = ""; renderSettings();
  const scope = signInScope();
  try {
    const data = await api("/settings/servers");
    state.primary = data.servers.find(server => server.id === "primary") || {base_url: "", models: [], default_model: null, has_key: false};
  } catch (error) { if (scope.alive()) state.modelsError = error.message; }
  finally { if (scope.alive()) { state.modelsLoading = false; renderSettings(); } }
}

function modelField(parent, label, input) {
  const wrapper = el("label", "model-field", label); wrapper.append(input); parent.append(wrapper); return input;
}

function modelInput(value, type = "text") {
  const input = el("input"); input.type = type; input.value = value; return input;
}

function renderModelList(parent, server) {
  const redraw = focus => { parent.replaceChildren(); renderModelList(parent, server); $(focus).focus(); };
  const defaultModel = el("select"); defaultModel.id = "settings-default-model";
  const enabledModels = server.models.filter(model => model.enabled);
  if (!enabledModels.length) {
    const option = el("option", "", "No enabled models"); option.value = ""; defaultModel.append(option);
  }
  for (const model of enabledModels) {
    const option = el("option", "", model.display_name || model.id); option.value = model.id; defaultModel.append(option);
  }
  defaultModel.value = server.default_model || "";
  defaultModel.disabled = state.settingsBusy || !enabledModels.length;
  defaultModel.addEventListener("change", () => { server.default_model = defaultModel.value || null; });
  modelField(parent, "Default model", defaultModel);
  if (!server.models.length) parent.append(el("p", "", "No models yet. Discover models or add one by name."));
  for (const [index, model] of server.models.entries()) {
    const row = el("div", "model-row"), heading = el("div", "model-row-heading");
    const expanded = state.openModel === model.id, label = model.display_name || model.id;
    const expand = el("button", "model-expand"); expand.type = "button"; expand.id = `settings-model-${index}`;
    expand.setAttribute("aria-expanded", String(expanded)); expand.setAttribute("aria-controls", `settings-model-details-${index}`);
    expand.setAttribute("aria-label", `Model settings for ${label}`);
    const copy = el("span", "model-copy"), title = el("span", "model-name", label);
    const id = el("small", "model-id" + (label === model.id ? " hidden" : ""), model.id);
    copy.append(title, id);
    const chevron = el("span", "model-chevron", "›"); chevron.setAttribute("aria-hidden", "true");
    expand.append(copy, chevron);
    expand.addEventListener("click", () => {
      state.openModel = expanded ? null : model.id; redraw(expand.id);
    });
    const enabled = el("button", "job-toggle" + (model.enabled ? "" : " off"));
    enabled.type = "button"; enabled.id = `settings-model-enabled-${index}`; enabled.disabled = state.settingsBusy;
    enabled.setAttribute("role", "switch"); enabled.setAttribute("aria-checked", String(model.enabled));
    enabled.setAttribute("aria-label", `${label} enabled`);
    enabled.addEventListener("click", () => {
      model.enabled = !model.enabled;
      if (!server.models.some(item => item.enabled && item.id === server.default_model)) {
        server.default_model = server.models.find(item => item.enabled)?.id || null;
      }
      redraw(enabled.id);
    });
    const toggle = el("span", "model-enabled"); toggle.append(el("span", "", "Enabled"), enabled);
    heading.append(expand, toggle); row.append(heading);
    const fields = el("div", "model-details" + (expanded ? "" : " hidden")); fields.id = `settings-model-details-${index}`;
    if (expanded) {
      const name = modelField(fields, "Display name", modelInput(model.display_name)); name.maxLength = 80;
      name.addEventListener("input", () => {
        model.display_name = name.value.trim() ? name.value : model.id;
        title.textContent = model.display_name; id.classList.toggle("hidden", model.display_name === model.id);
        expand.setAttribute("aria-label", `Model settings for ${model.display_name}`);
        enabled.setAttribute("aria-label", `${model.display_name} enabled`);
        const option = [...defaultModel.options].find(option => option.value === model.id);
        if (option) option.textContent = model.display_name;
      });
      const thinking = el("select");
      for (const [value, text] of [["reasoning_effort", "reasoning_effort"], ["template_flag", "Template flag"], ["unsupported", "Unsupported"]]) {
        const option = el("option", "", text); option.value = value; thinking.append(option);
      }
      thinking.value = model.thinking_mode;
      thinking.addEventListener("change", () => { model.thinking_mode = thinking.value; });
      modelField(fields, "Thinking mode", thinking);
      const tools = modelField(fields, "Web tools", modelInput("", "checkbox"));
      tools.checked = model.tools !== false; tools.addEventListener("change", () => { model.tools = tools.checked; });
      const context = modelField(fields, "Context size (optional)", modelInput(model.context_tokens ?? "", "number"));
      context.min = 1; context.max = 100000000; context.step = 1;
      context.addEventListener("input", () => { if (context.value) model.context_tokens = Number(context.value); else delete model.context_tokens; });
    }
    row.append(fields); parent.append(row);
  }
}

function renderWebSearch(detail) {
  const section = settingsSection(detail, "Web search", "Connect SearXNG to let models search. Leave the URL empty to turn web tools off.");
  const url = modelField(section, "SearXNG base URL", modelInput(state.settings.search_url || "", "url"));
  url.id = "settings-search-url"; url.placeholder = "https://search.example.com"; url.autocomplete = "off";
  const query = modelField(section, "Test query", modelInput("Roost web search")); query.id = "settings-search-query"; query.maxLength = 2000;
  const actions = el("div", "settings-actions");
  actions.append(settingsButton("primary", "Save", async () => {
    if (await saveSettings({search_url: url.value.trim()}, "Web search saved.")) await loadModels();
  }), settingsButton("secondary", "Test", async () => {
    const base = url.value.trim(), text = query.value;
    if (!await saveSettings({search_url: base}, "Web search saved.")) return;
    await loadModels();
    try {
      const result = await api("/settings/web-search/test", {method: "POST", body: JSON.stringify({query: text})});
      notice(`Search connected: ${result.results.length} results.`);
    } catch (error) { notice(error.message); }
  }));
  section.append(actions);
}

function renderModels(detail) {
  const backend = settingsSection(detail, "Backend", "Choose which conversations Roost shows.");
  const select = el("select");
  for (const value of ["hermes", "native"]) { const option = el("option", "", value === "native" ? "Native" : "Hermes"); option.value = value; select.append(option); }
  select.value = state.backend; select.disabled = state.settingsBusy;
  modelField(backend, "Backend", select);
  select.addEventListener("change", async () => {
    if (await saveSettings({backend: select.value}, "Backend saved.")) location.reload();
  });
  if (!isNative()) return;
  const section = settingsSection(detail, "Primary server", "An OpenAI-compatible model server. Keys stay on the server.");
  if (state.modelsError) { const error = el("p", "error", state.modelsError); error.setAttribute("role", "alert"); section.append(error); }
  if (state.modelsLoading) { section.append(el("p", "", "Loading models…")); return; }
  const server = state.primary;
  if (!server) { section.append(settingsButton("secondary", "Load server", loadPrimary)); return; }
  const url = modelField(section, "Base URL", modelInput(server.base_url, "url"));
  url.placeholder = "https://models.example.com/v1"; url.spellcheck = false; url.autocapitalize = "off"; url.autocomplete = "off";
  url.addEventListener("input", () => { server.base_url = url.value; });
  const key = modelField(section, "API key", modelInput("", "password"));
  key.maxLength = 4096;
  key.autocomplete = "new-password"; key.placeholder = server.has_key ? "Key saved" : "Optional";
  section.append(el("p", "", server.has_key ? "Key saved. Enter a new key to replace it." : "No key saved."));
  const actions = el("div", "settings-actions");
  const saveServer = async clear => {
    const changes = {base_url: url.value, ...(clear ? {clear_key: true} : key.value ? {key: key.value} : {})};
    key.value = "";
    return withSettingsBusy(async () => {
      const saved = await api("/settings/servers/primary", {method: "PUT", body: JSON.stringify(changes)});
      state.primary = {...server, base_url: saved.base_url, has_key: saved.has_key}; notice(clear ? "Key cleared." : "Server saved.");
    });
  };
  actions.append(settingsButton("primary", "Save server", () => saveServer(false)));
  if (server.has_key) actions.append(settingsButton("secondary", "Clear key", () => saveServer(true)));
  actions.append(settingsButton("secondary", "Discover", async () => {
    if (!await saveServer(false)) return;
    await withSettingsBusy(async () => {
      const data = await api("/settings/servers/primary/discover", {method: "POST"});
      const drafts = new Map(server.models.map(model => [model.id, model]));
      state.primary.models = data.models.map(model => drafts.get(model.id) || model); notice("Models discovered. Choose which to enable, then save models.");
    });
  }));
  section.append(actions);
  const curated = settingsSection(detail, "Models", "Enable models for the chat picker and choose a default. Save models when you’re done.");
  const list = el("div", "model-list"); renderModelList(list, server); curated.append(list);
  const add = modelField(curated, "Add model by name", modelInput("")); add.autocapitalize = "off"; add.spellcheck = false; add.placeholder = "model-id";
  const buttons = el("div", "settings-actions");
  buttons.append(settingsButton("secondary", "Add model", () => {
    const id = add.value.trim(); if (!id) return;
    if (server.models.some(model => model.id === id)) { notice("That model is already listed."); return; }
    server.models.push({id, display_name: id, enabled: false, thinking_mode: "unsupported", tools: true});
    list.replaceChildren(); renderModelList(list, server); add.value = "";
  }), settingsButton("primary", "Save models", () => withSettingsBusy(async () => {
    await api("/settings/servers/primary/models", {method: "PUT", body: JSON.stringify({models: server.models, default_model: server.default_model})});
    await loadModels(); notice("Models saved.");
  })));
  curated.append(buttons);
}

function settingsGroups() {
  return [["appearance", "Appearance", "Theme and accent colour"], ["time", "Time zone", "How times are shown"],
          ["models", "Models", "Backend and model server"], ...(isNative() ? [["web", "Web search", "SearXNG connection"], ["personas", "Persona", "Your assistant presets"], ["memory", "Memory", "Facts kept across chats"]] : [["agent", agent(), "Name and instructions"]]), ["data", "Data", isNative() ? "Delete conversations" : "Delete conversations or jobs"],
          ["service", "Service", isNative() ? "Restart Roost" : `Restart Roost or ${agent()}`], ["notifications", "Notifications", isNative() ? "Replies on your devices" : "Replies and job reports on your devices"],
          ["account", "Account", "Passkeys and sign out"],
          ["about", "About", "Version, live status and credits"]];
}

const SETTINGS_ICONS = { personas: "persona", memory: "memory", web: "globe", models: "server", appearance: "palette", time: "globe", data: "data", service: "server", notifications: "bell", account: "key", about: "info" };

async function loadSettings() {
  const scope = signInScope();
  if (!state.settingsGroup && !phoneWidth.matches) state.settingsGroup = "appearance";
  renderSettings();
  try {
    const data = await api("/settings");
    state.settings = data;
    applyServerSettings(data);
    renderSettings();
    if (isNative() && state.settingsGroup === "personas") { await loadPersonas(); renderSettings(); }
    if (isNative() && state.settingsGroup === "memory") { await loadMemory(); renderSettings(); }
  } catch (error) { report(error, scope); }
  if (state.settingsGroup === "agent" && !isNative()) loadSoul();
}

function selectSettingsGroup(id) {
  state.settingsGroup = id;
  $("app").classList.add("settings-open");
  placePresence();
  renderSettings();
  $("settings-detail").scrollTop = 0;
  if (id === "models") loadPrimary();
  if (id === "personas") loadPersonas().then(renderSettings).catch(error => notice(error.message));
  if (id === "memory") loadMemory().then(renderSettings).catch(error => notice(error.message));
  if (id === "agent" && !isNative()) loadSoul();
  if (id === "about") loadAbout();
  if (id === "account") loadPasskeys();
  if (id === "notifications") { loadPush(); if (!isNative()) loadHermesLink(); }
}

function renderSettings() {
  const list = $("sidebar-settings-list"); list.replaceChildren();
  for (const [id, label, hint] of settingsGroups()) {
    const row = el("button", "job-list-row settings-row" + (state.settingsGroup === id ? " selected" : ""));
    row.type = "button";
    if (state.settingsGroup === id) row.setAttribute("aria-current", "true");
    const badge = el("span", "settings-icon");
    if (id === "agent") badge.textContent = agent().charAt(0).toUpperCase(); else badge.append(icon(SETTINGS_ICONS[id]));
    const copy = el("span", "settings-row-copy");
    copy.append(el("span", "job-list-name", label), el("span", "settings-row-hint", hint));
    row.append(badge, copy);
    row.addEventListener("click", () => selectSettingsGroup(id));
    list.append(row);
  }
  const group = settingsGroups().find(([id]) => id === state.settingsGroup);
  $("settings-title").textContent = group ? group[1] : "Settings";
  $("settings-subtitle").textContent = group ? group[2] : "";
  const detail = $("settings-detail"); detail.replaceChildren();
  if (!group) return;
  if (!state.settings) { detail.append(el("p", "job-history-empty", "Loading settings…")); return; }
  ({ personas: renderPersonas, memory: renderMemory, web: renderWebSearch, models: renderModels, appearance: renderAppearance, time: renderTimeSettings, agent: renderAgentSettings, data: renderDataSettings,
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

async function loadPersonas() {
  const scope = signInScope(), data = await api("/personas");
  if (scope.alive()) state.personaData = data;
}

async function loadMemory() {
  const scope = signInScope(), data = await api("/memory");
  if (scope.alive()) state.memoryData = data;
}

function fillPersonaPicker(picker, selected) {
  picker.replaceChildren();
  const data = state.personaData;
  const name = data.personas.find(item => item.id === data.default_persona_id)?.name || "Assistant";
  const defaultOption = el("option", "", `Default (${name})`); defaultOption.value = ""; picker.append(defaultOption);
  for (const persona of data.personas) { const option = el("option", "", persona.name); option.value = persona.id; picker.append(option); }
  picker.value = selected || "";
}

function renderPersonas(detail) {
  if (!state.personaData) { detail.append(el("p", "", "Loading personas…")); return; }
  const data = state.personaData;
  const list = settingsSection(detail, "Personas", "Choose a voice for each chat. Changes apply when the next reply starts.");
  for (const persona of data.personas) {
    const row = el("div", "preset-row"), actions = el("div", "settings-actions");
    row.append(el("strong", "", persona.name + (persona.id === data.default_persona_id ? " · Default" : "")));
    actions.append(settingsButton("secondary", "Edit", () => { state.editPersona = {...persona}; renderSettings(); }));
    if (persona.id !== data.default_persona_id) {
      actions.append(settingsButton("secondary", "Set as default", () => withSettingsBusy(async scope => {
        await api("/personas/default", {method: "PUT", body: JSON.stringify({default_persona_id: persona.id})});
        if (scope.alive()) { await loadPersonas(); if (state.current) await loadMessages(); else { applyAgentName(); renderWelcome(); } }
      })));
      actions.append(settingsButton("danger-button", "Delete", () => withSettingsBusy(async scope => {
        await api(`/personas/${persona.id}`, {method: "DELETE"});
        if (scope.alive()) { if (state.editPersona?.id === persona.id) state.editPersona = null; await loadPersonas(); if (state.current) await loadMessages(); }
      })));
    }
    row.append(actions); list.append(row);
  }
  if (!state.editPersona) { list.append(settingsButton("primary persona-add", "Add persona", () => { state.editPersona = {name: "", prompt: ""}; renderSettings(); })); return; }
  const draft = state.editPersona, form = el("form", "preset-editor");
  const nameLabel = el("label", "", "Name"); nameLabel.htmlFor = "persona-name";
  const name = el("input"); name.id = "persona-name"; name.maxLength = 60; name.required = true; name.value = draft.name;
  name.addEventListener("input", () => { draft.name = name.value; });
  const promptLabel = el("label", "", "Persona prompt"); promptLabel.htmlFor = "persona-prompt";
  const prompt = el("textarea"); prompt.id = "persona-prompt"; prompt.maxLength = 8000; prompt.rows = 8; prompt.value = draft.prompt;
  prompt.addEventListener("input", () => { draft.prompt = prompt.value; });
  const actions = el("div", "settings-actions"), save = el("button", "primary", "Save persona"); save.type = "submit"; save.disabled = state.settingsBusy;
  actions.append(save, settingsButton("secondary", "Cancel", () => { state.editPersona = null; renderSettings(); }));
  form.append(nameLabel, name, promptLabel, prompt, actions);
  form.addEventListener("submit", event => {
    event.preventDefault();
    const payload = {name: name.value, prompt: prompt.value};
    withSettingsBusy(async scope => {
      await api(draft.id ? `/personas/${draft.id}` : "/personas", {method: draft.id ? "PUT" : "POST", body: JSON.stringify(payload)});
      if (scope.alive()) { state.editPersona = null; await loadPersonas(); if (state.current) await loadMessages(); else { applyAgentName(); renderWelcome(); } }
    });
  });
  list.append(form);
}

function renderMemory(detail) {
  const data = state.memoryData;
  if (!data) { detail.append(el("p", "", "Loading memory…")); return; }
  const section = settingsSection(detail, "Memory", "Every fact is included in every reply. Keep the things you want remembered across chats.");
  const over = data.used_tokens > data.budget_tokens;
  section.append(el("p", "memory-meter" + (over ? " over-budget" : ""), `~${data.used_tokens.toLocaleString()} / ${data.budget_tokens.toLocaleString()} tokens`));
  const meter = el("progress", "memory-progress" + (over ? " over-budget" : "")); meter.max = data.budget_tokens; meter.value = data.used_tokens; meter.setAttribute("aria-label", "Memory tokens"); section.append(meter);
  if (over) section.append(el("p", "memory-warning", "Over budget. You can still save facts; model saves are paused until memory fits."));
  for (const fact of data.facts) {
    const row = el("div", "preset-row memory-row"), head = el("div", "memory-row-head");
    const actions = el("div", "memory-actions");
    actions.append(settingsButton("secondary", "Edit", () => { state.editMemory = {...fact}; renderSettings(); }),
      settingsButton("danger-button", "Delete", () => withSettingsBusy(async scope => {
        const result = await api(`/memory/${fact.id}`, {method: "DELETE"});
        if (scope.alive()) { state.memoryData = result; if (state.editMemory?.id === fact.id) state.editMemory = null; }
      })));
    head.append(el("span", "memory-origin", fact.origin === "model" ? "Model" : "You"), actions);
    const text = el("p", "memory-text clamped", fact.text); row.append(head, text);
    if (fact.text.length > 160 || fact.text.split("\n").length > 3) {
      const more = el("button", "memory-more", "Show all"); more.type = "button"; more.setAttribute("aria-expanded", "false");
      more.addEventListener("click", () => {
        const open = text.classList.toggle("clamped") === false;
        more.textContent = open ? "Show less" : "Show all"; more.setAttribute("aria-expanded", String(open));
      });
      row.append(more);
    }
    section.append(row);
  }
  const draft = state.editMemory || {text: ""}, form = el("form", "preset-editor");
  const label = el("label", "", draft.id ? "Edit fact" : "Add a fact"); label.htmlFor = "memory-text";
  const text = el("textarea"); text.id = "memory-text"; text.maxLength = 500; text.rows = 3; text.required = true; text.value = draft.text;
  text.addEventListener("input", () => { state.editMemory = {...draft, text: text.value}; });
  const actions = el("div", "settings-actions"), save = el("button", "primary", draft.id ? "Save fact" : "Add fact"); save.type = "submit"; save.disabled = state.settingsBusy;
  actions.append(save);
  if (draft.id) actions.append(settingsButton("secondary", "Cancel", () => { state.editMemory = null; renderSettings(); }));
  form.append(label, text, actions); section.append(form);
  form.addEventListener("submit", event => {
    event.preventDefault(); const payload = {text: text.value};
    withSettingsBusy(async scope => {
      const result = await api(draft.id ? `/memory/${draft.id}` : "/memory", {method: draft.id ? "PUT" : "POST", body: JSON.stringify(payload)});
      if (scope.alive()) { state.memoryData = result; state.editMemory = null; }
    });
  });
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
  const thinking = settingsSection(detail, "Thinking", "Applies to this device. New replies start with Thoughts collapsed; you can still open them while the reply is being written.");
  const collapsed = window.roostTheme.collapseThinking();
  const toggle = el("button", "job-toggle" + (collapsed ? "" : " off"));
  toggle.type = "button"; toggle.setAttribute("role", "switch");
  toggle.setAttribute("aria-checked", String(collapsed));
  toggle.setAttribute("aria-label", "Keep thinking collapsed");
  toggle.addEventListener("click", () => {
    window.roostTheme.setCollapseThinking(!window.roostTheme.collapseThinking());
    const chosen = window.roostTheme.collapseThinking();
    toggle.className = "job-toggle" + (chosen ? "" : " off");
    toggle.setAttribute("aria-checked", String(chosen));
  });
  const label = el("label", "job-enabled");
  label.append(el("span", "", "Keep thinking collapsed"), toggle);
  thinking.append(label);
  const accent = settingsSection(detail, "Accent colour", "Used for your messages, buttons and highlights on every device you sign in on.");
  const swatches = el("div", "swatches");
  swatches.setAttribute("role", "radiogroup"); swatches.setAttribute("aria-label", "Accent colour");
  for (const name of state.settings.accents) {
    const label = name.charAt(0).toUpperCase() + name.slice(1);
    const swatch = el("button", "swatch swatch-" + name);
    swatch.type = "button"; swatch.title = label; swatch.disabled = state.settingsBusy;
    swatch.append(el("i"), el("span", "", label));
    swatch.setAttribute("role", "radio");
    swatch.setAttribute("aria-checked", String(state.settings.accent === name));
    swatch.addEventListener("click", () => saveSettings({ accent: name }, label + " accent saved."));
    swatches.append(swatch);
  }
  // Custom: the system colour picker. It previews while you choose and saves when the picker closes.
  const chosen = state.settings.accents.includes(state.settings.accent) ? "" : state.settings.accent;
  const custom = el("label", "swatch swatch-custom" + (chosen ? " chosen" : ""));
  custom.title = "Custom";
  const dot = el("i", "", chosen ? "" : "+");
  if (chosen) dot.style.background = chosen;
  const picker = el("input");
  picker.type = "color"; picker.value = chosen || "#2f6f4e"; picker.disabled = state.settingsBusy;
  picker.setAttribute("aria-label", "Custom accent colour");
  picker.addEventListener("input", () => window.roostTheme.setAccent(picker.value, false));
  picker.addEventListener("change", () => saveSettings({ accent: picker.value }, "Custom accent saved."));
  custom.append(dot, picker, el("span", "", "Custom"));
  swatches.append(custom);
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
  const scope = signInScope();
  try {
    const data = await api("/about");
    state.about = { ...data, at: new Date() };
    if (state.section === "settings" && state.settingsGroup === "about") renderSettings();
  } catch (error) { if (scope.alive() && !state.about) notice(error.message); }
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
  if (!isNative()) {
    const hermes = about.hermes;
    const hermesSection = settingsSection(detail, agent() + " (Hermes)", null);
    hermesSection.append(statList(hermes.reachable ? [
      ["Status", hermes.gateway_running ? "Running" : (hermes.gateway_state || "Stopped")],
      ["Response time", hermes.latency_ms + " ms"],
      ["Hermes version", String(hermes.version || "Unknown"), hermes.release_date ? String(hermes.release_date) : ""],
      ["Active sessions", String(hermes.active_sessions ?? "Unknown")]] : [["Status", "Unreachable", "Roost can’t reach the Hermes dashboard"]]));
  }
  const credits = settingsSection(detail, "Credits", null);
  const list = el("ul", "credits");
  for (const [text, href] of [
    ["Roost by calebbrinkley, MIT License", "https://github.com/calebbrinkley/roost"],
    ["Hermes Agent by Nous Research", "https://github.com/NousResearch/hermes-agent"],
    ["Space Grotesk typeface by the Space Grotesk Project Authors, SIL Open Font License 1.1", "https://github.com/floriankarsten/space-grotesk"],
    ["aiohttp by the aio-libs contributors, Apache License 2.0", "https://github.com/aio-libs/aiohttp"]]) {
    if (isNative() && text.startsWith("Hermes Agent")) continue;
    const item = el("li"), link = el("a", "", text);
    link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer";
    item.append(link); list.append(item);
  }
  credits.append(list);
}

function renderAgentSettings(detail) {
  if (isNative()) return;
  const hermesName = state.settings.hermes_agent_name;
  const name = settingsSection(detail, "Name", hermesName ?
    `Leave empty to use the name Hermes gives it: ${hermesName}.` :
    (isNative() ? "Roost shows “Agent” when this is empty." : "Hermes has no name for it, so Roost shows “Agent” when this is empty."));
  const label = el("label", "", "Name shown in Roost"); label.htmlFor = "settings-agent-name";
  const input = el("input"); input.id = "settings-agent-name"; input.maxLength = 40; input.autocomplete = "off";
  input.value = state.nameDraft ?? state.settings.agent_name; input.placeholder = hermesName || "Agent";
  input.addEventListener("input", () => { state.nameDraft = input.value; });
  const actions = el("div", "settings-actions");
  actions.append(settingsButton("primary", "Save name", async () => {
    if (await saveSettings({ agent_name: input.value.trim() }, "Name saved.")) { state.nameDraft = undefined; renderSettings(); }
  }));
  name.append(label, input, actions);

  if (isNative()) return;
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
    if (isNative() && kind === "jobs") continue;
    const section = settingsSection(detail, heading, isNative() ? "Removes all native conversations for good." : text);
    // The operation's own name is the phrase to type, shown in the empty field; capitals and spacing don't matter.
    const phrase = heading.toLowerCase(), prompt = `Type “${phrase}” to confirm`;
    const input = el("input"); input.id = "confirm-" + kind; input.autocomplete = "off"; input.autocapitalize = "off";
    input.spellcheck = false; input.placeholder = prompt; input.setAttribute("aria-label", prompt);
    const typed = () => input.value.trim().replace(/\s+/g, " ").toLowerCase() === phrase;
    const actions = el("div", "settings-actions");
    const button = settingsButton("danger-button", heading, () => deleteEverything(kind, typed));
    button.disabled = true;
    input.addEventListener("input", () => { button.disabled = state.settingsBusy || !typed(); });
    actions.append(button);
    section.append(input, actions);
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
  const scope = signInScope();
  try {
    const data = await api("/passkeys");
    state.passkeyList = data.passkeys;
    state.passkeysAvailable = data.passkeys.length > 0;
    if (state.settingsGroup === "account") renderSettings();
    if (passkeysSupported()) state.passkeyRegister = await prefetch("/passkeys/register/options");
  } catch (error) { report(error, scope); }
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
  return withSettingsBusy(async (scope) => {
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
      if (scope.alive()) loadPasskeys();
      if (error.name === "InvalidStateError") throw new Error("This device already has a passkey for Roost.");
      if (error.name === "NotAllowedError") throw new Error("Adding the passkey was cancelled or timed out.");
      throw error;
    }
    await api("/passkeys", { method: "POST", body: JSON.stringify({
      name, id: toB64url(credential.rawId), clientDataJSON: toB64url(credential.response.clientDataJSON),
      attestationObject: toB64url(credential.response.attestationObject) }) });
    state.passkeyNameDraft = undefined;
    notice(`Passkey added. Next time, sign in with ${name}.`);
    await loadPasskeys();
  });
}

function removePasskey(passkey) {
  return withSettingsBusy(async () => {
    await api("/passkeys/" + encodeURIComponent(passkey.id), { method: "DELETE", body: JSON.stringify({ confirm: true }) });
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
  if (!isNative()) {
    const agentSection = settingsSection(detail, `Restart ${agent()}`,
      `Restarts the Hermes gateway: its messaging platforms and the job scheduler. A reply that is being written may stop.`);
    const agentActions = el("div", "settings-actions");
    agentActions.append(settingsButton("secondary", `Restart ${agent()}`, () =>
      serviceAction("/service/restart-agent", `${agent()} is restarting. Replies may take a minute.`)));
    agentSection.append(agentActions);
  }
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
  const scope = signInScope();
  try {
    const [data, mine] = await Promise.all([api("/push"), thisDevicePush().catch(() => null)]);
    if (!scope.alive()) return;
    state.push = { ...data, mine: mine?.id || null };
    if (state.settingsGroup === "notifications") renderSettings();
  } catch (error) { report(error, scope); }
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
  if (!isNative()) renderHermesLink(detail);
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
  const scope = signInScope();
  try {
    const data = await api("/hermes");
    state.hermesLink = data;
    if (state.settingsGroup === "notifications") renderSettings();
  } catch (error) { report(error, scope); }
}

// Job reports and messages the agent starts reach Roost through a plugin installed in Hermes.
function renderHermesLink(detail) {
  const link = state.hermesLink;
  const section = settingsSection(detail, "Job reports from " + agent(),
    `Install Roost’s plugin in Hermes for reply token counts and TPS, plus ${agent()}’s job reports and messages it sends on its own.`);
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
  return withSettingsBusy(async () => {
    const data = await api("/hermes/token", { method: "POST", body: JSON.stringify({ confirm: true }) });
    state.hermesLink = data; state.rotatingToken = false;
    notice("New token made. Update ROOST_DELIVERY_TOKEN in Hermes; the old one no longer works.");
  });
}

function enablePush() {
  return withSettingsBusy(async () => {
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
    notice("Notifications are on for this device.");
    await loadPush();
  });
}

function disablePush() {
  return withSettingsBusy(async (scope) => {
    const mine = await thisDevicePush();
    if (mine && state.push.devices.some((device) => device.id === mine.id)) {
      await api("/push/devices/" + encodeURIComponent(mine.id), { method: "DELETE", body: JSON.stringify({ confirm: true }) });
    }
    await mine?.subscription.unsubscribe();
    if (!scope.alive()) return;
    notice("Notifications are off for this device.");
    await loadPush();
  });
}

function removePushDevice(device) {
  return withSettingsBusy(async () => {
    await api("/push/devices/" + encodeURIComponent(device.id), { method: "DELETE", body: JSON.stringify({ confirm: true }) });
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
  const scope = signInScope();
  state.settingsBusy = true; renderSettings();
  try { await work(scope); return scope.alive(); }
  catch (error) { report(error, scope); return false; }
  finally { if (scope.alive()) { state.settingsBusy = false; renderSettings(); } }
}

function saveSettings(changes, message) {
  return withSettingsBusy(async () => {
    const data = await api("/settings", { method: "PATCH", body: JSON.stringify(changes) });
    state.settings = data;
    applyServerSettings(data);
    notice(message);
  });
}

async function loadSoul() {
  if (state.soul !== undefined || state.soulLoading) return;
  const scope = signInScope();
  state.soulLoading = true;
  try {
    const data = await api("/settings/soul");
    state.soul = data.content;
    if (state.settingsGroup === "agent") renderSettings();
  } catch (error) { report(error, scope); }
  finally { state.soulLoading = false; }
}

function saveSoul() {
  const content = $("settings-soul").value;
  return withSettingsBusy(async () => {
    await api("/settings/soul", { method: "PUT", body: JSON.stringify({ content }) });
    state.soul = content; state.soulDraft = undefined;
    notice("Instructions saved.");
  });
}

function deleteEverything(kind, typed) {
  if (!typed()) return;
  return withSettingsBusy(async () => {
    const result = await api("/" + kind, { method: "DELETE", body: JSON.stringify({ confirm: "DELETE" }) });
    if (kind === "conversations") {
      state.sentMessages.forEach(stopSendCheck);
      Object.assign(state, { conversations: [], current: null, turn: null, working: false, sentMessages: [], conversationOffset: 0, conversationsMore: false });
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
  return withSettingsBusy(async (scope) => {
    await api(path, { method: "POST" });
    if (scope.alive()) notice(message);
  });
}

function restartRoost() {
  return withSettingsBusy(async () => {
    await api("/service/restart-roost", { method: "POST" });
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
$("chat-settings").append(icon("more"));
$("nav-settings").addEventListener("click", () => setSection("settings"));
$("mobile-nav-settings").addEventListener("click", () => setSection("settings"));
$("back-to-settings").addEventListener("click", () => { $("app").classList.remove("settings-open"); placePresence(); });
$("top-action").addEventListener("click", () => state.section === "jobs" ? openJob() : newConversation());
$("sidebar-new").addEventListener("click", () => state.section === "jobs" ? openJob() : newConversation());
$("back-to-list").addEventListener("click", () => { stopRecording(true); $("app").classList.remove("chat-open"); placePresence(); reportPresence(); });
$("back-to-jobs").addEventListener("click", () => { $("app").classList.remove("job-open"); placePresence(); });

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
  if (state.section === "jobs") { state.jobSearch = query; renderJobs(); } else { state.search = isNative() ? query.slice(0, 200) : query; if (isNative()) queueSearch(); renderConversations(); }
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
  const scope = signInScope(), began = Date.now();
  pullIndicator.classList.remove("armed"); pullIndicator.classList.add("refreshing");
  settlePull(PULL_HOLD);
  try { await (state.section === "jobs" ? loadJobs() : loadConversations()); }
  catch (error) { report(error, scope); }
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
$("jump-latest").append(icon("down"));
$("jump-latest").addEventListener("click", () => {
  $("transcript").scrollTop = $("transcript").scrollHeight; updateLatest();
});
$("transcript").addEventListener("scroll", () => {
  updateLatest();
  if ($("transcript").scrollTop <= 0) loadEarlierMessages();
}, { passive: true });
$("reasoning").addEventListener("change", changeReasoning);
$("web-toggle").append(icon("globe"));
$("web-toggle").addEventListener("click", () => { state.webForce = !state.webForce; updateWebToggle(); });
$("sheet-model").addEventListener("change", updateSheetThinking);
$("message").addEventListener("input", updateNativeComposer);
$("send").addEventListener("click", event => {
  if (isNative() && (runningTurn() || queuedTurn())) { event.preventDefault(); stopNativeReply(); }
});
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
$("remember-form").addEventListener("submit", saveRemember);
$("remember-cancel").addEventListener("click", () => $("remember-sheet").close());
$("summary-close").addEventListener("click", () => $("summary-sheet").close());
$("sheet-instructions-form").addEventListener("submit", saveInstructions);
$("sheet-export").addEventListener("click", () => setTimeout(() => $("conversation-sheet").close(), 0));
$("chat-settings").addEventListener("click", () => {
  if (!state.current) return;
  openConversationSheet(state.conversations.find((row) => row.id === state.current) || { id: state.current, title: $("chat-title").textContent });
});
$("sheet-confirm-delete").addEventListener("click", deleteConversation);
for (const id of ["sheet-cancel", "sheet-instructions-cancel", "sheet-rename-cancel", "sheet-confirm-cancel"]) $(id).addEventListener("click", () => $("conversation-sheet").close());
// A tap on the dimmed backdrop (outside the sheet) closes it.
$("conversation-sheet").addEventListener("click", (event) => {
  const box = $("conversation-sheet").getBoundingClientRect();
  if (event.target === $("conversation-sheet") && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom)) $("conversation-sheet").close();
});
$("message").addEventListener("paste", pasteText);
$("message").addEventListener("input", () => {
  saveComposer();
  const hasText = Boolean($("message").value.trim());
  $("composer").classList.toggle("has-text", hasText);
  if (!hasText) state.voiceDraft = false;
});
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
  if (document.hidden || $("app").classList.contains("hidden") || state.section !== "messages" || state.busy) return;
  const delay = messagePollDelay();
  if (state.lastMessagePoll !== null && Date.now() - state.lastMessagePoll < delay) return;
  if (state.current) refreshConnection(() => loadMessages());
}, 3000);
setInterval(() => {
  if (document.hidden || $("app").classList.contains("hidden")) return;
  if (state.section === "messages") refreshConnection(() => loadConversations());
  else if (state.section === "jobs") refreshConnection(() => loadJobs());
}, 20000);

// Dictation stays in the composer for review. Sending that draft opts into a spoken reply.
const voice = { take: null };

function recordingUI(text = "") {
  const take = voice.take, recording = take?.phase === "recording";
  $("composer").classList.toggle("recording", recording);
  $("composer").classList.toggle("voice-active", Boolean(take));
  $("voice-button").disabled = Boolean(take && take.phase === "transcribing");
  const label = take ? (recording ? "Stop recording" : "Cancel recording") : "Start recording";
  $("voice-button").setAttribute("aria-label", label); $("voice-button").title = label;
  $("voice-button").setAttribute("aria-pressed", String(recording));
  $("send").disabled = Boolean(take || state.busy);
  $("voice-status-text").textContent = text;
  show("voice-status", Boolean(take));
}

function currentTake(take) {
  return voice.take === take && take.scope.alive();
}

function releaseRecording(take) {
  take.stream?.getTracks().forEach((track) => track.stop());
  clearInterval(take.timer);
}

async function startRecording() {
  if (voice.take || state.busy) return;
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder !== "function") { notice("This browser can’t record audio."); return; }
  const take = { scope: viewScope(), phase: "requesting", chunks: [] };
  voice.take = take; recordingUI("Waiting for microphone permission…");
  try {
    take.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    if (!currentTake(take)) { releaseRecording(take); return; }
    const type = ["audio/mp4", "audio/webm;codecs=opus", "audio/webm"].find((name) => MediaRecorder.isTypeSupported?.(name));
    const recorder = take.recorder = new MediaRecorder(take.stream, type ? { mimeType: type } : undefined);
    recorder.addEventListener("dataavailable", (chunk) => { if (chunk.data.size) take.chunks.push(chunk.data); });
    recorder.addEventListener("stop", () => finishRecording(take));
    recorder.addEventListener("error", () => {
      if (currentTake(take)) { stopRecording(true); notice("Recording stopped unexpectedly. Please try again."); }
    });
    recorder.start(); take.phase = "recording"; take.started = Date.now();
    const tick = () => {
      const seconds = Math.floor((Date.now() - take.started) / 1000);
      recordingUI(`Recording ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")} · tap the mic to stop`);
    };
    tick(); take.timer = setInterval(tick, 500);
  } catch {
    releaseRecording(take);
    if (currentTake(take)) {
      voice.take = null; recordingUI();
      notice("Roost couldn’t start recording. Check microphone permission and try again.");
    }
  }
}

function stopRecording(cancel) {
  const take = voice.take;
  if (!take) return;
  if (cancel || take.phase === "requesting") {
    voice.take = null; recordingUI();
  } else {
    take.phase = "transcribing"; recordingUI("Transcribing…");
  }
  releaseRecording(take);
  if (take.recorder?.state === "recording") take.recorder.stop();
}

function finishRecording(take) {
  releaseRecording(take);
  if (!currentTake(take)) return;
  take.phase = "transcribing"; recordingUI("Transcribing…");
  if (!take.chunks.length) {
    voice.take = null; recordingUI(); notice("No audio was recorded. Please try again."); return;
  }
  sendRecording(new Blob(take.chunks, { type: take.recorder.mimeType || take.chunks[0].type || "audio/mp4" }), take);
}

async function sendRecording(recording, take) {
  try {
    const dataUrl = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("Roost couldn’t read the recording."));
      reader.readAsDataURL(recording);
    });
    if (!currentTake(take)) return;
    const data = await api("/audio/transcribe", { method: "POST", body: JSON.stringify({ data_url: dataUrl, mime_type: recording.type }) });
    if (!currentTake(take)) return;
    const text = String(data.transcript || "").trim();
    if (!text) { notice("Didn’t catch that. Try again a little closer to the mic."); return; }
    const draft = [$("message").value.trimEnd(), text].filter(Boolean).join("\n");
    if (draft.length > $("message").maxLength) { notice("The transcript won’t fit in this message. Shorten the draft and try a shorter recording."); return; }
    $("message").value = draft; state.voiceDraft = true; saveComposer();
    $("composer").classList.add("has-text");
  } catch (error) { if (currentTake(take)) notice(error.message); }
  finally { if (voice.take === take) { voice.take = null; recordingUI(); } }
}

function audioPlayer() {
  if (!state.player) {
    const player = state.player = new Audio();
    player.addEventListener("ended", () => { state.playing = null; refreshPlayButtons(); });
    player.addEventListener("error", () => {
      if (!state.playing || !player.src.includes("/speech/")) return;
      state.playing = null; refreshPlayButtons();
      notice("Couldn’t read that aloud. Check Hermes’s text-to-speech setup, or try again.");
    });
  }
  return state.player;
}

// Played from a tap, the player may later start a reply on its own (Safari otherwise refuses).
function unlockPlayer() {
  if (typeof Audio !== "function" || state.playing) return;
  const player = audioPlayer();
  player.src = "/api/audio/silence";
  player.play().catch(() => {});
}

function playReply(conversation, id) {
  if (typeof Audio !== "function") return;
  const player = audioPlayer();
  state.playing = { conversation, id };
  player.src = `/api/conversations/${encodeURIComponent(conversation)}/speech/${encodeURIComponent(id)}`;
  player.play().catch((error) => {
    // Refused without a tap (an automatic reading): show the play button again, quietly.
    if (error?.name === "NotAllowedError" && state.playing?.id === id) { state.playing = null; refreshPlayButtons(); }
  });
  refreshPlayButtons();
}

function stopPlaying() {
  state.player?.pause();
  state.playing = null;
  refreshPlayButtons();
}

function refreshPlayButtons() { if (state.lastRows) renderMessages(state.lastRows); }

function playButton(id) {
  const playing = state.playing?.conversation === state.current && state.playing.id === id;
  const button = el("button", "play-reply" + (playing ? " playing" : ""));
  button.type = "button";
  button.setAttribute("aria-label", playing ? "Stop reading aloud" : "Read aloud");
  button.title = playing ? "Stop" : "Read aloud";
  button.append(icon(playing ? "stop" : "speaker"));
  button.addEventListener("click", (event) => { event.stopPropagation(); if (playing) stopPlaying(); else playReply(state.current, id); });
  return button;
}

// Once the reply to a voice message is in, read it aloud (once; ten minutes at most). The voice message
// is found by position: the row Hermes saved it as, and its reply is the last answer before the owner's
// next message, once the server says the turn is over.
function readVoiceReply(view) {
  const waiting = state.voiceReply;
  if (!waiting || waiting.id !== state.current || view.live) return;
  if (Date.now() - waiting.since > 600000) { state.voiceReply = null; return; }
  waiting.row ??= view.savedAs[waiting.key] ?? null;
  const own = waiting.row == null ? -1 : view.rows.findIndex((row) => row.id === waiting.row);
  if (own < 0 || view.working) return;
  const next = view.rows.findIndex((row, index) => index > own && row.role === "user");
  const reply = view.rows.slice(own + 1, next >= 0 ? next : undefined)
    .findLast((row) => row.role === "assistant" && row.id != null && messageText(row));
  if (!reply) return;
  state.voiceReply = null;
  // After this render: starting the player renders the transcript again.
  const conversation = state.current;
  setTimeout(() => playReply(conversation, reply.id), 0);
}

$("voice-button").addEventListener("click", () => voice.take ? stopRecording(false) : startRecording());
$("voice-cancel").addEventListener("click", () => stopRecording(true));
window.addEventListener("pagehide", () => stopRecording(true));

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
  state.backend = data.backend || "hermes";
  applyAgentName(data.agent);
  state.passkeysAvailable = Boolean(data.passkeys);
  return data.signed_in ? signedIn() : signedOut(false);
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopRecording(true);
  reportPresence();
  if (document.visibilityState === "visible") { boot.delay = 2000; if (boot.done) refreshOnResume(); else start(); }
});
setInterval(reportPresence, 15000);
// A notification tapped while Roost is already open.
navigator.serviceWorker?.addEventListener("message", (event) => {
  if (typeof event.data?.link === "string") notificationLink(event.data.link);
  else if (typeof event.data?.open === "string") notificationLink("?c=" + encodeURIComponent(event.data.open));
});
$("delivery-close").addEventListener("click", () => $("delivery-sheet").close());
window.addEventListener("online", () => { boot.delay = 2000; if (boot.done) refreshOnResume(); else start(); });
start();
