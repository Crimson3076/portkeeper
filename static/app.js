"use strict";

const $ = (id) => document.getElementById(id);
const targetLabels = { linux: "Linux / Steam Deck", windows: "Windows", handheld: "Handheld", capture: "Source" };
const targetGlyphs = { linux: "◈", windows: "▦", handheld: "▤", capture: "◇" };
const captureRecipe = { version: 1, mode: "capture", payload: { kind: "source" }, notes: "Preserve tagged upstream source. Add a tested recipe to build a package." };
const activeStatuses = new Set(["queued", "running", "checking", "capturing", "building", "preserving", "downloading_rom", "extracting", "packaging_linux", "packaging_windows", "preserving_source", "preserving_assets", "capturing_dependencies", "building_extractor", "preparing", "validating"]);
const busy = new Set();
const preservationCache = new Map();
const preservationLoading = new Set();
const assistantPromptDrafts = new Map();
const retentionDrafts = new Map();
const seenNotifications = new Set();
let state = { projects: [], jobs: [], artifacts: [], notifications: [], profiles: [], settings: {}, runner: {}, summary: {}, romm: {}, assistant: {} };
let recipes = [];
let profiles = [];
let activityView = "jobs";
let librarySearch = "", libraryPlatform = "", libraryStage = "";
let settingsView = "romm";
let route = { page: "library", projectId: null, tab: "overview" };
let selectedJobId = null;
let recipeProjectId = null;
let metadataProjectId = null;
let validationArtifactId = null;
let retentionReview = null;
let recipeProposal = null;
let refreshing = false;
let lastRender = null;
let notificationsInitialized = false;
let formConfigFingerprint = null;
let wizard = null;
let wizardBusy = false;
let wizardSerial = 0;
let desktopNotifications = readPreference("portkeeper.desktop-notifications") === "true";

function readPreference(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
function savePreference(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* Browser storage is optional. */ } }
function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined && content !== null) (Array.isArray(content) ? content : [content]).forEach((child) => node.append(child instanceof Node ? child : document.createTextNode(String(child))));
  return node;
}
function button(label, className, action, disabled = false) {
  const node = el("button", className, label);
  node.type = "button";
  node.disabled = disabled;
  node.addEventListener("click", action);
  return node;
}
function input(id, type, value, placeholder) {
  const node = el("input");
  node.id = id;
  node.type = type;
  if (value !== undefined && value !== null) node.value = value;
  if (placeholder) node.placeholder = placeholder;
  return node;
}
function field(parent, labelText, node, help) {
  const label = el("label", "", labelText);
  label.htmlFor = node.id;
  parent.append(label, node);
  if (help) parent.append(el("p", "field-help", help));
  return node;
}
function toast(message, isError = false) {
  const node = el("div", `toast${isError ? " error" : ""}`, el("span", "", message));
  node.append(button("×", "icon-button", () => node.remove()));
  node.lastChild.setAttribute("aria-label", "Dismiss notification");
  $("toasts").append(node);
  while ($("toasts").children.length > 4) $("toasts").firstChild.remove();
  setTimeout(() => node.remove(), isError ? 12000 : 6500);
}
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}) });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = payload.error || payload.detail || payload.message || `Request failed (${response.status})`;
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return payload;
}
function timeAgo(value) {
  if (!value) return "Not checked yet";
  const delta = Math.max(0, Date.now() - Date.parse(value));
  if (!Number.isFinite(delta)) return String(value);
  if (delta < 60000) return "Just now";
  if (delta < 3600000) return `${Math.floor(delta / 60000)}m ago`;
  if (delta < 86400000) return `${Math.floor(delta / 3600000)}h ago`;
  return `${Math.floor(delta / 86400000)}d ago`;
}
function dateLabel(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
function bytes(value) {
  if (!Number.isFinite(Number(value))) return "—";
  let size = Number(value), index = 0;
  const units = ["B", "KB", "MB", "GB"];
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index++; }
  return `${size.toFixed(index ? 1 : 0)} ${units[index]}`;
}
function newestFirst(items) { return items.slice().sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0)); }
function statusBadge(status, override) {
  const value = String(status || "idle").toLowerCase();
  const labels = {
    idle: "Waiting", ready: "Packaged", watching: "Watching", captured: "Source preserved", archived: "Source preserved", succeeded: "Complete", success: "Complete", completed: "Complete", published: "In local library", built: "Packaged", unchanged: "Up to date",
    failed: "Needs attention", error: "Needs attention", needs_attention: "Needs attention", blocked: "Needs attention", needs_rom: "Needs a ROM", missing_inputs: "Needs game files", capture_only: "Source only", unsupported_version: "Recipe update needed",
    queued: "Queued", running: "Working", checking: "Checking upstream", capturing: "Preserving source", preserving: "Preserving source", downloading_rom: "Downloading ROM", extracting: "Extracting game assets", packaging_linux: "Packaging Linux", packaging_windows: "Packaging Windows", building: "Building package", preserving_source: "Preserving source", preserving_assets: "Preserving releases", capturing_dependencies: "Preserving dependencies", building_extractor: "Building extractor", preparing: "Preparing inputs", validating: "Validating package", complete: "Complete"
  };
  let tone = "";
  if (["ready", "watching", "captured", "archived", "succeeded", "success", "completed", "published", "built", "unchanged", "complete"].includes(value)) tone = "good";
  if (["failed", "error", "needs_attention", "blocked"].includes(value)) tone = "bad";
  if (["needs_rom", "missing_inputs", "capture_only", "unsupported_version"].includes(value)) tone = "warn";
  if (activeStatuses.has(value)) tone = "active";
  const node = el("span", `status-badge ${tone}`, override || labels[value] || value.replace(/_/g, " "));
  if (tone === "active") node.prepend(el("span", "spinner"));
  return node;
}
function targetPill(target) { return el("span", "target-pill", [el("span", "", targetGlyphs[target] || "◇"), targetLabels[target] || target]); }
function meta(label, value, wide = false) { return el("div", wide ? "wide" : "", [el("span", "meta-label", label), el("span", "meta-value", value ?? "—")]); }
function repoLink(repo) {
  const node = el("a", "repo-link", repo || "No repository");
  if (/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo || "")) { node.href = `https://github.com/${repo}`; node.target = "_blank"; node.rel = "noopener noreferrer"; }
  return node;
}
function projectSource(project) { return project.demo ? el("span", "repo-link", "Synthetic sample files") : repoLink(project.repo); }
function projectById(id) { return state.projects.find((project) => String(project.id) === String(id)); }
function projectArtifacts(id) { return newestFirst(state.artifacts.filter((artifact) => String(artifact.project_id) === String(id))); }
function projectJobs(id) { return newestFirst(state.jobs.filter((job) => String(job.project_id) === String(id))); }
function jobActive(job) { return activeStatuses.has(job.status) && !job.finished_at; }
function projectActiveJob(project) { const jobs = projectJobs(project.id).filter(jobActive); return jobs.find((job) => job.status !== "queued") || jobs[0]; }
function jobStage(job) { return job.status === "failed" ? "failed" : job.stage || job.status; }
function projectStatus(project) { const active = projectActiveJob(project); return active ? jobStage(active) : project.status; }
function validationLevel(artifact) { return artifact.validation_level || artifact.validation?.level || (typeof artifact.validation === "string" ? artifact.validation : "packaged"); }
function validationLabel(artifact) {
  const level = validationLevel(artifact);
  const label = level === "user_confirmed_playable" ? "User confirmed playable" : level === "launch_checked" ? "Launch checked" : "Packaged";
  const node = el("span", `validation-label ${level === "user_confirmed_playable" ? "confirmed" : level === "launch_checked" ? "launch" : ""}`, label);
  node.title = level === "user_confirmed_playable" ? "The user confirmed this exact package is playable." : level === "launch_checked" ? "A launch check was recorded. Gameplay is not confirmed." : "Package files were validated. Launch and gameplay are not confirmed.";
  return node;
}
function artifactDownload(artifact, className = "button primary small", label = "↓ Download package") {
  const node = el("a", className, label);
  node.href = `/api/artifacts/${encodeURIComponent(artifact.id)}/download`;
  node.setAttribute("download", "");
  return node;
}
function safeDownloadLink(url, label, className = "button secondary small") {
  if (!url) return null;
  try {
    const parsed = new URL(url, location.origin);
    if (parsed.origin !== location.origin || !parsed.pathname.startsWith("/api/")) return null;
    const link = el("a", className, label); link.href = parsed.href; link.setAttribute("download", ""); return link;
  } catch (_) { return null; }
}
function emptyState(glyph, heading, description, action) {
  const node = el("div", "empty-state", [el("span", "empty-glyph", glyph), el("h3", "", heading), el("p", "", description)]);
  if (action) node.append(action);
  return node;
}
function table(headers, rows) {
  const node = el("table");
  node.append(el("thead", "", el("tr", "", headers.map((text) => el("th", "", text)))), el("tbody", "", rows));
  return el("div", "table-wrap", node);
}
function rommConfig() { return state.romm || state.settings?.romm || {}; }
function rommReady() { const config = rommConfig(); return Boolean(config.url && (config.token_configured || config.has_token)); }
function notificationTitle(notification) {
  const names = { ready: "Package ready", build_failed: "Build needs attention", needs_input: "Game input required", upstream_unavailable: "Upstream unavailable" };
  const title = notification.title || names[notification.kind] || "Game update";
  return notification.project_name ? `${notification.project_name} · ${title}` : title;
}
function notificationUnread(notification) { return notification.unread !== undefined ? Boolean(notification.unread) : !(notification.read || notification.read_at); }
function knownGood(artifact, project) { return Boolean(artifact.known_good || project?.known_good_artifact_id === artifact.id || project?.known_good?.[artifact.target] === artifact.id || project?.known_good_artifacts?.[artifact.target] === artifact.id); }

function parseRoute() {
  const parts = location.hash.slice(1).split("/").map((part) => { try { return decodeURIComponent(part); } catch (_) { return part; } });
  if (parts[0] === "project" && parts[1]) return { page: "project", projectId: parts[1], tab: ["overview", "versions", "activity", "advanced"].includes(parts[2]) ? parts[2] : "overview" };
  return { page: ["activity", "settings"].includes(parts[0]) ? parts[0] : parts[0] === "storage" ? "settings" : "library", projectId: null, tab: "overview" };
}
function navigateProject(id, tab = "overview", jobId = null) { selectedJobId = jobId; location.hash = `project/${encodeURIComponent(id)}/${tab}`; if (route.projectId === id && route.tab === tab) renderProject(); }
function navigateSettings(tab = "romm") { settingsView = tab; location.hash = "settings"; renderSettingsTabs(); }
function renderRoute() {
  route = parseRoute();
  if (location.hash === "#storage") settingsView = "storage";
  ["library", "activity", "settings", "project"].forEach((page) => { $(`${page}-page`).hidden = route.page !== page; });
  document.querySelectorAll(".nav-link").forEach((link) => { const active = link.dataset.page === (route.page === "project" ? "library" : route.page); link.classList.toggle("active", active); if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current"); });
  $("breadcrumb").textContent = route.page === "project" ? "WORKER DETAILS" : { library: "YOUR LIBRARY", activity: "WORKSPACE ACTIVITY", settings: "WORKSPACE SETTINGS" }[route.page];
  renderSettingsTabs();
  if (route.page === "project") renderProject();
  if (route.page === "activity") renderActivity();
}
function renderLibrary() {
  $("summary-projects").textContent = state.summary.projects ?? state.projects.length;
  $("summary-artifacts").textContent = state.summary.artifacts ?? state.artifacts.length;
  const attention = state.summary.needs_attention ?? state.projects.filter((project) => project.last_error || ["needs_rom", "needs_attention", "failed"].includes(project.status)).length;
  $("summary-attention").textContent = attention;
  $("attention-summary").classList.toggle("attention-active", attention > 0);
  const grid = $("project-grid"); grid.replaceChildren();
  $("library-results").textContent = "0 games";
  if (!state.projects.length) {
    const empty = emptyState("▦", "A home for your next playthrough", "Connect your RomM library, choose a game, and preserve its updates.");
    empty.append(el("div", "onboarding-steps", [el("span", "", [el("b", "", "1"), "Connect RomM"]), el("span", "", [el("b", "", "2"), "Select a game"]), el("span", "", [el("b", "", "3"), "Test a build"])]));
    empty.append(button("Set up your first game →", "button primary", openWizard), button("Explore with a synthetic demo", "text-button", () => navigateSettings("demo")));
    grid.append(empty); return;
  }
  const matches = state.projects.filter((project) => {
    const hasPackages = projectArtifacts(project.id).length > 0;
    const needsAttention = Boolean(project.last_error) || ["needs_rom", "missing_inputs", "needs_attention", "failed", "blocked", "unsupported_version"].includes(project.status);
    return `${project.name} ${project.repo || ""}`.toLowerCase().includes(librarySearch.toLowerCase()) && (!libraryPlatform || (project.targets || []).includes(libraryPlatform)) && (!libraryStage || (libraryStage === "packages" ? hasPackages : libraryStage === "source" ? !hasPackages && project.recipe?.mode === "capture" : needsAttention));
  });
  $("library-results").textContent = `${matches.length} of ${state.projects.length} games`;
  if (!matches.length) grid.append(emptyState("⌕", "No games match", "Try another search or choose All platforms and All games."));
  matches.forEach((project) => {
    const artifacts = projectArtifacts(project.id);
    const newest = artifacts[0];
    const good = artifacts.find((artifact) => knownGood(artifact, project));
    const title = button(project.name, "project-name", () => navigateProject(project.id));
    const avatar = el("div", "project-avatar", String(project.name || "P").split(/\s+/).map((word) => word[0]).slice(0, 2).join("").toUpperCase());
    const top = el("div", "project-card-top", [el("div", "project-title-row", [avatar, el("div", "project-title-block", [title, projectSource(project)])]), el("div", "project-state-row", [statusBadge(projectStatus(project)), ...(project.demo ? [el("span", "validation-label", "Demo")] : [])]), el("div", "project-targets", (project.targets || []).map(targetPill)), el("div", "project-meta", [meta("Latest package", newest?.version || "No package yet"), meta("Preferred package", good?.version || "Not selected"), meta("Preserved source", project.latest_version || "Not captured yet")])]);
    const active = projectActiveJob(project);
    const description = project.recipe?.mode === "capture" ? "Update tracking only. Automatic package builds are not configured." : newest ? "Portable packages available. Check each platform’s test status below." : "No package yet. Open this game to prepare a build.";
    top.append(el("p", "compact-note", description));
    if (newest) {
      const platforms = el("div", "package-platforms");
      [...new Set(artifacts.map((artifact) => artifact.target))].forEach((target) => {
        const artifact = artifacts.find((item) => item.target === target);
        platforms.append(el("div", "package-platform-row", [targetPill(target), validationLabel(artifact), artifactDownload(artifact, "table-action", "Download →")]));
      });
      top.append(platforms);
    }
    if (project.last_error && !active) top.append(el("p", "project-error", project.last_error));
    const bottom = el("div", "project-card-bottom", [el("span", "card-update-time", active ? "Game is running" : project.last_checked ? `Checked ${timeAgo(project.last_checked).toLowerCase()}` : "Not checked yet"), button("Open game →", "button secondary small", () => navigateProject(project.id))]);
    grid.append(el("article", "project-card", [top, bottom]));
  });
}
function jobRow(job) {
  const project = projectById(job.project_id);
  const actions = el("div", "table-actions", button("View game →", "table-action", () => navigateProject(job.project_id, "activity", job.id)));
  if (job.status === "failed") actions.append(button("Retry step", "table-action", () => retryJob(job.id), busy.has(`retry-${job.id}`)));
  return el("tr", "", [el("td", "", [el("span", "table-title", job.project_name || project?.name || "Project"), el("span", "table-detail", `${job.version || "Update check"} · ${targetLabels[job.target] || job.target || "Source"}`)]), el("td", "", statusBadge(jobStage(job))), el("td", "", dateLabel(job.created_at)), el("td", "", actions)]);
}
function packageRow(artifact) {
  const project = projectById(artifact.project_id);
  return el("tr", "", [el("td", "", [el("span", "table-title", artifact.project_name || project?.name || "Project"), el("span", "table-detail", `${artifact.version || "—"} · ${targetLabels[artifact.target] || artifact.target}`)]), el("td", "", validationLabel(artifact)), el("td", "", bytes(artifact.size)), el("td", "", el("div", "table-actions", [artifactDownload(artifact, "table-action", "Download"), button("All versions →", "table-action", () => navigateProject(artifact.project_id, "versions"))]))]);
}
function renderActivity() {
  const tabs = { jobs: $("jobs-tab"), packages: $("packages-tab"), notifications: $("notifications-tab") };
  Object.entries(tabs).forEach(([key, node]) => { node.classList.toggle("selected", activityView === key); node.setAttribute("aria-selected", String(activityView === key)); });
  $("jobs-count").textContent = state.jobs.length; $("artifacts-count").textContent = state.artifacts.length; $("notifications-count").textContent = state.notifications.filter(notificationUnread).length;
  const content = $("activity-content"); content.setAttribute("aria-labelledby", tabs[activityView].id);
  if (activityView === "jobs") content.replaceChildren(state.jobs.length ? table(["Game / version", "Current step", "Started", ""], newestFirst(state.jobs).slice(0, 60).map(jobRow)) : emptyState("◷", "The pipeline is quiet", "A game’s update checks and builds will appear here."));
  if (activityView === "packages") content.replaceChildren(state.artifacts.length ? table(["Game / package", "Validation", "Size", ""], newestFirst(state.artifacts).map(packageRow)) : emptyState("▱", "Your first package is ahead", "Completed packages will appear here with their validation level."));
  if (activityView === "notifications") renderNotifications(content);
}
function renderNotifications(parent) {
  parent.replaceChildren();
  const notifications = newestFirst(state.notifications);
  const unread = notifications.filter(notificationUnread).length;
  if (!notifications.length) { parent.append(emptyState("✓", "You’re all caught up", "Ready packages and events that need your attention will appear here.")); return; }
  parent.append(el("div", "notification-toolbar", [el("span", "", `${unread} unread · ${notifications.length} events`), button("Mark all read", "text-button", markAllRead, !unread || busy.has("read-all"))]));
  notifications.forEach((notification) => {
    const failure = /fail|error|unavailable|attention/.test(notification.kind || notification.type || "");
    const main = el("div", "notification-body", [el("h3", "", notificationTitle(notification)), el("p", "", notification.message || notification.body || ""), el("span", "notification-time", dateLabel(notification.created_at))]);
    const actions = el("div", "notification-actions");
    if (notification.project_id) actions.append(button("Open game →", "table-action", () => { if ($("inbox-dialog").open) $("inbox-dialog").close(); navigateProject(notification.project_id); }));
    if (notificationUnread(notification)) actions.append(button("Mark read", "table-action", () => markRead(notification.id), busy.has(`read-${notification.id}`)));
    main.append(actions);
    parent.append(el("div", `notification-item${notificationUnread(notification) ? " unread" : ""}`, [el("span", `notification-symbol${failure ? " error" : ""}`, failure ? "!" : "✓"), main]));
  });
}
function renderSettingsTabs() {
  document.querySelectorAll("[data-settings-tab]").forEach((node) => { const selected = node.dataset.settingsTab === settingsView; node.classList.toggle("selected", selected); node.setAttribute("aria-selected", String(selected)); $(`settings-${node.dataset.settingsTab}`).hidden = !selected; });
}
function renderSettings() {
  const config = rommConfig();
  const connected = config.connected || config.last_test?.ok || config.last_test?.success;
  $("romm-connection-status").replaceWith(Object.assign(statusBadge(connected ? "ready" : rommReady() ? "watching" : "needs_rom", connected ? "Connection verified" : rommReady() ? "Token saved" : "Not connected"), { id: "romm-connection-status" }));
  $("romm-token-help").textContent = config.token_configured || config.has_token ? "A token is saved. Leave this field empty to keep it, or paste a replacement. Tokens never appear in recipes or browser state." : "Create a RomM Client API Token with permission to read your library and download ROMs. Your token stays on this server.";
  $("romm-token").placeholder = config.token_configured || config.has_token ? "Saved token · leave blank to keep" : "Paste a RomM token";
  $("romm-test").disabled = !rommReady() || busy.has("romm-settings"); $("romm-clear").disabled = !(config.token_configured || config.has_token) || busy.has("romm-settings");
  const assistant = state.assistant || state.assistant_settings || state.settings?.assistant || {};
  const fingerprint = JSON.stringify([config.url, assistant.endpoint, assistant.model]);
  if (fingerprint !== formConfigFingerprint) {
    if (!$('romm-url').dataset.dirty) $("romm-url").value = config.url || "";
    if (!$('assistant-endpoint').dataset.dirty) $("assistant-endpoint").value = assistant.endpoint || "http://127.0.0.1:11434";
    if (!$('assistant-model').dataset.dirty) $("assistant-model").value = assistant.model || "";
    formConfigFingerprint = fingerprint;
  }
  const settings = state.settings || {};
  $("storage-grid").replaceChildren(...[["Original game files", settings.input_root], ["Preservation archive", settings.archive_root], ["Local library output", settings.library_root]].map(([label, path]) => el("div", "storage-item", [el("h3", "", label), el("code", "", path || "Not configured")])));
  const docker = state.runner?.docker_available && settings.docker_enabled;
  $("runner-state").replaceWith(Object.assign(statusBadge(docker ? "ready" : "watching", docker ? "Docker available" : "Local assembly"), { id: "runner-state" }));
  const detail = $("runner-details"); detail.replaceChildren();
  const enabled = state.runner?.profile_builds_enabled;
  const buildNote = el("div", "runner-note", [el("h3", "", "Verified profile runner"), statusBadge(enabled ? "ready" : "capture_only", enabled ? "Enabled" : "Not enabled")]);
  buildNote.append(el("p", "", enabled ? "The reviewed Ship of Harkinian profile can compile its extractor locally. Custom recipes use the assembly or configured Docker runner." : "Start Portkeeper with --enable-profile-builds to let the reviewed Ship of Harkinian profile compile its extractor locally."));
  const missing = state.runner?.missing || state.runner?.missing_tools || state.runner?.profile_tools_missing || state.runner?.profile_runner?.missing_tools || state.runner?.profile_runner?.missing;
  if (Array.isArray(missing) && missing.length) buildNote.append(el("p", "", `Required tools missing: ${missing.join(", ")}`));
  detail.append(buildNote, el("p", "quiet-note", "Device support is defined by each profile or recipe. The current Ship of Harkinian profile supports Linux, Steam Deck through Linux, and Windows."));
  renderNotificationPreference(); renderToolchain();
}
function renderNotificationPreference() {
  const supported = "Notification" in window;
  const allowed = supported && Notification.permission === "granted";
  $("browser-notifications").textContent = desktopNotifications && allowed ? "Disable desktop notifications" : "Enable desktop notifications";
  $("browser-notifications").disabled = !supported || (supported && Notification.permission === "denied");
  $("browser-notification-state").textContent = !supported ? "This browser does not support desktop notifications. The in-app inbox is available." : Notification.permission === "denied" ? "Browser permission is blocked. Enable it in your browser’s site settings if you want desktop notifications." : desktopNotifications && allowed ? "Enabled for this browser while Portkeeper is open." : "Disabled for this browser. Your inbox still records updates.";
}
function render() {
  const fingerprint = JSON.stringify([state, [...busy], route, activityView, settingsView]);
  if (fingerprint === lastRender) return;
  lastRender = fingerprint;
  $("nav-project-count").textContent = state.projects.length;
  const unread = state.notifications.filter(notificationUnread).length;
  [$("nav-notification-count"), $("inbox-count")].forEach((node) => { node.textContent = unread; node.hidden = !unread; });
  renderLibrary(); renderActivity(); renderSettings();
  if (route.page === "project") renderProject();
  if ($("inbox-dialog").open) renderNotifications($("inbox-content"));
}
function processNotifications() {
  if (!notificationsInitialized) { state.notifications.forEach((item) => seenNotifications.add(String(item.id))); notificationsInitialized = true; return; }
  state.notifications.forEach((notification) => {
    const key = String(notification.id);
    if (seenNotifications.has(key)) return;
    seenNotifications.add(key);
    if (!notificationUnread(notification)) return;
    const title = notificationTitle(notification);
    const message = notification.message || notification.body || "";
    toast(`${title}${message ? `: ${message}` : ""}`, /fail|error/.test(notification.kind || notification.type || ""));
    if (desktopNotifications && "Notification" in window && Notification.permission === "granted") {
      const desktop = new Notification(title, { body: message, tag: `portkeeper-${key}` });
      desktop.onclick = () => { window.focus(); if (notification.project_id) navigateProject(notification.project_id); else { activityView = "notifications"; location.hash = "activity"; } desktop.close(); };
    }
  });
}
async function refresh(manual = false) {
  if (refreshing) return;
  refreshing = true;
  try {
    const result = await api("/api/state");
    (result.projects || []).forEach((project) => {
      const previous = projectById(project.id);
      const previousJobs = projectJobs(project.id).map((job) => `${job.id}:${job.status}`).join(",");
      const nextJobs = (result.jobs || []).filter((job) => job.project_id === project.id).map((job) => `${job.id}:${job.status}`).join(",");
      if (previous?.capture_id !== project.capture_id || previousJobs !== nextJobs) preservationCache.delete(project.id);
    });
    state = { ...state, ...result, projects: result.projects || [], jobs: result.jobs || [], artifacts: result.artifacts || [], notifications: result.notifications || [] };
    if (Array.isArray(result.profiles)) profiles = result.profiles;
    $("load-error").hidden = true;
    $("connection").classList.remove("offline"); $("connection").replaceChildren(el("span", "status-dot"), document.createTextNode("Workspace connected"));
    $("last-refresh").textContent = `Synced ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
    processNotifications(); render();
    if (manual) toast("Workspace refreshed.");
  } catch (error) {
    $("connection").classList.add("offline"); $("connection").replaceChildren(el("span", "status-dot"), document.createTextNode("Server unavailable"));
    $("load-error").textContent = `Could not refresh: ${error.message}. Retrying automatically.`; $("load-error").hidden = false;
  } finally { refreshing = false; }
}
async function runAction(key, path, body, message) {
  if (busy.has(key)) return null;
  busy.add(key); render();
  try { const result = await api(path, { method: "POST", body }); if (message) toast(message); await refresh(); return result; }
  catch (error) { toast(error.message, true); return null; }
  finally { busy.delete(key); render(); }
}
function checkProject(id) { return runAction(`check-${id}`, `/api/projects/${encodeURIComponent(id)}/check`, {}, "Update check queued."); }
function buildProject(id) { return runAction(`build-${id}`, `/api/projects/${encodeURIComponent(id)}/build`, {}, "Build queued. Follow its progress in Activity."); }
function testBuild(id) { return runAction(`build-${id}`, `/api/projects/${encodeURIComponent(id)}/test-build`, {}, "Test build queued. Source will be preserved before packaging."); }
function retryJob(id) { return runAction(`retry-${id}`, `/api/jobs/${encodeURIComponent(id)}/retry`, {}, "Retry queued for the failed step."); }
function publishArtifact(id) { return runAction(`publish-${id}`, `/api/artifacts/${encodeURIComponent(id)}/publish`, {}, "This version is now in your local library folder."); }
function pinArtifact(artifact) { return runAction(`pin-${artifact.id}`, `/api/artifacts/${encodeURIComponent(artifact.id)}/pin`, { pinned: !artifact.pinned }, artifact.pinned ? "Version unpinned." : "Version pinned. Retention will keep it."); }
function setKnownGood(id) { return runAction(`good-${id}`, `/api/artifacts/${encodeURIComponent(id)}/known-good`, {}, "Known-good version saved for this target."); }
function markRead(id) { return runAction(`read-${id}`, `/api/notifications/${encodeURIComponent(id)}/read`, {}, ""); }
function markAllRead() { return runAction("read-all", "/api/notifications/read-all", {}, "Inbox marked read."); }

function renderProject() {
  const project = projectById(route.projectId);
  const heading = $("project-heading"), content = $("project-content");
  const focused = content.contains(document.activeElement) && document.activeElement.id ? { id: document.activeElement.id, start: document.activeElement.selectionStart, end: document.activeElement.selectionEnd } : null;
  const openJobs = new Set([...content.querySelectorAll("details[open][data-job-id]")].map((node) => node.dataset.jobId));
  heading.replaceChildren(); content.replaceChildren();
  document.querySelectorAll("[data-project-tab]").forEach((node) => { const selected = node.dataset.projectTab === route.tab; node.classList.toggle("selected", selected); node.setAttribute("aria-selected", String(selected)); });
  content.setAttribute("aria-labelledby", `project-${route.tab}-tab`);
  if (!project) { heading.append(el("div", "", [el("h1", "", "Game unavailable"), el("p", "", "Return to the library to choose a game.")])); return; }
  const title = el("div", "", [el("div", "eyebrow", project.profile ? "VERIFIED PROFILE" : project.demo ? "SYNTHETIC DEMO" : "CUSTOM PROJECT"), el("h1", "", project.name), projectSource(project), el("div", "project-targets", (project.targets || []).map(targetPill))]);
  const working = Boolean(projectActiveJob(project)) || busy.has(`check-${project.id}`) || busy.has(`build-${project.id}`);
  const actions = el("div", "project-header-actions", statusBadge(projectStatus(project)));
  if (!project.demo) actions.append(button("Check update", "button secondary small", () => checkProject(project.id), working));
  if (project.profile) actions.append(button("Test build", "button primary small", () => testBuild(project.id), working));
  else if (project.recipe?.mode !== "capture") actions.append(button("Build package", "button primary small", () => buildProject(project.id), working || !project.capture_id));
  heading.append(title, actions);
  if (route.tab === "overview") renderOverview(project, content);
  if (route.tab === "versions") renderVersions(project, content);
  if (route.tab === "activity") renderProjectActivity(project, content);
  if (route.tab === "advanced") renderAdvanced(project, content, openJobs);
  if (focused && $(focused.id)) { $(focused.id).focus({ preventScroll: true }); if (focused.start !== null && focused.start !== undefined && typeof $(focused.id).setSelectionRange === "function") $(focused.id).setSelectionRange(focused.start, focused.end); }
}
function renderOverview(project, content) {
  const grid = el("div", "detail-grid"), left = el("div"), right = el("div");
  const artifacts = projectArtifacts(project.id), latest = artifacts[0];
  const current = artifacts.filter((artifact) => artifact.published);
  const packageCard = el("section", "detail-card", el("h2", "", "Portable packages"));
  if (latest) {
    packageCard.append(el("div", "detail-facts", [meta("Latest package", latest.version), meta("Validation", validationLevel(latest).replace(/_/g, " ")), meta("In local library", current.length ? current.map((artifact) => `${artifact.version} (${targetLabels[artifact.target] || artifact.target})`).join(", ") : "No package selected", true)]), validationLabel(latest), el("p", "compact-note", "A package is ready to download. Gameplay is confirmed only when you record your own play test."), el("div", "inline-actions", [artifactDownload(latest), button("Browse all versions →", "text-button", () => navigateProject(project.id, "versions"))]));
  } else packageCard.append(el("p", "detail-empty", project.profile ? "Your selected ROM and the verified profile will be used for the first test build." : "No packages yet. Preserved source is kept independently of your build results."));
  if (project.recipe?.mode === "capture") packageCard.append(el("p", "quiet-note", "This game tracks and preserves upstream updates. It does not automatically build a playable package. Manually imported games can be found in RomM."), button("Open RomM →", "button secondary small", () => { const url = state.romm?.url; if (url && /^https?:\/\//.test(url)) window.open(url, "_blank", "noopener,noreferrer"); else navigateSettings("romm"); }));
  if (artifacts.length > 1) packageCard.append(el("div", "package-platforms", [...new Set(artifacts.map((artifact) => artifact.target))].map((target) => { const artifact = artifacts.find((item) => item.target === target); return el("div", "package-platform-row", [targetPill(target), validationLabel(artifact), artifactDownload(artifact, "table-action", "Download →")]); })));
  left.append(packageCard);
  const job = projectActiveJob(project) || projectJobs(project.id)[0];
  const progress = el("section", "detail-card", [el("h2", "", projectActiveJob(project) ? "Build in progress" : "Latest game run")]);
  if (job) {
    progress.append(statusBadge(jobStage(job)), progressSteps(job, project), el("p", "progress-caption", job.error || `${job.version || "Update check"} · ${dateLabel(job.created_at)}`));
    if (job.status === "failed") progress.append(el("div", "inline-actions", button("Retry failed step", "button secondary small", () => retryJob(job.id), busy.has(`retry-${job.id}`))));
    progress.append(el("div", "inline-actions", button("View activity →", "text-button", () => navigateProject(project.id, "activity"))));
  } else progress.append(el("p", "detail-empty", "Check for an update or start a test build to begin."));
  left.append(progress);
  const game = el("section", "detail-card", [el("h2", "", "Game files & schedule")]);
  const source = project.rom_source || {};
  game.append(el("div", "detail-facts", [meta("Game input", source.member || source.filename || source.path || (project.demo ? "Synthetic fixtures" : "Recipe-defined local files"), true), meta("ROM revision", source.revision || profiles.find((profile) => profile.id === project.profile)?.supported_roms?.find((rom) => rom.sha1 === source.sha1)?.name || (source.sha1 ? "Hash verified" : "Not selected")), meta("Update schedule", `Every ${project.poll_minutes || 60} min`), meta("After an update", project.auto_build ? "Build automatically" : "Preserve and wait"), meta("Local library copy", project.auto_publish ? "Automatic" : "Manual")]));
  if (source.sha1) game.append(statusBadge("ready", "ROM hash selected"));
  if (project.last_error && !projectActiveJob(project)) game.append(el("p", "project-error", project.last_error));
  const identity = project.metadata || {};
  const metadataCard = el("section", "detail-card", [el("h2", "", "Game metadata & artwork"), el("p", "detail-note", "Link the original game or a port-specific IGDB entry. Choose custom port artwork in RomM once, then reuse it for later builds."), el("div", "detail-facts", [meta("Game", identity.game || "Not linked"), meta("IGDB ID", identity.igdb_id || "Not linked"), meta("Artwork", identity.cover_policy === "romm" ? "Follow linked RomM cover" : "Keep current covers")]), button("Edit metadata link", "button secondary small", () => openMetadata(project.id))]);
  if (identity.romm_rom_id && rommConfig().url) { const link = el("a", "text-button", "Choose metadata & cover in RomM →"); link.href = `${rommConfig().url}/rom/${identity.romm_rom_id}`; link.target = "_blank"; link.rel = "noopener noreferrer"; metadataCard.append(el("div", "inline-actions", link)); }
  right.append(metadataCard, game, retentionCard(project), preservationCard(project));
  grid.append(left, right); content.append(grid);
}
function progressSteps(job, project) {
  const stage = job.stage || job.status;
  if (Array.isArray(job.checkpoints) && job.checkpoints.length) {
    const names = { checking: "Check upstream", preserving_source: "Preserve source", preserving_assets: "Preserve release assets", capturing_dependencies: "Preserve pinned dependencies", building_extractor: "Build reviewed extractor", downloading_rom: "Verify game input", extracting: "Extract game assets", packaging_linux: "Package Linux / Steam Deck", packaging_windows: "Package Windows", preparing: "Prepare inputs", validating: "Validate package", complete: job.target === "capture" ? "Save preservation checkpoint" : "Archive package" };
    const list = el("ol", "progress-steps");
    job.checkpoints.forEach((checkpoint, index) => {
      const failed = job.status === "failed" && index === job.checkpoints.length - 1;
      const complete = checkpoint.status === "complete" && !failed;
      list.append(el("li", failed ? "failed" : complete ? "complete" : "current", [el("span", "step-dot", failed ? "!" : complete ? "✓" : "•"), el("span", "step-label", names[checkpoint.stage] || checkpoint.stage.replace(/_/g, " "))]));
    });
    return list;
  }
  const labels = [["preserving", "Preserve source & release"], ["downloading_rom", "Verify game input"], ["extracting", "Extract game assets"], ...(job.target && job.target !== "capture" ? [job.target] : project.targets || []).map((target) => [`packaging_${target}`, `Package ${targetLabels[target] || target}`]), ["ready", "Archive package"]];
  if (job.kind === "capture" || job.target === "capture") labels.splice(1, labels.length - 1, ["ready", "Save preservation checkpoint"]);
  const aliases = { queued: -1, checking: 0, capturing: 0, preserving: 0, preserving_source: 0, preserving_assets: 0, capturing_dependencies: 0, building_extractor: 2, preparing: 1, validating: labels.length - 1, complete: labels.length - 1, building: 2, needs_rom: 1, missing_inputs: 1, succeeded: labels.length - 1, completed: labels.length - 1, ready: labels.length - 1, failed: -1 };
  const index = labels.findIndex(([key]) => key === stage);
  const current = index >= 0 ? index : aliases[stage] ?? -1;
  const succeeded = ["succeeded", "success", "completed", "ready", "built"].includes(job.status);
  const list = el("ol", "progress-steps");
  labels.forEach(([_, label], position) => { const complete = succeeded || position < current; const running = !succeeded && position === current; list.append(el("li", complete ? "complete" : running ? "current" : "", [el("span", "step-dot", complete ? "✓" : running ? "•" : String(position + 1)), el("span", "step-label", label)])); });
  return list;
}
function retentionCard(project) {
  const card = el("section", "detail-card", [el("h2", "", "Keep the versions you want")]);
  const draft = retentionDrafts.get(project.id) || { mode: project.retention_keep_last || project.retention?.keep_last ? "last" : "all", count: project.retention_keep_last || project.retention?.keep_last || 3 };
  const select = el("select"); select.setAttribute("aria-label", "Version retention");
  [ ["all", "Keep every version"], ["last", "Keep latest versions"] ].forEach(([value, label]) => { const option = el("option", "", label); option.value = value; select.append(option); }); select.value = draft.mode;
  const count = input(`retention-count-${project.id}`, "number", draft.count); count.min = "1"; count.max = "1000"; count.setAttribute("aria-label", "Number of versions to retain per target"); count.hidden = draft.mode !== "last";
  const update = () => { count.hidden = select.value !== "last"; retentionDrafts.set(project.id, { mode: select.value, count: Number(count.value) || 3 }); };
  select.addEventListener("change", update); count.addEventListener("input", update);
  card.append(el("div", "retention-controls", [select, count]), el("p", "compact-note", "Pinned, known-good, and current library packages are protected. Save a policy to clean up future builds automatically."), el("div", "inline-actions", button("Preview retention →", "button secondary small", () => previewRetention(project.id), busy.has(`retention-${project.id}`))));
  return card;
}
function preservationCard(project) {
  const card = el("section", "detail-card", [el("h2", "", "Preservation bundle")]);
  const data = preservationCache.get(project.id);
  if (!project.capture_id) { card.append(el("p", "detail-empty", "The first upstream capture creates a source checkpoint. Bundle completeness is reported after capture.")); return card; }
  if (!data) {
    card.append(el("p", "detail-empty", preservationLoading.has(project.id) ? "Checking preserved materials…" : "Checking bundle completeness…"));
    loadPreservation(project.id); return card;
  }
  if (data.error) { card.append(el("p", "project-error", data.error), button("Check again", "text-button", () => { preservationCache.delete(project.id); renderProject(); })); return card; }
  const materials = data.materials || data.checklist || data.items || [];
  const complete = data.complete === true || data.completeness === "complete";
  card.append(statusBadge(complete ? "ready" : "capture_only", complete ? "Materials complete" : "Completeness reported"));
  const list = el("ul", "checklist");
  const normalized = Array.isArray(materials) ? materials.map((item) => typeof item === "string" ? { id: item, available: true } : item) : Object.entries(materials).map(([key, item]) => typeof item === "object" ? { id: key, ...item } : { id: key, available: Boolean(item) });
  normalized.unshift({ label: "Upstream source & release assets", available: data.source_archived ?? Boolean(project.capture_id), detail: "Preserved capture with file checksums" });
  normalized.push({ label: "Build toolchain", available: Boolean(data.toolchain_archived), detail: data.toolchain_archived ? "Registered toolchain archive included in future exports" : "No toolchain archive registered" });
  normalized.push({ label: "Offline rebuild verification", available: Boolean(data.offline_rebuild_verified), detail: data.offline_rebuild_verified ? "An offline rebuild was verified" : "An offline rebuild has not been verified" });
  if (normalized.length) normalized.forEach((material) => {
    const available = material.available ?? material.present ?? material.complete ?? ["preserved", "available", "complete", "captured"].includes(material.status);
    const name = material.label || material.name || material.id?.replace(/_/g, " ") || "Material";
    list.append(el("li", "", [el("span", `check-icon${available ? "" : " missing"}`, available ? "✓" : "!"), el("span", "", [el("span", "check-title", name), el("span", "check-description", material.detail || material.description || material.reason || (available ? "Preserved" : "Not preserved"))])]));
  });
  else list.append(el("li", "", [el("span", "check-icon", "✓"), el("span", "", "Source checkpoint preserved. See the exported manifest for available materials.")]));
  card.append(list, el("p", "compact-note", data.note || data.notes || "Source, dependencies, patches, and toolchain material are listed explicitly. A checkpoint may still need tools or game files for a future rebuild."), el("div", "inline-actions", button("Export preservation bundle", "button secondary small", () => exportPreservation(project.id), busy.has(`export-${project.id}`))));
  const source = el("a", "text-button", "Download captured source →"); source.href = `/api/captures/${encodeURIComponent(project.capture_id)}/download`; source.setAttribute("download", ""); card.append(el("div", "inline-actions", source));
  const exports = newestFirst(data.exports || []).slice(0, 3);
  if (exports.length) {
    const saved = el("div", "saved-preservation", el("h3", "", "Saved bundles"));
    exports.forEach((record) => {
      const version = record.manifest?.version || "";
      const link = safeDownloadLink(record.download_url, `↓ ${version} · ${dateLabel(record.created_at)} · ${bytes(record.size)}`);
      if (link) saved.append(el("div", "inline-actions", link));
    });
    card.append(saved);
  }
  return card;
}
async function loadPreservation(id) {
  if (preservationLoading.has(id)) return;
  preservationLoading.add(id);
  try { preservationCache.set(id, await api(`/api/projects/${encodeURIComponent(id)}/preservation`)); }
  catch (error) { preservationCache.set(id, { error: error.message }); }
  finally { preservationLoading.delete(id); if (route.projectId === id && route.tab === "overview") renderProject(); }
}
async function exportPreservation(id) {
  const result = await runAction(`export-${id}`, `/api/projects/${encodeURIComponent(id)}/preservation/export`, {}, "Preservation bundle exported.");
  if (!result) return;
  const link = safeDownloadLink(result.download_url || result.export?.download_url, "↓ Download preservation bundle");
  if (link) { const card = el("div", "toast", [el("span", "", "Your preservation bundle is ready."), link]); $("toasts").append(card); }
  else toast("Export completed. Its download URL was not returned.", true);
  preservationCache.delete(id); if (route.projectId === id) loadPreservation(id);
}
function renderVersions(project, content) {
  const artifacts = projectArtifacts(project.id);
  if (!artifacts.length) { content.append(emptyState("▱", "No packages yet", "Start a test build. Versions will appear here with download, pin, and rollback controls.")); return; }
  const list = el("div", "version-list");
  artifacts.forEach((artifact) => {
    const labels = [validationLabel(artifact)];
    if (artifact.pinned) labels.push(statusBadge("watching", "Pinned"));
    if (knownGood(artifact, project)) labels.push(statusBadge("ready", "Known good"));
    if (artifact.published) labels.push(statusBadge("published"));
    const actions = el("div", "version-actions", [artifactDownload(artifact), button(artifact.pinned ? "Unpin" : "Pin version", "table-action", () => pinArtifact(artifact), busy.has(`pin-${artifact.id}`))]);
    if (!knownGood(artifact, project)) {
      const goodButton = button("Set known good", "table-action", () => setKnownGood(artifact.id), busy.has(`good-${artifact.id}`) || !["launch_checked", "user_confirmed_playable"].includes(validationLevel(artifact)));
      if (goodButton.disabled) goodButton.title = "Record a launch check or confirm gameplay before selecting a known-good package.";
      actions.append(goodButton);
    }
    if (!artifact.demo && !project.demo && validationLevel(artifact) !== "user_confirmed_playable") actions.append(button("Confirm playable", "table-action", () => openValidation(artifact)));
    if (!artifact.published) actions.append(button("Use in local library", "table-action", () => publishArtifact(artifact.id), busy.has(`publish-${artifact.id}`)));
    list.append(el("article", "version-card", [el("div", "version-heading", [el("h3", "", artifact.version || "Archived version"), targetPill(artifact.target)]), el("div", "version-labels", labels), el("div", "version-meta", [el("span", "", bytes(artifact.size)), el("span", "", dateLabel(artifact.created_at))]), ...(artifact.validation?.notes ? [el("p", "version-description", artifact.validation.notes)] : !artifact.demo && !project.demo && validationLevel(artifact) === "packaged" ? [el("p", "version-description", "Confirm gameplay after trying this package to select it as a known-good version.")] : []), actions]));
  });
  content.append(el("p", "section-intro", "Selecting an older version for the local library rolls it back. Pinned and known-good packages are protected from retention cleanup."), el("div", "inline-actions", button("Retention settings →", "text-button", () => navigateProject(project.id))), list);
  list.style.marginTop = "22px";
}
function renderProjectActivity(project, content) {
  const jobs = projectJobs(project.id);
  if (!jobs.length) { content.append(emptyState("◷", "No runs yet", "Check for an update or start a test build. This view shows meaningful steps; diagnostics are in Advanced.")); return; }
  const panel = el("div", "activity-panel");
  jobs.forEach((job) => {
    const main = el("div", "job-item-main", [el("div", "job-item-top", [el("h3", "", job.version || "Update check"), targetPill(job.target || "capture"), statusBadge(jobStage(job))]), el("p", "", job.error || job.message || (job.status === "succeeded" ? "Game step completed." : "Follow the current step above."))]);
    const actions = el("div", "inline-actions", button("View diagnostics →", "table-action", () => navigateProject(project.id, "advanced", job.id)));
    if (job.status === "failed") actions.prepend(button("Retry failed step", "table-action", () => retryJob(job.id), busy.has(`retry-${job.id}`)));
    main.append(actions); panel.append(el("article", "job-item", [main, el("span", "job-date", dateLabel(job.created_at))]));
  });
  content.append(panel);
}
function renderAdvanced(project, content, openJobs) {
  const stack = el("div", "advanced-stack");
  const configuration = el("section", "detail-card", [el("h2", "", "Recipe & automation"), el("p", "detail-note", project.profile ? "This game uses fixed, reviewed build instructions for its supported release. Automation settings are editable; changing trusted build execution requires a profile update. New unsupported releases are preserved for review." : "A custom recipe controls required inputs, outputs, and the build runner.")]);
  configuration.append(el("div", "detail-facts", [meta("Recipe mode", project.recipe?.mode || "capture"), meta("Automatic build", project.auto_build ? "Enabled" : "Disabled"), meta("Automatic local library copy", project.auto_publish ? "Enabled" : "Disabled"), meta("Polling interval", `${project.poll_minutes || 60} minutes`)]), button("Edit recipe & automation", "button secondary small", () => openRecipe(project.id)));
  const json = el("details", "diagnostics-list"); json.append(el("summary", "table-action", "View saved recipe JSON"), el("pre", "json-preview", JSON.stringify(project.recipe || captureRecipe, null, 2))); configuration.append(json); stack.append(configuration);
  const assistant = el("section", "detail-card", [el("h2", "", "Ask for a recipe proposal"), el("p", "detail-note", project.profile ? "A local model can draft notes for this verified profile. Its reviewed build instructions remain fixed; proposals are reviewed before saving." : "Describe a required change or a build error. A local model can draft a proposal; you review the JSON before saving it.")]);
  const prompt = el("textarea", "assistant-prompt"); prompt.id = "assistant-project-prompt"; prompt.rows = 3; prompt.placeholder = "Explain the change you want to make…"; prompt.value = assistantPromptDrafts.get(project.id) || ""; prompt.addEventListener("input", () => assistantPromptDrafts.set(project.id, prompt.value));
  field(assistant, "Requested change", prompt);
  const assistantConfig = state.assistant || state.assistant_settings || state.settings?.assistant || {};
  const configured = Boolean(assistantConfig.endpoint && assistantConfig.model);
  assistant.append(el("div", "inline-actions", [button(busy.has(`proposal-${project.id}`) ? "Drafting proposal…" : "Draft proposal", "button secondary small", () => draftProposal(project.id), !configured || busy.has(`proposal-${project.id}`)), button("Assistant settings →", "text-button", () => navigateSettings("assistant"))]));
  if (!configured) assistant.append(el("p", "field-help", "Configure your local Ollama endpoint and model in Settings first.")); stack.append(assistant);
  const diagnostic = el("section", "detail-card", [el("h2", "", "Build diagnostics"), el("p", "detail-note", "Detailed logs and errors for troubleshooting a game step.")]);
  const jobs = projectJobs(project.id); const logs = el("div", "diagnostics-list");
  jobs.forEach((job) => { const details = el("details", "job-panel"); details.dataset.jobId = job.id; details.open = openJobs.has(String(job.id)) || String(job.id) === String(selectedJobId); details.append(el("summary", "", [targetPill(job.target || "capture"), el("span", "", job.version || "Update check"), statusBadge(jobStage(job)), el("span", "job-date", dateLabel(job.created_at))]), el("pre", "", (Array.isArray(job.logs) ? job.logs.join("\n") : String(job.logs || "Waiting for log output…")) + (job.error ? `\n\nError: ${job.error}` : ""))); logs.append(details); });
  diagnostic.append(jobs.length ? logs : el("p", "detail-empty", "No diagnostics yet.")); stack.append(diagnostic);
  if (project.rom_source?.sha1) stack.append(el("section", "detail-card", [el("h2", "", "Game input identity"), el("div", "detail-facts", [meta("ROM SHA-1", project.rom_source.sha1, true), meta("Cache key", project.rom_source.cache_key || "Not cached", true)])]));
  content.append(stack);
}

function openMetadata(id) {
  const project = projectById(id); metadataProjectId = id;
  const identity = project.metadata || {};
  $("metadata-game").value = identity.game || project.name;
  $("metadata-prefix").value = identity.filename_prefix || project.library_basename || "";
  $("metadata-romm").value = identity.romm_rom_id || "";
  $("metadata-igdb").value = identity.igdb_id || "";
  $("metadata-cover").value = identity.cover_policy || "preserve";
  $("metadata-error").textContent = ""; $("metadata-dialog").showModal();
}
async function saveMetadata(event) {
  event.preventDefault(); $("metadata-save").disabled = true;
  try {
    const identity = {game: $("metadata-game").value.trim(), filename_prefix: $("metadata-prefix").value.trim(), cover_policy: $("metadata-cover").value};
    const reference = $("metadata-romm").value.trim();
    if (reference) {
      const match = reference.match(/^(?:https?:\/\/[^/]+\/rom\/)?([0-9]+)\/?$/);
      if (!match) throw new Error("Enter a RomM entry ID or its /rom/123 link.");
      await api(`/api/projects/${metadataProjectId}/metadata-link`, {method: "POST", body: {...identity, romm_rom_id: Number(match[1])}});
    } else {
      const igdb_id = Number($("metadata-igdb").value);
      if (!Number.isSafeInteger(igdb_id) || igdb_id < 1) throw new Error("Link a matched RomM entry or enter its IGDB game ID.");
      await api(`/api/projects/${metadataProjectId}`, {method: "PATCH", body: {metadata: {...identity, igdb_id}}});
    }
    $("metadata-dialog").close(); await refresh(); toast("Metadata linked. Imports are checked every minute after RomM scans them.");
  } catch (error) { $("metadata-error").textContent = error.message; }
  finally { $("metadata-save").disabled = false; }
}

function openWizard() {
  wizardSerial += 1; wizardBusy = false;
  wizard = { step: rommReady() ? 2 : 1, profile: profiles[0]?.id || "shipwright", query: "Ocarina of Time", roms: [], rom: null, files: [], filename: null, inspection: null, candidate: null, targets: ["linux", "windows"], name: "Ship of Harkinian", pollMinutes: 60, autoBuild: true, autoPublish: false, url: rommConfig().url || "", token: "" };
  $("wizard-error").textContent = ""; renderWizard(); $("worker-dialog").showModal();
}
function wizardProfile() { return profiles.find((profile) => profile.id === wizard?.profile); }
function profileTargetLabel(profile, target) { return profile?.devices?.find((device) => device.id === target)?.label || targetLabels[target] || target; }
function profileTargets(profile) {
  const values = profile?.targets || ["linux", "windows"];
  return values.map((value) => typeof value === "object" ? value.id || value.target : value).filter(Boolean);
}
function renderWizard() {
  if (!wizard) return;
  const labels = ["Connect", "Project", "Game files", "Devices", "Test build"];
  $("wizard-steps").replaceChildren(...labels.map((text, index) => el("li", index + 1 === wizard.step ? "active" : index + 1 < wizard.step ? "complete" : "", [el("b", "", index + 1 < wizard.step ? "✓" : String(index + 1)), el("span", "", text)])));
  const content = $("wizard-content"); content.replaceChildren();
  const actions = $("wizard-actions"); actions.replaceChildren();
  if (wizard.step > 1) actions.append(button("← Back", "button subtle left-action", () => { wizard.step--; $("wizard-error").textContent = ""; renderWizard(); }, wizardBusy));
  else actions.append(button("Custom project", "text-button left-action", () => { $("worker-dialog").close(); openCustomProject(); }, wizardBusy));
  if (wizard.step === 1) {
    content.append(el("h3", "wizard-section-title", "Connect your game library"), el("p", "wizard-description", "Use a RomM Client API Token so games can find and download the game files you choose."));
    const url = input("wizard-romm-url", "url", wizard.url, "https://romm.example.com"); url.autocomplete = "url"; url.addEventListener("input", () => { wizard.url = url.value; }); field(content, "RomM server URL", url);
    const token = input("wizard-romm-token", "password", wizard.token, rommReady() ? "Saved token · leave blank to keep" : "Paste a RomM token"); token.autocomplete = "new-password"; token.addEventListener("input", () => { wizard.token = token.value; }); field(content, "Client API token", token, "A token with library read and download permission is enough. It is saved only on the Portkeeper server.");
    if (rommReady()) content.append(statusBadge("watching", "A token is already saved"));
    actions.append(button(wizardBusy ? "Connecting…" : "Save & connect →", "button primary", connectWizard, wizardBusy));
  }
  if (wizard.step === 2) {
    content.append(el("h3", "wizard-section-title", "Choose a verified project"), el("p", "wizard-description", "A profile contains reviewed download, ROM matching, and packaging instructions for a specific release."));
    const list = el("div", "choice-list");
    if (!profiles.length) list.append(el("p", "detail-empty", "Loading verified profiles…"));
    profiles.forEach((profile) => {
      const radio = input(`profile-${profile.id}`, "radio", profile.id); radio.name = "wizard-profile"; radio.checked = wizard.profile === profile.id;
      radio.addEventListener("change", () => { wizard.profile = profile.id; wizard.name = profile.name || "Ship of Harkinian"; wizard.targets = profileTargets(profile); wizard.rom = null; wizard.files = []; wizard.inspection = null; wizard.candidate = null; renderWizard(); });
      const versions = profile.supported_versions || (profile.tested_release ? [profile.tested_release] : profile.version ? [profile.version] : []);
      const text = el("div", "", [el("h3", "", profile.name || profile.id), el("p", "", profile.description || "Build a package with your own compatible game files."), statusBadge("ready", `Verified${versions.length ? ` for ${versions.join(", ")}` : " profile"}`), el("p", "", profileTargets(profile).map((target) => profileTargetLabel(profile, target)).join(" · "))]);
      list.append(el("label", "choice-card", [radio, text]));
    });
    content.append(list, el("p", "quiet-note", "Future unsupported releases are still preserved. Packaging pauses for recipe review instead of assuming the new release works."), button("Use a custom GitHub project instead →", "text-button", () => { $("worker-dialog").close(); openCustomProject(); }));
    actions.append(button("Choose game files →", "button primary", () => { if (!wizardProfile()) { $("wizard-error").textContent = "Choose a verified profile first."; return; } wizard.step = 3; $("wizard-error").textContent = ""; renderWizard(); }, wizardBusy || !profiles.length));
  }
  if (wizard.step === 3) {
    content.append(el("h3", "wizard-section-title", "Match your own ROM"), el("p", "wizard-description", "Find a game in RomM, then verify the actual ROM inside its archive. Matching uses the ROM hash and byte order."));
    const query = input("wizard-search", "search", wizard.query, "Search your RomM library"); query.setAttribute("aria-label", "Search your RomM library"); query.addEventListener("input", () => { wizard.query = query.value; }); query.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); searchRoms(); } });
    content.append(el("div", "search-row", [query, button(wizardBusy ? "Searching…" : "Search", "button secondary", searchRoms, wizardBusy)]));
    if (wizard.rom) {
      content.append(el("div", "selected-rom", [el("span", "check-icon", "✓"), el("div", "", [el("h3", "", romTitle(wizard.rom)), el("p", "", romPlatform(wizard.rom))]), button("Change", "text-button", () => { wizard.rom = null; wizard.inspection = null; wizard.candidate = null; renderWizard(); }, wizardBusy)]));
      if (wizard.files.length) {
        const select = el("select"); select.id = "wizard-rom-file"; select.disabled = wizardBusy;
        wizard.files.forEach((file) => { const name = typeof file === "string" ? file : file.name || file.filename || file.file_name || file.file_name_no_tags; if (!name) return; const option = el("option", "", `${name}${file.size ? ` · ${bytes(file.size)}` : ""}`); option.value = name; select.append(option); });
        select.value = wizard.filename || select.options[0]?.value || "";
        if (!wizard.filename) wizard.filename = select.value;
        select.addEventListener("change", () => { wizard.filename = select.value; wizard.inspection = null; wizard.candidate = null; renderWizard(); });
        field(content, "File to verify", select);
        content.append(el("div", "inline-actions", button(wizardBusy ? "Verifying ROM…" : "Download & verify ROM", "button secondary", inspectRom, wizardBusy || !wizard.filename)));
      } else content.append(el("p", "detail-empty", wizardBusy ? "Loading game files…" : "This entry returned no downloadable files. Choose another RomM entry."));
      if (wizard.inspection) renderCandidates(content);
    } else if (wizard.roms.length) {
      const list = el("div", "rom-results");
      wizard.roms.forEach((rom) => list.append(button(el("span", "", [el("span", "rom-result-title", romTitle(rom)), el("span", "rom-result-detail", romPlatform(rom))]), "rom-result", () => selectRom(rom), wizardBusy)));
      content.append(list);
    } else if (wizard.searched) content.append(el("p", "detail-empty", "No matching games found. Try a shorter title."));
    if (wizardBusy) content.append(el("p", "working-note", [el("span", "spinner"), wizard.workMessage || "Connecting to RomM…"]));
    actions.append(button("Choose devices →", "button primary", () => { if (!wizard.candidate?.supported) { $("wizard-error").textContent = "Verify and select a supported ROM revision first."; return; } wizard.step = 4; $("wizard-error").textContent = ""; renderWizard(); }, wizardBusy || !wizard.candidate?.supported));
  }
  if (wizard.step === 4) {
    content.append(el("h3", "wizard-section-title", "Where do you want to play?"), el("p", "wizard-description", "Choose packages to keep ready. Steam Deck uses the Linux package; other handheld platforms need their own reviewed recipe."));
    const devices = el("fieldset"); devices.append(el("legend", "", "Supported devices"));
    const choices = el("div", "target-options");
    profileTargets(wizardProfile()).forEach((target) => { const check = input(`wizard-target-${target}`, "checkbox", target); check.checked = wizard.targets.includes(target); check.addEventListener("change", () => { wizard.targets = [...choices.querySelectorAll("input:checked")].map((node) => node.value); }); choices.append(el("label", "", [check, profileTargetLabel(wizardProfile(), target)])); }); devices.append(choices); content.append(devices);
    if (wizardProfile()?.unsupported_devices?.length) content.append(el("p", "field-help", `This profile does not support ${wizardProfile().unsupported_devices.join(", ")}.`));
    const name = input("wizard-project-name", "text", wizard.name); name.maxLength = 100; name.addEventListener("input", () => { wizard.name = name.value; }); field(content, "Game name", name);
    const minutes = input("wizard-poll-minutes", "number", wizard.pollMinutes); minutes.min = "1"; minutes.max = "10080"; minutes.addEventListener("input", () => { wizard.pollMinutes = Number(minutes.value); }); field(content, "Check for updates every (minutes)", minutes);
    const auto = input("wizard-auto-build", "checkbox"); auto.checked = wizard.autoBuild; auto.addEventListener("change", () => { wizard.autoBuild = auto.checked; publish.disabled = !auto.checked; if (!auto.checked) { wizard.autoPublish = false; publish.checked = false; } }); content.append(el("label", "checkbox-line", [auto, "Build automatically after a compatible update"]));
    const publish = input("wizard-auto-publish", "checkbox"); publish.checked = wizard.autoPublish; publish.disabled = !wizard.autoBuild; publish.addEventListener("change", () => { wizard.autoPublish = publish.checked; }); content.append(el("label", "checkbox-line", [publish, "Copy successful packages into the local library automatically"]));
    content.append(el("p", "field-help", "RomM API upload is planned for a later update. Original ROMs stay unchanged."));
    actions.append(button("Review test build →", "button primary", () => { if (!wizard.targets.length) { $("wizard-error").textContent = "Choose at least one device."; return; } if (!wizard.name.trim()) { $("wizard-error").textContent = "Give the game a name."; return; } if (!Number.isInteger(wizard.pollMinutes) || wizard.pollMinutes < 1 || wizard.pollMinutes > 10080) { $("wizard-error").textContent = "Use an interval from 1 to 10,080 minutes."; return; } wizard.step = 5; $("wizard-error").textContent = ""; renderWizard(); }, wizardBusy));
  }
  if (wizard.step === 5) {
    content.append(el("h3", "wizard-section-title", "Let’s make the first package"), el("p", "wizard-description", "Portkeeper will preserve the upstream release, reuse your verified ROM download, extract the game assets, and archive a ZIP for each device."));
    const revision = wizard.candidate?.revision || wizard.candidate?.name || "Supported ROM revision";
    content.append(el("div", "review-grid", [meta("Game", wizard.name), meta("Verified project", wizardProfile()?.name || wizard.profile), meta("Game revision", revision, true), meta("Game file", wizard.candidate?.member || wizard.filename, true), meta("Packages", wizard.targets.map((target) => targetLabels[target] || target).join(" · "), true), meta("Update schedule", `${wizard.pollMinutes} minutes`), meta("Future builds", wizard.autoBuild ? "Automatic" : "Manual")]));
    content.append(el("p", "info-note", "A successful build earns the “Packaged” label. You can record a gameplay confirmation after you try the exact package."));
    const disabled = state.runner?.profile_builds_enabled === false;
    if (disabled) content.append(el("p", "quiet-note", "The verified profile runner is disabled. Start Portkeeper with --enable-profile-builds to enable the test build."));
    actions.append(button(wizardBusy ? "Creating game…" : "Create game & test build →", "button primary", createWizardWorker, wizardBusy || disabled));
  }
}
function romTitle(rom) { return rom.name || rom.title || rom.fs_name_no_tags || rom.fs_name || `ROM ${rom.id}`; }
function romPlatform(rom) { const platform = rom.platform_name || (typeof rom.platform === "string" ? rom.platform : rom.platform?.name || rom.platform?.slug) || rom.platform_slug || ""; return [platform, rom.fs_size || rom.size ? bytes(rom.fs_size || rom.size) : ""].filter(Boolean).join(" · ") || "RomM library entry"; }
async function connectWizard() {
  if (wizardBusy) return;
  wizardBusy = true; $("wizard-error").textContent = ""; renderWizard();
  try { await saveRomMConnection(wizard.url, wizard.token); wizard.token = ""; await refresh(); wizard.step = 2; }
  catch (error) { wizard.token = ""; $("wizard-error").textContent = error.message; await refresh(); }
  finally { wizardBusy = false; renderWizard(); }
}
async function saveRomMConnection(url, token) {
  let parsed;
  try { parsed = new URL(String(url).trim()); } catch (_) { throw new Error("Enter your complete RomM server URL."); }
  if (!["https:", "http:"].includes(parsed.protocol)) throw new Error("Use an HTTP or HTTPS server URL.");
  if (!String(token || "").trim() && !rommReady()) throw new Error("Paste a RomM Client API Token to connect.");
  const body = { url: parsed.href.replace(/\/$/, "") };
  if (String(token || "").trim()) body.token = String(token).trim();
  await api("/api/romm/config", { method: "POST", body });
  await api("/api/romm/test", { method: "POST", body: {} });
}
async function searchRoms() {
  if (wizardBusy || !wizard) return;
  const serial = wizardSerial; wizardBusy = true; wizard.workMessage = "Searching your RomM library…"; $("wizard-error").textContent = ""; renderWizard();
  try { const result = await api(`/api/romm/search?q=${encodeURIComponent(wizard.query.trim())}`); if (serial !== wizardSerial) return; wizard.roms = result.roms || result.items || result.results || (Array.isArray(result) ? result : []); wizard.searched = true; }
  catch (error) { if (serial === wizardSerial) $("wizard-error").textContent = error.message; }
  finally { if (serial === wizardSerial) { wizardBusy = false; renderWizard(); } }
}
async function selectRom(rom) {
  if (wizardBusy || !wizard) return;
  const serial = wizardSerial; wizard.rom = rom; wizard.files = []; wizard.filename = null; wizard.inspection = null; wizard.candidate = null; wizardBusy = true; wizard.workMessage = "Loading this game’s files…"; $("wizard-error").textContent = ""; renderWizard();
  try {
    const result = await api(`/api/romm/roms/${encodeURIComponent(rom.id)}`); if (serial !== wizardSerial) return;
    const detail = result.rom || result;
    wizard.rom = { ...rom, ...detail };
    wizard.files = result.files || detail.files || detail.rom_files || [];
    if (!Array.isArray(wizard.files)) wizard.files = Object.values(wizard.files);
    if (!wizard.files.length && (detail.fs_name || detail.filename)) wizard.files = [{ name: detail.fs_name || detail.filename, size: detail.fs_size || detail.size }];
    const first = wizard.files[0]; wizard.filename = typeof first === "string" ? first : first?.name || first?.filename || first?.file_name || null;
  } catch (error) { if (serial === wizardSerial) $("wizard-error").textContent = error.message; }
  finally { if (serial === wizardSerial) { wizardBusy = false; renderWizard(); } }
}
async function inspectRom() {
  if (wizardBusy || !wizard?.rom || !wizard.filename) return;
  const serial = wizardSerial; wizardBusy = true; wizard.workMessage = "Downloading and verifying the ROM. A verified copy is cached for future builds…"; $("wizard-error").textContent = ""; renderWizard();
  try { const result = await api("/api/romm/inspect", { method: "POST", body: { rom_id: wizard.rom.id, filename: wizard.filename, profile: wizard.profile } }); if (serial !== wizardSerial) return; wizard.inspection = result; const supported = (result.candidates || []).filter((candidate) => candidate.supported); wizard.candidate = supported.length === 1 ? supported[0] : null; }
  catch (error) { if (serial === wizardSerial) $("wizard-error").textContent = error.message; }
  finally { if (serial === wizardSerial) { wizardBusy = false; renderWizard(); } }
}
function renderCandidates(parent) {
  const candidates = wizard.inspection.candidates || [];
  if (!candidates.length) { parent.append(el("p", "form-error", "No N64 ROM was found in this file. Choose a different file or game entry.")); return; }
  parent.append(el("h3", "", "Verified archive contents"));
  const list = el("div", "candidate-list");
  candidates.forEach((candidate, index) => {
    const radio = input(`candidate-${index}`, "radio", String(index)); radio.name = "wizard-candidate"; radio.checked = wizard.candidate === candidate; radio.disabled = !candidate.supported || wizardBusy;
    radio.addEventListener("change", () => { wizard.candidate = candidate; $("wizard-error").textContent = ""; renderWizard(); });
    const name = candidate.revision || candidate.name || candidate.member || "ROM candidate";
    list.append(el("div", "candidate-card", [el("label", "checkbox-line", [radio, name]), el("p", "candidate-hash", candidate.member || wizard.filename), el("p", "candidate-hash", `SHA-1 ${candidate.sha1 || "Not available"}`), statusBadge(candidate.supported ? "ready" : "needs_attention", candidate.supported ? "Supported ROM hash" : "Unsupported ROM hash"), ...(!candidate.supported ? [el("p", "field-help", candidate.reason || "This ROM revision is not supported by the selected profile.")] : [])]));
  });
  parent.append(list, el("p", "field-help", wizard.inspection.cached || wizard.inspection.cache_hit || wizard.inspection.cache_reused ? "Reused the verified cached download." : "The download is cached. Future builds verify and reuse it."));
}
async function createWizardWorker() {
  if (wizardBusy || !wizard?.candidate?.supported) return;
  wizardBusy = true; $("wizard-error").textContent = ""; renderWizard();
  try {
    const body = { name: wizard.name.trim(), profile: wizard.profile, targets: wizard.targets, poll_minutes: wizard.pollMinutes, auto_build: wizard.autoBuild, auto_publish: wizard.autoPublish, rom_source: { rom_id: wizard.rom.id, filename: wizard.filename, member: wizard.candidate.member, sha1: wizard.candidate.sha1, cache_key: wizard.inspection.cache_key } };
    const result = await api("/api/projects", { method: "POST", body }); const project = result.project || result;
    if (!project.id) throw new Error("The game was created, but the server returned no game ID.");
    await refresh(); $("worker-dialog").close(); navigateProject(project.id); toast("Game created. Starting the first test build.");
    await testBuild(project.id);
  } catch (error) { $("wizard-error").textContent = error.message; }
  finally { wizardBusy = false; renderWizard(); }
}

function openCustomProject() { $("project-form").reset(); $("project-form-error").textContent = ""; $("project-dialog").showModal(); $("project-name").focus(); }
function normalizedRepo(value) {
  let repo = value.trim().replace(/\/$/, "");
  if (/^https?:\/\//i.test(repo)) { const url = new URL(repo); if (url.hostname !== "github.com") throw new Error("Use a GitHub repository URL or owner/repository."); repo = url.pathname.replace(/^\//, "").replace(/\/$/, ""); }
  repo = repo.replace(/\.git$/, "");
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) throw new Error("Repository must be owner/repository, for example HarbourMasters/Shipwright.");
  return repo;
}
async function submitProject(event) {
  event.preventDefault(); const submit = $("project-submit"); if (submit.disabled) return; $("project-form-error").textContent = "";
  try {
    const targets = [...document.querySelectorAll('#project-form input[name="target"]:checked')].map((node) => node.value); if (!targets.length) throw new Error("Select at least one target.");
    const body = { name: $("project-name").value.trim(), repo: normalizedRepo($("project-repo").value), targets, poll_minutes: Number($("poll-minutes").value), auto_build: false, auto_publish: false, recipe: recipes.find((item) => item.id === $("initial-recipe").value)?.recipe || captureRecipe };
    submit.disabled = true; submit.textContent = "Adding…";
    const result = await api("/api/projects", { method: "POST", body }); $("project-dialog").close(); await refresh(); const project = result.project || result;
    if (project.id) navigateProject(project.id); toast("Project added. Check an update to create its first preservation checkpoint.");
  } catch (error) { $("project-form-error").textContent = error.message; }
  finally { submit.disabled = false; submit.textContent = "Add project"; }
}
function openRecipe(id) {
  const project = projectById(id); if (!project) return; recipeProjectId = id;
  $("recipe-heading").textContent = `${project.name} recipe`; $("recipe-dialog").querySelector(".dialog-intro").textContent = project.profile ? "This profile’s reviewed build instructions are fixed. You can edit the automation settings below." : "Saved recipes run repeatably. Device-specific overrides define each package’s outputs."; $("recipe-json").value = JSON.stringify(project.recipe || captureRecipe, null, 2); $("recipe-json").readOnly = Boolean(project.profile); $("recipe-template").disabled = Boolean(project.profile); $("apply-template").disabled = Boolean(project.profile); $("recipe-auto-build").checked = Boolean(project.auto_build); $("recipe-auto-publish").checked = Boolean(project.auto_publish); syncAutoPublish(); $("recipe-template").value = ""; $("recipe-form-error").textContent = ""; $("recipe-dialog").showModal();
}
function syncAutoPublish() { const enabled = $("recipe-auto-build").checked; $("recipe-auto-publish").disabled = !enabled; if (!enabled) $("recipe-auto-publish").checked = false; }
async function submitRecipe(event) {
  event.preventDefault(); const submit = $("recipe-submit"); if (submit.disabled) return; $("recipe-form-error").textContent = "";
  try {
    let recipe; try { recipe = JSON.parse($("recipe-json").value); } catch (error) { throw new Error(`The recipe must be valid JSON. ${error.message}`); }
    if (!recipe || Array.isArray(recipe) || typeof recipe !== "object") throw new Error("The recipe must be a JSON object.");
    submit.disabled = true; submit.textContent = "Saving…";
    await api(`/api/projects/${encodeURIComponent(recipeProjectId)}`, { method: "PATCH", body: { recipe, auto_build: $("recipe-auto-build").checked, auto_publish: $("recipe-auto-publish").checked } }); $("recipe-dialog").close(); toast("Recipe and automation saved."); await refresh();
  } catch (error) { $("recipe-form-error").textContent = error.message; }
  finally { submit.disabled = false; submit.textContent = "Save recipe"; }
}
function openValidation(artifact) { validationArtifactId = artifact.id; $("validation-form").reset(); $("validation-package").textContent = `${artifact.version} · ${targetLabels[artifact.target] || artifact.target}`; $("validation-error").textContent = ""; $("validation-dialog").showModal(); }
async function submitValidation(event) {
  event.preventDefault(); const submit = $("validation-submit"); if (submit.disabled || !$("validation-confirm").checked) return; submit.disabled = true; $("validation-error").textContent = "";
  try { await api(`/api/artifacts/${encodeURIComponent(validationArtifactId)}/validation`, { method: "POST", body: { level: "user_confirmed_playable", notes: $("validation-notes").value.trim(), evidence: { user_confirmed: true, source: "package review" } } }); $("validation-dialog").close(); toast("Your gameplay confirmation was recorded."); await refresh(); }
  catch (error) { $("validation-error").textContent = error.message; }
  finally { submit.disabled = false; }
}
async function previewRetention(id) {
  const saved = projectById(id); const draft = retentionDrafts.get(id) || { mode: saved?.retention_keep_last || saved?.retention?.keep_last ? "last" : "all", count: saved?.retention_keep_last || saved?.retention?.keep_last || 3 }; const keepLast = draft.mode === "all" ? null : Number(draft.count);
  if (keepLast !== null && (!Number.isInteger(keepLast) || keepLast < 1 || keepLast > 1000)) { toast("Keep at least one version per target, up to 1,000.", true); return; }
  const result = await runAction(`retention-${id}`, `/api/projects/${encodeURIComponent(id)}/retention`, { keep_last: keepLast, apply: false }, ""); if (!result) return;
  retentionReview = { projectId: id, keepLast, result }; $("retention-error").textContent = ""; renderRetentionReview(); $("retention-dialog").showModal();
}
function retentionCandidates(result) { const plan = result.plan || result; const items = plan.remove || plan.delete || plan.candidates || plan.delete_candidates || plan.to_delete || []; return Array.isArray(items) ? items : []; }
function renderRetentionReview() {
  const { result, keepLast } = retentionReview; const plan = result.plan || result; const candidates = retentionCandidates(result);
  const protectedItems = plan.protected || plan.kept || []; const removeCount = candidates.length || plan.remove_count || plan.delete_count || 0;
  const size = plan.reclaim_bytes || plan.bytes_reclaimed || candidates.reduce((total, item) => total + (Number(item.size) || 0), 0);
  const content = $("retention-content"); content.replaceChildren(el("p", "dialog-intro", keepLast === null ? "Keep every archived version. No packages will be removed." : `Keep the latest ${keepLast} versions per target, along with all protected packages.`), el("div", "retention-summary", [el("div", "", [el("strong", "", String(removeCount)), el("span", "", "packages to remove")]), el("div", "", [el("strong", "", bytes(size)), el("span", "", "space to reclaim")])]));
  if (candidates.length) content.append(el("ul", "retention-files", candidates.map((item) => el("li", "", typeof item === "string" ? item : `${item.version || item.filename || item.id} · ${targetLabels[item.target] || item.target || "package"} · ${bytes(item.size)}`))));
  if (protectedItems.length || plan.protected_count) content.append(el("p", "compact-note", `${plan.protected_count || protectedItems.length} packages kept by this retention policy and version protections.`));
  content.append(el("p", "quiet-note", "Applying this saves the policy and cleans up the listed packages now. Future successful builds follow the same policy. Pinned, known-good, and current library versions remain protected."));
  $("retention-actions").replaceChildren(button("Cancel", "button secondary", () => $("retention-dialog").close()), button(removeCount ? "Save policy & clean up" : "Save retention policy", "button primary", applyRetention));
}
async function applyRetention() {
  const actions = [...$("retention-actions").querySelectorAll("button")]; actions.forEach((node) => { node.disabled = true; }); $("retention-error").textContent = "";
  try { await api(`/api/projects/${encodeURIComponent(retentionReview.projectId)}/retention`, { method: "POST", body: { keep_last: retentionReview.keepLast, apply: true } }); $("retention-dialog").close(); toast("Retention policy applied. Protected versions were kept."); await refresh(); }
  catch (error) { $("retention-error").textContent = error.message; }
  finally { actions.forEach((node) => { node.disabled = false; }); }
}
async function draftProposal(id) {
  const prompt = assistantPromptDrafts.get(id)?.trim(); if (!prompt) { toast("Describe the recipe change you want first.", true); return; }
  const result = await runAction(`proposal-${id}`, `/api/projects/${encodeURIComponent(id)}/recipe-proposal`, { instruction: prompt }, ""); if (!result) return;
  recipeProposal = result.proposal || result; $("proposal-error").textContent = ""; $("proposal-confirm").checked = false; $("proposal-apply").disabled = true;
  const data = recipeProposal.recipe || recipeProposal.proposed_recipe;
  const content = $("proposal-content"); content.replaceChildren(el("p", "dialog-intro", recipeProposal.summary || recipeProposal.explanation || "Review the complete proposed recipe. It will replace the saved recipe only after you apply it."), el("pre", "proposal-json", data ? JSON.stringify(data, null, 2) : recipeProposal.text || recipeProposal.content || "No recipe was returned."));
  const validation = recipeProposal.validation || {}; const errors = recipeProposal.errors || validation.errors || [];
  const valid = recipeProposal.valid !== false && validation.valid !== false && !errors.length && Boolean(data) && Boolean(recipeProposal.id);
  recipeProposal._reviewValid = valid;
  content.append(statusBadge(valid ? "ready" : "failed", valid ? "Recipe schema validated" : "Proposal needs changes"));
  if (errors.length) content.append(el("p", "form-error", errors.map((error) => typeof error === "string" ? error : JSON.stringify(error)).join("\n")));
  if (recipeProposal.warnings?.length || validation.warnings?.length) content.append(el("p", "compact-note", (recipeProposal.warnings || validation.warnings).join("\n")));
  content.append(el("p", "quiet-note", "Schema validation checks the recipe structure. It does not prove the proposed build commands will work.")); $("proposal-dialog").showModal();
}
async function applyProposal() {
  if (!recipeProposal?._reviewValid || !$("proposal-confirm").checked) return; $("proposal-apply").disabled = true; $("proposal-error").textContent = "";
  try { await api(`/api/proposals/${encodeURIComponent(recipeProposal.id)}/apply`, { method: "POST", body: {} }); $("proposal-dialog").close(); toast("Reviewed recipe applied. Future builds use it."); await refresh(); }
  catch (error) { $("proposal-error").textContent = error.message; $("proposal-apply").disabled = false; }
}

async function submitRomM(event) {
  event.preventDefault(); if (busy.has("romm-settings")) return; busy.add("romm-settings"); $("romm-save").disabled = true; $("romm-save").textContent = "Connecting…"; $("romm-form-result").textContent = "";
  try { await saveRomMConnection($("romm-url").value, $("romm-token").value); $("romm-url").dataset.dirty = ""; $("romm-form-result").classList.remove("error"); $("romm-form-result").textContent = "Connection verified. You can now select game files in game setup."; }
  catch (error) { $("romm-form-result").classList.add("error"); $("romm-form-result").textContent = error.message; }
  finally { $("romm-token").value = ""; busy.delete("romm-settings"); $("romm-save").disabled = false; $("romm-save").textContent = "Save & test connection"; await refresh(); }
}
async function testSavedRomM() {
  $("romm-form-result").textContent = "Testing saved connection…";
  const result = await runAction("romm-settings", "/api/romm/test", {}, "");
  if (result) { $("romm-form-result").classList.remove("error"); $("romm-form-result").textContent = `Connection verified${result.version ? ` · RomM ${result.version}` : ""}${result.user?.username ? ` · ${result.user.username}` : ""}.`; }
  else { $("romm-form-result").classList.add("error"); $("romm-form-result").textContent = "The connection test failed. See the error notification for details."; }
}
async function clearRomMToken() {
  const result = await runAction("romm-settings", "/api/romm/config", { url: rommConfig().url, clear_token: true }, "Saved RomM token removed.");
  if (result) { $("romm-token").value = ""; $("romm-form-result").textContent = "Token removed. Workers will need a new token to download game files."; }
}
async function submitAssistant(event) {
  event.preventDefault(); const submit = $("assistant-save"); if (submit.disabled) return; submit.disabled = true; $("assistant-result").textContent = "";
  try { await api("/api/assistant/config", { method: "POST", body: { endpoint: $("assistant-endpoint").value.trim(), model: $("assistant-model").value.trim() } }); $("assistant-result").classList.remove("error"); $("assistant-result").textContent = "Assistant configured. Draft a proposal from a game’s Advanced tab."; [$("assistant-endpoint"), $("assistant-model")].forEach((node) => { node.dataset.dirty = ""; }); await refresh(); }
  catch (error) { $("assistant-result").classList.add("error"); $("assistant-result").textContent = error.message; }
  finally { submit.disabled = false; }
}
function renderToolchain() {
  const saved = state.settings?.toolchain || {};
  const content = $("toolchain-current"); content.replaceChildren();
  if (saved.material) content.append(el("div", "detail-facts", [meta("Preserved toolchain", saved.identity || "Registered archive", true), meta("Archive", saved.material, true)]), statusBadge("ready", "Toolchain archived"));
  else content.append(el("p", "compact-note", "No toolchain archive is registered yet. Source and dependency preservation remains available."));
  const docker = Boolean(state.runner?.docker_available);
  $("toolchain-archive").disabled = !docker || busy.has("toolchain-image");
  $("toolchain-archive").title = docker ? "Archive the specified image from the local Docker daemon." : "A working local Docker daemon is required. You can register an existing archive below.";
}
async function submitToolchainImage(event) {
  event.preventDefault(); if (busy.has("toolchain-image")) return; busy.add("toolchain-image"); $("toolchain-archive").disabled = true; $("toolchain-image-result").classList.remove("error"); $("toolchain-image-result").textContent = "Archiving the installed image…";
  try { await api("/api/toolchains/archive", { method: "POST", body: { image: $("toolchain-image").value.trim() } }); $("toolchain-image-result").textContent = "Toolchain archived. Future preservation exports will include it."; preservationCache.clear(); await refresh(); }
  catch (error) { $("toolchain-image-result").classList.add("error"); $("toolchain-image-result").textContent = error.message; }
  finally { busy.delete("toolchain-image"); renderToolchain(); }
}
async function submitToolchainMaterial(event) {
  event.preventDefault(); const submit = $("toolchain-register"); if (submit.disabled) return; submit.disabled = true; $("toolchain-material-result").textContent = "";
  try { await api("/api/toolchains/config", { method: "POST", body: { material: $("toolchain-material").value.trim(), identity: $("toolchain-identity").value.trim() } }); $("toolchain-material-result").classList.remove("error"); $("toolchain-material-result").textContent = "Archive registered. Future preservation exports will include it."; preservationCache.clear(); await refresh(); }
  catch (error) { $("toolchain-material-result").classList.add("error"); $("toolchain-material-result").textContent = error.message; }
  finally { submit.disabled = false; }
}
async function toggleDesktopNotifications() {
  if (!("Notification" in window)) return;
  if (desktopNotifications && Notification.permission === "granted") { desktopNotifications = false; }
  else {
    const permission = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    desktopNotifications = permission === "granted";
  }
  savePreference("portkeeper.desktop-notifications", String(desktopNotifications)); renderNotificationPreference();
}
async function runDemo() {
  const node = $("run-demo"); node.disabled = true; node.textContent = "Starting demo…";
  try { const result = await api("/api/demo", { method: "POST", body: {} }); toast("Synthetic demo started. Sample packages will appear in your library."); await refresh(); if (result.project?.id) navigateProject(result.project.id); else location.hash = "library"; }
  catch (error) { toast(error.message, true); }
  finally { node.disabled = false; node.textContent = "Run synthetic demo →"; }
}
async function loadCatalog() {
  const results = await Promise.allSettled([api("/api/profiles"), api("/api/recipes")]);
  if (results[0].status === "fulfilled") profiles = results[0].value.profiles || [];
  if (results[1].status === "fulfilled") {
    recipes = results[1].value.recipes || [];
    recipes.forEach((item) => {
      const option = el("option", "", item.name); option.value = item.id; $("recipe-template").append(option);
      if (item.recipe?.mode === "capture" && item.id !== "capture") { const initial = el("option", "", item.name); initial.value = item.id; $("initial-recipe").append(initial); }
    });
  }
  if (wizard && $("worker-dialog").open) renderWizard();
}
function openInbox() { renderNotifications($("inbox-content")); $("inbox-dialog").showModal(); }

$("add-project").addEventListener("click", openWizard);
$("manual-project").addEventListener("click", openCustomProject);
$("refresh").addEventListener("click", () => refresh(true));
$("metadata-form").addEventListener("submit", saveMetadata);
$("project-form").addEventListener("submit", submitProject);
$("recipe-form").addEventListener("submit", submitRecipe);
$("recipe-auto-build").addEventListener("change", syncAutoPublish);
$("validation-form").addEventListener("submit", submitValidation);
$("romm-form").addEventListener("submit", submitRomM);
$("romm-test").addEventListener("click", testSavedRomM);
$("romm-clear").addEventListener("click", clearRomMToken);
$("assistant-form").addEventListener("submit", submitAssistant);
$("toolchain-image-form").addEventListener("submit", submitToolchainImage);
$("toolchain-material-form").addEventListener("submit", submitToolchainMaterial);
$("browser-notifications").addEventListener("click", toggleDesktopNotifications);
$("run-demo").addEventListener("click", runDemo);
$("open-inbox").addEventListener("click", openInbox);
$("settings-open-inbox").addEventListener("click", openInbox);
$("proposal-confirm").addEventListener("change", () => { $("proposal-apply").disabled = !$("proposal-confirm").checked || !recipeProposal?._reviewValid; });
$("proposal-apply").addEventListener("click", applyProposal);
["romm-url", "assistant-endpoint", "assistant-model"].forEach((id) => $(id).addEventListener("input", () => { $(id).dataset.dirty = "true"; }));
[ ["jobs-tab", "jobs"], ["packages-tab", "packages"], ["notifications-tab", "notifications"] ].forEach(([id, view]) => $(id).addEventListener("click", () => { activityView = view; renderActivity(); }));
document.querySelectorAll("[data-settings-tab]").forEach((node) => node.addEventListener("click", () => { settingsView = node.dataset.settingsTab; renderSettingsTabs(); }));
document.querySelectorAll("[data-project-tab]").forEach((node) => node.addEventListener("click", () => navigateProject(route.projectId, node.dataset.projectTab)));
$("apply-template").addEventListener("click", () => {
  const template = recipes.find((item) => item.id === $("recipe-template").value);
  if (!template) { $("recipe-form-error").textContent = "Choose a recipe template first."; return; }
  $("recipe-json").value = JSON.stringify(template.recipe, null, 2); $("recipe-form-error").textContent = "";
});
document.querySelectorAll("[data-close]").forEach((node) => node.addEventListener("click", () => $(node.dataset.close).close()));
document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => {
  if (event.target !== dialog) return;
  const bounds = dialog.getBoundingClientRect();
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
}));
document.querySelectorAll('[role="tablist"]').forEach((list) => list.addEventListener("keydown", (event) => {
  const tabs = [...list.querySelectorAll('[role="tab"]')]; const index = tabs.indexOf(document.activeElement); if (index < 0) return;
  let next = null;
  if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
  if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
  if (event.key === "Home") next = 0;
  if (event.key === "End") next = tabs.length - 1;
  if (next !== null) { event.preventDefault(); tabs[next].focus(); tabs[next].click(); }
}));
window.addEventListener("hashchange", () => { renderRoute(); lastRender = null; render(); window.scrollTo(0, 0); });
[ ["library-search", "input"], ["library-platform", "change"], ["library-stage", "change"] ].forEach(([id, event]) => $(id).addEventListener(event, () => { librarySearch = $("library-search").value.trim(); libraryPlatform = $("library-platform").value; libraryStage = $("library-stage").value; renderLibrary(); }));
renderRoute(); render();
Promise.allSettled([refresh(), loadCatalog()]);
setInterval(() => { if (!document.hidden) refresh(); }, 3000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
