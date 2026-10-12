"use strict";

const $ = (id) => document.getElementById(id);
const projectPageElement = $("project-page");
const projectPageHome = projectPageElement.parentElement;
const targetLabels = { linux: "Linux / Steam Deck", windows: "Windows", handheld: "Handheld", capture: "Source" };
const targetGlyphs = { linux: "◈", windows: "▦", handheld: "▤", capture: "◇" };
const captureRecipe = { version: 1, mode: "capture", payload: { kind: "source" }, notes: "Preserve tagged upstream source. Add a tested recipe to build a package." };
const activeStatuses = new Set(["queued", "running", "checking", "capturing", "building", "preserving", "downloading_rom", "extracting", "packaging_linux", "packaging_windows", "preserving_source", "preserving_assets", "capturing_dependencies", "building_extractor", "preparing", "validating", "publishing", "verifying_inputs", "preparing_inputs", "archiving"]);
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
let libraryLayout = ["workspace", "collection", "list"].includes(readPreference("portkeeper.library-layout")) ? readPreference("portkeeper.library-layout") : "workspace";
let workspaceProjectId = readPreference("portkeeper.selected-game");
const rommImports = new Map();
const rommImportsLoading = new Set();
let settingsView = "romm";
let route = { page: "library", projectId: null, tab: "overview" };
let selectedJobId = null;
let recipeProjectId = null;
let editorOriginal = null, editorView = "settings", editorDirty = false;
const automationDrafts = new Map();
const inputDrafts = new Map();
const projectDisclosures = new Map();
const platformDetections = new Map();
const platformSelections = new Map();
let metadataProjectId = null;
let coverProjectId = null;
let coverChoice = null;
let validationArtifactId = null;
let reviewContext = null;
let activityGameFilter = "", activityTypeFilter = "";
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
function savePreference(key, value) { try { localStorage.setItem(key, value); return true; } catch (_) { return false; } }
function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined && content !== null) (Array.isArray(content) ? content : [content]).forEach((child) => node.append(child instanceof Node ? child : document.createTextNode(String(child))));
  return node;
}
function disclosure(key, className, content) {
  const node = el("details", className, content);
  node.dataset.disclosureKey = key;
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
    idle: "Waiting", ready: "Packaged", watching: "Watching", captured: "Saved only (not built)", archived: "Saved only (not built)", succeeded: "Complete", success: "Complete", completed: "Complete", published: "In local library", built: "Packaged", unchanged: "Up to date",
    failed: "Needs attention", error: "Needs attention", needs_attention: "Needs attention", blocked: "Needs attention", needs_rom: "Needs a ROM", missing_inputs: "Needs game files", capture_only: "Saved only (not built)", unsupported_version: "New version needs review",
    queued: "Queued", running: "Working", checking: "Checking upstream", capturing: "Preserving source", preserving: "Preserving source", downloading_rom: "Downloading ROM", extracting: "Extracting game assets", packaging_linux: "Packaging Linux", packaging_windows: "Packaging Windows", building: "Building package", preserving_source: "Preserving source", preserving_assets: "Preserving releases", capturing_dependencies: "Preserving dependencies", building_extractor: "Building extractor", preparing: "Preparing inputs", validating: "Validating package", complete: "Complete", publishing: "Publishing to RomM", verifying_inputs: "Checking game files", preparing_inputs: "Preparing game files", archiving: "Writing package"
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
function projectCover(project, className) {
  const metadata = project.metadata || {};
  const custom = metadata.cover_policy === "custom" && metadata.url_cover;
  if (!metadata.romm_rom_id && !custom) return null;
  const image = el("img", className);
  const version = custom ? `art=${encodeURIComponent(metadata.url_cover.slice(-80))}` : `rom=${encodeURIComponent(metadata.romm_rom_id)}`;
  image.src = `/api/projects/${encodeURIComponent(project.id)}/cover?${version}`;
  image.alt = `${project.name} cover art`;
  image.loading = "lazy"; image.decoding = "async";
  image.addEventListener("error", () => image.remove(), {once: true});
  return image;
}
function projectSource(project) { return project.demo ? el("span", "repo-link", "Synthetic sample files") : repoLink(project.repo); }
function projectById(id) { return state.projects.find((project) => String(project.id) === String(id)); }
function relatedProjects(project) {
  const identity = project?.metadata;
  if (!identity?.filename_prefix) return project ? [project] : [];
  return state.projects.filter((item) => item.metadata?.filename_prefix?.toLowerCase() === identity.filename_prefix.toLowerCase() && item.metadata?.igdb_id === identity.igdb_id);
}
function libraryGames() {
  const seen = new Set(), games = [];
  state.projects.forEach((project) => {
    if (seen.has(project.id)) return;
    const related = relatedProjects(project); related.forEach((item) => seen.add(item.id));
    games.push(related.find((item) => !/ windows$/i.test(item.name)) || project);
  });
  return games.sort((a,b) => a.name.localeCompare(b.name));
}
function projectArtifacts(id) {
  const ids = new Set(relatedProjects(projectById(id)).map((project) => String(project.id)));
  return newestFirst(state.artifacts.filter((artifact) => ids.has(String(artifact.project_id))));
}
function importKey(project) { return project.metadata?.filename_prefix || project.library_basename || project.id; }
function importedPackages(project) { return rommImports.get(importKey(project))?.items || []; }
function hasDownloads(project) { return projectArtifacts(project.id).length > 0 || importedPackages(project).length > 0; }
function reviewFor(artifact, rom) {
  const signature = artifact?.sha256 || rom?.package_signature;
  return (state.playtests || []).find((test) => test.package.signature === signature && (artifact ? test.package.artifact_id === artifact.id : test.package.romm_rom_id === rom?.id)) || (!artifact ? rom?.review : null);
}
function testStatus(artifact, rom) {
  const review = reviewFor(artifact, rom);
  if (review) return review.result;
  const level = artifact ? validationLevel(artifact) : "packaged";
  return {launch_checked: "starts", user_confirmed_playable: "played", failed: "failed"}[level] || "untested";
}
function testStatusBadge(artifact, rom) {
  const result = testStatus(artifact, rom);
  const wine = reviewFor(artifact, rom)?.environment === "wine_proton";
  return el("span", `validation-label ${result === "played" ? "confirmed" : result === "starts" ? "launch" : result === "failed" ? "failed" : ""}`, testResultLabels[result] + (wine ? " under Wine/Proton" : ""));
}
function downloadDetails(artifact, rom) {
  const details = artifact?.manifest || rom?.package_details || {};
  const review = reviewFor(artifact, rom);
  const version = artifact?.version || details.version;
  const revision = details.package_revision;
  const revisionLabel = typeof revision === "string" && /^nvidiafix\d+$/.test(revision) ? `Portkeeper fix ${revision.match(/\d+$/)[0]}` : Number.isInteger(revision) ? `Package revision ${revision}${details.definition ? ` · definition ${details.definition.id} r${details.definition.revision}` : ""}` : revision;
  const section = el("div", "download-details");
  section.append(el("p", "package-revision", `${version ? `Upstream ${version}` : "Upstream version: Unknown"} · ${revisionLabel || "Package revision: Unknown"}`));
  const text = value => typeof value === "string" ? value : Array.isArray(value) ? value.filter(item => typeof item === "string").join("; ") : "";
  const summary = rom?.summary || "";
  const requirements = text(details.requirements) || text(details.dependencies) || summary.split(/(?<=[.!?])\s+/).filter(sentence => /\b(requires?|needs?|writable|Windows 10|Python 3|Vulkan 1)\b/i.test(sentence)).join(" ");
  const changes = text(details.changes) || (revision ? text(details.validation) || review?.notes : "");
  const issues = text(details.known_issues) || text(details.limitations) || (review?.result === "failed" ? review.notes : "");
  [["What changed", changes], ["Requirements", requirements], ["Known issues", issues]].forEach(([label, value]) => section.append(el("p", "download-detail-line", [el("strong", "", `${label}: `), value || "Unknown — not recorded for this download."])));
  const fileNames = [...Object.keys(details.files || {}), ...(details.recipe?.expected_files || [])];
  const launcher = fileNames.find(name => typeof name === "string" && /(?:^|\/)(?:launch[^/]*\.(?:sh|cmd|bat))$/i.test(name));
  const launch = text(details.launch_instructions) || text(details.launch?.instructions) || (launcher ? `Extract the complete ZIP to a writable folder, then run ${launcher}. See the included README for saves, settings and dependencies.` : "") || (rom?.summary || "").split(/(?<=[.!?])\s+/).filter(sentence => /\b(extract|run|saves|settings)\b/i.test(sentence)).join(" ");
  section.append(el("h4", "", "Launch instructions"), el("p", "download-detail-line", launch || "Not recorded for this download. Consult the README included in the archive or the upstream documentation before launching."));
  const result = testStatus(artifact, rom);
  section.append(el("p", "compact-note", {starts:"Startup checked; gameplay has not been confirmed.",played:"Gameplay confirmed for this exact download. See the device and testing notes.",failed:"A test reported a problem. See the testing notes before downloading.",untested:"No startup or gameplay check has been recorded for this exact download."}[result]));
  return section;
}
function gameNeedsAttention(project) {
  return relatedProjects(project).some((item) => item.last_error || ["needs_rom", "missing_inputs", "needs_attention", "failed", "blocked", "unsupported_version"].includes(item.status)) || gameTargets(project).some((target) => validationLevel(projectArtifacts(project.id).find((artifact) => artifact.target === target) || {}) === "failed") || importedPackages(project).some((rom) => { const result = reviewFor(null, rom)?.result; return result === "failed" || (!result && /\bfailed\b/i.test(rom.summary || "")); });
}
function gameTargets(project) { return [...new Set([...relatedProjects(project).flatMap((item) => item.targets || []), ...projectArtifacts(project.id).map((item) => item.target), ...importedPackages(project).map((rom) => rom.platform_slug === "win" ? "windows" : "linux")])]; }
async function loadImports(project) {
  const key = importKey(project);
  if (!rommReady() || !project.metadata?.filename_prefix || rommImportsLoading.has(key) || Date.now() - (rommImports.get(key)?.checkedAt || 0) < 60000) return;
  rommImportsLoading.add(key);
  try {
    const response = await api(`/api/projects/${project.id}/imports`);
    rommImports.set(key, {checkedAt: Date.now(), items: response.items || []});
  } catch (_) { rommImports.set(key, {checkedAt: Date.now(), items: rommImports.get(key)?.items || [], error: true}); }
  finally { rommImportsLoading.delete(key); lastRender = null; render(); }
}
function gameAvailability(project) {
  if (projectActiveJob(project)) return "Working…";
  if (gameNeedsAttention(project)) return "Needs attention";
  if (hasDownloads(project)) {
    const available = new Set([...projectArtifacts(project.id).map(item => item.target), ...importedPackages(project).map(item => item.platform_slug === "win" ? "windows" : "linux")]);
    return (project.targets || []).every(target => available.has(target)) ? "Downloads available" : "Some downloads available";
  }
  if (project.releases?.some(release => release.status === "needs_review")) return "New version needs review";
  if (project.packaging?.runner === "operations" && project.packaging.inputs?.some(item => item.required !== false && !project.input_bindings?.[item.id])) return "Needs your game files";
  if (rommImportsLoading.has(importKey(project))) return "Checking RomM…";
  if (rommImports.get(importKey(project))?.error) return "RomM check unavailable";
  return project.recipe?.mode === "capture" ? "Needs packaging steps" : "No downloads yet";
}
function projectJobs(id) { return newestFirst(state.jobs.filter((job) => String(job.project_id) === String(id))); }
function jobActive(job) { return activeStatuses.has(job.status) && !job.finished_at; }
function projectActiveJob(project) { const jobs = projectJobs(project.id).filter(jobActive); return jobs.find((job) => job.status !== "queued") || jobs[0]; }
function jobStage(job) { return job.status === "failed" ? "failed" : job.stage || job.status; }
function projectStatus(project) { const active = projectActiveJob(project); return active ? jobStage(active) : project.status; }
function validationLevel(artifact) { return artifact.validation_level || artifact.validation?.level || (typeof artifact.validation === "string" ? artifact.validation : "packaged"); }
const testResultLabels = {untested: "Not tested", starts: "Starts successfully", played: "Played successfully", failed: "Doesn’t work"};
function validationLabel(artifact) {
  const level = validationLevel(artifact);
  const label = level === "user_confirmed_playable" ? "Played successfully" : level === "launch_checked" ? "Starts successfully" : level === "failed" ? "Doesn’t work" : "Not tested";
  const node = el("span", `validation-label ${level === "user_confirmed_playable" ? "confirmed" : level === "launch_checked" ? "launch" : level === "failed" ? "failed" : ""}`, label);
  node.title = level === "failed" ? "A check found that this exact download does not work. See the recorded notes." : level === "user_confirmed_playable" ? "The user confirmed this exact package is playable." : level === "launch_checked" ? "A launch check was recorded. Gameplay is not confirmed." : "Package files were validated. Launch and gameplay are not confirmed.";
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
  const names = { ready: "Package ready", build_failed: "Build needs attention", needs_input: "Game input required", upstream_unavailable: "Upstream unavailable", needs_review: "Release needs review" };
  const title = notification.title || names[notification.kind] || "Game update";
  return notification.project_name ? `${notification.project_name} · ${title}` : title;
}
function notificationUnread(notification) { return notification.unread !== undefined ? Boolean(notification.unread) : !(notification.read || notification.read_at); }
function knownGood(artifact, project) { return Boolean(artifact.known_good || project?.known_good_artifact_id === artifact.id || project?.known_good?.[artifact.target] === artifact.id || project?.known_good_artifacts?.[artifact.target] === artifact.id); }

function parseRoute() {
  const parts = location.hash.slice(1).split("/").map((part) => { try { return decodeURIComponent(part); } catch (_) { return part; } });
  if (parts[0] === "project" && parts[1]) return { page: "project", projectId: parts[1], tab: parts[2] === "versions" ? "activity" : ["overview", "activity", "advanced"].includes(parts[2]) ? parts[2] : "overview" };
  return { page: ["activity", "settings"].includes(parts[0]) ? parts[0] : parts[0] === "storage" ? "settings" : "library", projectId: null, tab: "overview" };
}
function navigateProject(id, tab = "overview", jobId = null) { if (tab === "versions") tab = "activity"; selectedJobId = jobId; location.hash = `project/${encodeURIComponent(id)}/${tab}`; if (route.projectId === id && route.tab === tab) renderProject(); }
function navigateSettings(tab = "romm") { settingsView = tab; location.hash = "settings"; renderSettingsTabs(); }
function renderRoute() {
  route = parseRoute();
  if (location.hash === "#storage") settingsView = "storage";
  ["library", "activity", "settings", "project"].forEach((page) => { $(`${page}-page`).hidden = route.page !== page; });
  document.querySelectorAll(".nav-link").forEach((link) => { const active = link.dataset.page === (route.page === "project" ? "library" : route.page); link.classList.toggle("active", active); if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current"); });
  $("breadcrumb").textContent = route.page === "project" ? "GAME DETAILS" : { library: "YOUR LIBRARY", activity: "WORKSPACE ACTIVITY", settings: "WORKSPACE SETTINGS" }[route.page];
  renderSettingsTabs();
  if (route.page === "project") renderProject();
  if (route.page === "activity") renderActivity();
}
function restoreProjectPage() { if (projectPageElement.parentElement !== projectPageHome) projectPageHome.insertBefore(projectPageElement, projectPageHome.querySelector("footer")); }
function renderLibrary() {
  restoreProjectPage();
  const games = libraryGames();
  $("library-page").dataset.layout = libraryLayout;
  games.forEach((project) => { void loadImports(project); });
  $("summary-projects").textContent = games.length;
  $("summary-artifacts").textContent = games.reduce((count, project) => count + new Set([...projectArtifacts(project.id).map((artifact) => artifact.target), ...importedPackages(project).map((rom) => rom.platform_slug === "win" ? "windows" : "linux")]).size, 0);
  const attention = games.filter(gameNeedsAttention).length;
  $("summary-attention").textContent = attention;
  $("attention-summary").classList.toggle("attention-active", attention > 0);
  const grid = $("project-grid"); grid.replaceChildren(); grid.className = `project-grid layout-${libraryLayout}`;
  const workspaceActive = libraryLayout === "workspace" && ["library", "project"].includes(route.page);
  if (workspaceActive) $("library-page").hidden = false;
  $("library-results").textContent = "0 games";
  if (!state.projects.length) {
    const empty = emptyState("▦", "A home for your next playthrough", "Connect your RomM library, choose a game, and preserve its updates.");
    empty.append(el("div", "onboarding-steps", [el("span", "", [el("b", "", "1"), "Connect RomM"]), el("span", "", [el("b", "", "2"), "Select a game"]), el("span", "", [el("b", "", "3"), "Test a build"])]));
    empty.append(button("Set up your first game →", "button primary", openAddGame), button("Explore with a synthetic demo", "text-button", () => navigateSettings("demo")));
    grid.append(empty); return;
  }
  const matches = games.filter((project) => {
    const hasPackages = hasDownloads(project);
    const needsAttention = gameNeedsAttention(project);
    return `${project.name} ${project.repo || ""}`.toLowerCase().includes(librarySearch.toLowerCase()) && (!libraryPlatform || gameTargets(project).includes(libraryPlatform)) && (!libraryStage || (libraryStage === "packages" ? hasPackages : libraryStage === "source" ? project.recipe?.mode === "capture" : needsAttention));
  });
  $("library-results").textContent = `${matches.length} of ${games.length} games`;
  if (!matches.length) grid.append(emptyState("⌕", "No games match", "Try another search or choose All platforms and All games."));
  if (workspaceActive && matches.length) {
    const chosen = projectById(route.projectId) || projectById(workspaceProjectId) || matches.find((item) => item.profile) || matches[0];
    const selected = matches.find((item) => relatedProjects(item).some((member) => member.id === chosen.id)) || matches[0];
    workspaceProjectId = selected.id;
    if (!route.projectId) route.projectId = selected.id;
    const sidebar = el("nav", "workspace-game-list"); sidebar.setAttribute("aria-label", "Choose a game");
    matches.forEach((project) => {
      const choose = button("", "workspace-game-button", () => { workspaceProjectId = project.id; savePreference("portkeeper.selected-game", project.id); navigateProject(project.id); });
      choose.setAttribute("aria-pressed", String(project.id === selected.id));
      const cover = projectCover(project, "workspace-game-cover");
      if (cover) choose.append(cover);
      choose.append(el("span", "", [el("strong", "", project.name), el("small", "", gameAvailability(project))]));
      sidebar.append(choose);
    });
    const main = el("div", "workspace-game-main", projectPageElement);
    grid.append(sidebar, main); projectPageElement.hidden = false; renderProject(); return;
  }
  projectPageElement.hidden = route.page !== "project";
  matches.forEach((project) => {
    const artifacts = projectArtifacts(project.id);
    const newest = artifacts[0];
    const good = artifacts.find((artifact) => knownGood(artifact, project));
    const title = button(project.name, "project-name", () => navigateProject(project.id));
    const avatar = el("div", "project-avatar", String(project.name || "P").split(/\s+/).map((word) => word[0]).slice(0, 2).join("").toUpperCase());
    const cover = projectCover(project, "project-cover-thumb");
    if (cover) {
      const initials = avatar.textContent;
      avatar.replaceChildren(cover); avatar.classList.add("has-cover");
      cover.addEventListener("error", () => { avatar.classList.remove("has-cover"); avatar.textContent = initials; }, {once: true});
    }
    const top = el("div", "project-card-top", [el("div", "project-title-row", [avatar, el("div", "project-title-block", [title, projectSource(project)])]), el("div", "project-state-row", [statusBadge(gameNeedsAttention(project) ? "needs_attention" : hasDownloads(project) ? "ready" : "capture_only", gameAvailability(project)), ...(project.demo ? [el("span", "validation-label", "Demo")] : [])]), el("div", "project-targets", gameTargets(project).map(targetPill)), el("div", "project-meta", [meta("Latest build", newest?.version || "See RomM downloads"), meta("In RomM", `${importedPackages(project).length} platform entries`)])]);
    const active = projectActiveJob(project);
    const description = hasDownloads(project) ? "Open this game for platform downloads and testing notes." : project.recipe?.mode === "capture" ? "Update tracking is enabled. Automatic build setup is needed." : "Open this game to prepare its first download.";
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
  $("jobs-count").textContent = unifiedActivity().length; $("artifacts-count").textContent = state.artifacts.length; $("notifications-count").textContent = state.notifications.filter(notificationUnread).length;
  const content = $("activity-content"); content.setAttribute("aria-labelledby", tabs[activityView].id);
  $("activity-filters").hidden = activityView !== "jobs";
  const filter = $("activity-game-filter"); filter.replaceChildren(el("option", "", "All games")); filter.firstChild.value = "";
  libraryGames().forEach((game) => { const option = el("option", "", game.name); option.value = game.id; filter.append(option); }); filter.value = activityGameFilter;
  if (activityView === "jobs") {
    const family = activityGameFilter ? new Set(relatedProjects(projectById(activityGameFilter)).map((project) => project.id)) : null;
    const events = unifiedActivity().filter((event) => (!family || family.has(event.project_id)) && (!activityTypeFilter || event.kind === activityTypeFilter));
    content.replaceChildren(events.length ? el("div", "activity-timeline", events.map(activityEvent)) : emptyState("◷", "No activity matches", "Imports, tests, builds and update checks will appear here. Try All games and All activity."));
  }
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
  const advanced = ["storage", "catalog", "demo"].includes(settingsView);
  $("settings-advanced").hidden = !advanced;
  document.querySelectorAll("[data-settings-tab]").forEach(node => {
    const selected = node.dataset.settingsTab === settingsView;
    node.classList.toggle("selected", selected); node.setAttribute("aria-selected", String(selected));
    node.tabIndex = selected ? 0 : -1; $(`settings-${node.dataset.settingsTab}`).hidden = !selected;
  });
  $("settings-advanced-tab").classList.toggle("selected", advanced);
  $("settings-advanced-tab").setAttribute("aria-selected", String(advanced));
  $("settings-advanced-tab").tabIndex = advanced ? 0 : -1;
}
function renderSettings() {
  document.querySelectorAll('[name="library-layout"]').forEach((node) => { node.checked = node.value === libraryLayout; });
  const config = rommConfig();
  const connected = config.connected || config.last_test?.ok || config.last_test?.success;
  $("romm-connection-status").replaceWith(Object.assign(statusBadge(connected ? "ready" : rommReady() ? "watching" : "needs_rom", connected ? "Connection verified" : rommReady() ? "Token saved" : "Not connected"), { id: "romm-connection-status" }));
  $("romm-token-help").textContent = config.token_configured || config.has_token ? "A token is saved. Leave this field empty to keep it, or paste a replacement. Tokens never appear in recipes or browser state." : "Create a RomM Client API Token with permission to read your library and download ROMs. Your token stays on this server.";
  $("romm-token").placeholder = config.token_configured || config.has_token ? "Saved token · leave blank to keep" : "Paste a RomM token";
  $("romm-test").disabled = !rommReady() || busy.has("romm-settings"); $("romm-clear").disabled = !(config.token_configured || config.has_token) || busy.has("romm-settings");
  const fingerprint = JSON.stringify([config.url, assistantSettings()]);
  if (fingerprint !== formConfigFingerprint) {
    if (!$('romm-url').dataset.dirty) $("romm-url").value = config.url || "";
    renderAssistantSettings();
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
  renderNotificationPreference(); renderToolchain(); renderCatalog();
}
function renderNotificationPreference() {
  const supported = "Notification" in window;
  const allowed = supported && Notification.permission === "granted";
  $("browser-notifications").textContent = desktopNotifications && allowed ? "Disable desktop notifications" : "Enable desktop notifications";
  $("browser-notifications").disabled = !supported || (supported && Notification.permission === "denied");
  $("browser-notification-state").textContent = !supported ? "This browser does not support desktop notifications. The in-app inbox is available." : Notification.permission === "denied" ? "Browser permission is blocked. Enable it in your browser’s site settings if you want desktop notifications." : desktopNotifications && allowed ? "Enabled for this browser while Portkeeper is open." : "Disabled for this browser. Your inbox still records updates.";
}
function render() {
  const fingerprint = JSON.stringify([state, [...busy], route, activityView, settingsView, libraryLayout]);
  if (fingerprint === lastRender) return;
  lastRender = fingerprint;
  // Moving the game pane while rebuilding the library also drops browser focus.
  const content = $("project-content"), active = document.activeElement;
  const focusedDisclosure = content.contains(active) && active.tagName === "SUMMARY" ? {view: content.dataset.viewKey, key: active.parentElement.dataset.disclosureKey} : null;
  $("nav-project-count").textContent = libraryGames().length;
  const unread = state.notifications.filter(notificationUnread).length;
  [$("nav-notification-count"), $("inbox-count")].forEach((node) => { node.textContent = unread; node.hidden = !unread; });
  renderLibrary(); renderActivity(); renderSettings();
  if (route.page === "project") renderProject();
  if ($("inbox-dialog").open) renderNotifications($("inbox-content"));
  if (focusedDisclosure && content.dataset.viewKey === focusedDisclosure.view) {
    [...content.querySelectorAll("details[data-disclosure-key]")].find(node => node.dataset.disclosureKey === focusedDisclosure.key)?.querySelector("summary")?.focus({preventScroll: true});
  }
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
  if (manual) { rommImports.clear(); lastRender = null; }
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
function publishArtifact(id) { return runAction(`publish-${id}`, `/api/artifacts/${encodeURIComponent(id)}/publish`, {}, "This version is now in your local library folder. That folder is separate from RomM unless it is RomM's own platform folder."); }
function pinArtifact(artifact) { return runAction(`pin-${artifact.id}`, `/api/artifacts/${encodeURIComponent(artifact.id)}/pin`, { pinned: !artifact.pinned }, artifact.pinned ? "Version unpinned." : "Version pinned. Retention will keep it."); }
function setKnownGood(id) { return runAction(`good-${id}`, `/api/artifacts/${encodeURIComponent(id)}/known-good`, {}, "Known-good version saved for this target."); }
function markRead(id) { return runAction(`read-${id}`, `/api/notifications/${encodeURIComponent(id)}/read`, {}, ""); }
function markAllRead() { return runAction("read-all", "/api/notifications/read-all", {}, "Inbox marked read."); }

function renderProject() {
  const project = projectById(route.projectId);
  const heading = $("project-heading"), content = $("project-content");
  // Polling replaces this page. Keep reading state scoped to the previous game
  // and tab, including sections the user deliberately collapsed.
  const previousView = content.dataset.viewKey;
  if (previousView) content.querySelectorAll("details[data-disclosure-key]").forEach(node => {
    projectDisclosures.set(`${previousView}:${node.dataset.disclosureKey}`, node.open);
  });
  const focusedSummary = content.contains(document.activeElement) && document.activeElement.tagName === "SUMMARY" ? document.activeElement.parentElement.dataset.disclosureKey : null;
  const viewKey = `${route.projectId}/${route.tab}`;
  content.dataset.viewKey = viewKey;
  const focused = content.contains(document.activeElement) && document.activeElement.id ? { id: document.activeElement.id, start: document.activeElement.selectionStart, end: document.activeElement.selectionEnd } : null;
  const openJobs = new Set([...content.querySelectorAll("details[open][data-job-id]")].map((node) => node.dataset.jobId));
  heading.replaceChildren(); content.replaceChildren();
  document.querySelectorAll("[data-project-tab]").forEach((node) => { const selected = node.dataset.projectTab === route.tab; node.classList.toggle("selected", selected); node.setAttribute("aria-selected", String(selected)); node.tabIndex = selected ? 0 : -1; });
  content.setAttribute("aria-labelledby", `project-${route.tab}-tab`);
  if (!project) { heading.append(el("div", "", [el("h1", "", "Game unavailable"), el("p", "", "Return to the library to choose a game.")])); return; }
  const title = el("div", "project-title-detail", [el("h1", "", project.name), projectSource(project), el("div", "project-targets", gameTargets(project).map(targetPill))]);
  const cover = projectCover(project, "project-hero-cover");
  if (cover) title.prepend(cover);
  const working = Boolean(projectActiveJob(project)) || busy.has(`check-${project.id}`) || busy.has(`build-${project.id}`);
  const actions = el("div", "project-header-actions");
  if (!project.demo) actions.append(button("Check for a new version", "button secondary small", () => checkProject(project.id), working));
  if (project.profile) actions.append(button("Test build", "button primary small", () => testBuild(project.id), working));
  if (project.recipe?.mode !== "capture" && !project.demo) actions.append(button("Build packages…", project.profile ? "button secondary small" : "button primary small", () => openBuild(project.id), working || !project.capture_id));
  else if (project.demo) actions.append(button("Build package", "button primary small", () => buildProject(project.id), working || !project.capture_id));
  heading.append(title, statusBadge(gameNeedsAttention(project) ? "needs_attention" : hasDownloads(project) ? "ready" : "capture_only", gameAvailability(project)));
  if (route.tab === "overview") renderOverview(project, content);
  if (route.tab === "activity") {
    content.append(el("section", "detail-card", [el("h2", "", "Updates & builds"), el("p", "field-help", "Check releases, build packages and follow progress here. Uploading and testing are separate from building."), actions]));
    if (project.packaging) content.append(automationCard(project), releasesCard(project));
    renderBuildDiagnostics(project, content, openJobs); renderVersions(project, content); renderProjectActivity(project, content);
  }
  if (route.tab === "advanced") renderAdvanced(project, content, openJobs);
  content.querySelectorAll("details[data-disclosure-key]").forEach(node => {
    const key = `${viewKey}:${node.dataset.disclosureKey}`;
    if (projectDisclosures.has(key)) node.open = projectDisclosures.get(key);
    if (previousView === viewKey && focusedSummary === node.dataset.disclosureKey) node.querySelector("summary")?.focus({preventScroll: true});
  });
  if (focused && $(focused.id)) { $(focused.id).focus({ preventScroll: true }); if (focused.start !== null && focused.start !== undefined && typeof $(focused.id).setSelectionRange === "function") $(focused.id).setSelectionRange(focused.start, focused.end); }
}
function renderOverview(project, content) {
  const grid = el("div", "detail-grid"), left = el("div"), right = el("div");
  const artifacts = projectArtifacts(project.id), latest = artifacts[0];
  const current = artifacts.filter((artifact) => artifact.published);
  const packageCard = el("section", "detail-card downloads-card", el("h2", "", "Choose your platform"));
  const imports = importedPackages(project);
  const targets = gameTargets(project);
  targets.forEach((target) => {
    const artifact = artifacts.find((item) => item.target === target);
    const roms = imports.filter((rom) => rom.platform_slug === (target === "windows" ? "win" : target === "linux" ? "linux" : target));
    const row = el("div", "download-platform-row");
    const info = el("div", "download-platform-info", [el("h3", "", targetLabels[target] || target)]);
    if (artifact) info.append(el("p", "field-help", artifact.version), testStatusBadge(artifact, null));
    else if (roms.length) info.append(el("p", "field-help", "Imported into RomM"), testStatusBadge(null, roms[0]));
    else info.append(el("p", "field-help", rommImportsLoading.has(importKey(project)) ? "Checking your RomM library…" : rommImports.get(importKey(project))?.error ? "RomM could not be checked. Refresh to retry." : project.recipe?.mode === "capture" ? "This game tracks upstream releases, but no platform download is recorded. Build setup is needed; check RomM for an existing import." : "No platform download is recorded. A completed build or matching RomM import will appear here."));
    const actions = el("div", "inline-actions");
    if (artifact) actions.append(artifactDownload(artifact, "button primary small", "Download ZIP"));
    else roms.forEach((rom) => (rom.files || []).filter(file => file.id && file.name?.toLowerCase().endsWith(".zip")).forEach((file) => {
      const download = el("a", "button primary small", "Download ZIP");
      download.href = `/api/projects/${project.id}/imports/${rom.id}/files/${file.id}/download`;
      download.setAttribute("download", ""); download.title = file.name; actions.append(download);
    }));
    roms.forEach((rom) => { const link = el("a", "button secondary small", "Open in RomM"); link.href = `${rommConfig().url}/rom/${rom.id}`; link.target = "_blank"; link.rel = "noopener noreferrer"; actions.append(link); });
    if (artifact || roms.length) actions.append(button("Record test result", "button secondary small", () => openReview(project, artifact, artifact ? null : roms[0])));
    const latestReview = reviewFor(artifact, artifact ? null : roms[0]);
    if (latestReview?.device) info.append(el("span", "field-help", `${latestReview.device} · ${dateLabel(latestReview.created_at)}`));
    if (latestReview?.notes) info.append(el("p", "review-notes", latestReview.notes));
    row.append(info, actions); packageCard.append(row);
    if (artifact || roms.length) packageCard.append(downloadDetails(artifact, artifact ? null : roms[0]));
    if (!artifact && roms.some((rom) => rom.summary)) {
      const notes = disclosure(`import-notes:${target}`, "import-testing-notes", [el("summary", "", `${targetLabels[target] || target}: testing & launch notes`), ...roms.filter((rom) => rom.summary).map((rom) => el("p", "field-help", rom.summary))]);
      packageCard.append(notes);
    }
  });
  packageCard.append(el("p", "compact-note", "Download availability does not confirm gameplay. A launch check confirms startup only."));
  left.append(nextStepCard(project), packageCard);
  right.append(button("Change cover art", "button secondary", () => openCoverSearch(project.id)));
  grid.append(left, right); content.append(grid);
}
function progressSteps(job, project) {
  const stage = job.stage || job.status;
  if (Array.isArray(job.checkpoints) && job.checkpoints.length) {
    const names = { checking: "Check upstream", preserving_source: "Preserve source", preserving_assets: "Preserve release assets", capturing_dependencies: "Preserve pinned dependencies", building_extractor: "Build reviewed extractor", downloading_rom: "Verify game input", extracting: "Extract game assets", packaging_linux: "Package Linux / Steam Deck", packaging_windows: "Package Windows", preparing: "Prepare inputs", verifying_inputs: "Check game files", preparing_inputs: "Copy game files", archiving: "Write package", publishing: "Send to RomM", validating: "Validate package", complete: job.target === "capture" ? "Save preservation checkpoint" : "Archive package" };
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
  if (!artifacts.length) { content.append(emptyState("▱", "No packages yet", "This page lists archives built by Portkeeper. Existing RomM imports are shown under Play; they do not create a build archive here.")); return; }
  const list = el("div", "version-list");
  artifacts.forEach((artifact) => {
    const labels = [validationLabel(artifact)];
    if (artifact.pinned) labels.push(statusBadge("watching", "Pinned"));
    if (knownGood(artifact, project)) labels.push(statusBadge("ready", "Known good"));
    if (artifact.published) labels.push(statusBadge("published", "In local library folder"));
    if (rommBadge(artifact)) labels.push(rommBadge(artifact));
    if (artifact.release_review === "compatible") labels.push(statusBadge("watching", "New release · untested"));
    const actions = el("div", "version-actions", [artifactDownload(artifact), button(artifact.pinned ? "Unpin" : "Pin version", "table-action", () => pinArtifact(artifact), busy.has(`pin-${artifact.id}`))]);
    if (!knownGood(artifact, project)) {
      const goodButton = button("Set known good", "table-action", () => setKnownGood(artifact.id), busy.has(`good-${artifact.id}`) || !["launch_checked", "user_confirmed_playable"].includes(validationLevel(artifact)));
      if (goodButton.disabled) goodButton.title = "Record a launch check or confirm gameplay before selecting a known-good package.";
      actions.append(goodButton);
    }
    if (!artifact.demo && !project.demo) actions.append(button("Record test result", "table-action", () => openValidation(artifact)));
    if (!artifact.demo && !["in_romm", "uploading"].includes(artifact.romm?.status)) actions.append(button(artifact.romm?.status === "failed" ? "Retry RomM upload" : "Send to RomM", "table-action", () => publishToRomM(artifact.id), busy.has(`romm-${artifact.id}`) || !rommReady()));
    if (artifact.romm?.rom_id && rommConfig().url) { const link = el("a", "table-action", "Open in RomM"); link.href = `${rommConfig().url}/rom/${artifact.romm.rom_id}`; link.target = "_blank"; link.rel = "noopener noreferrer"; actions.append(link); }
    if (!artifact.published) actions.append(disclosure(`archive-transfer:${artifact.id}`, "advanced-copy", [el("summary", "table-action", "Advanced transfer"), button("Copy to local library folder", "table-action", () => publishArtifact(artifact.id), busy.has(`publish-${artifact.id}`))]));
    list.append(el("article", "version-card", [el("div", "version-heading", [el("h3", "", artifact.version || "Archived version"), targetPill(artifact.target)]), el("div", "version-labels", labels), el("div", "version-meta", [el("span", "", bytes(artifact.size)), el("span", "", dateLabel(artifact.created_at))]), ...(artifact.validation?.notes ? [el("p", "version-description", artifact.validation.notes)] : !artifact.demo && !project.demo && validationLevel(artifact) === "packaged" ? [el("p", "version-description", "Confirm gameplay after trying this package to select it as a known-good version.")] : []), actions]));
  });
  content.append(el("p", "section-intro", "Publish to RomM uploads a package through RomM's API; older versions stay in RomM as separate files. Copying an older version to the local library folder rolls that folder back. Pinned and known-good packages are protected from retention cleanup."), el("div", "inline-actions", button("Retention settings →", "text-button", () => navigateProject(project.id))), list);
  list.style.marginTop = "22px";
}
function unifiedActivity() {
  const events = [...(state.activity || [])];
  state.jobs.forEach((job) => events.push({...job, kind: "build", message: job.error || job.message || (job.target === "capture" ? "Upstream release and update check" : "Portable build"), title: job.target === "capture" ? "Update check" : "Build", result: job.status}));
  state.artifacts.forEach((artifact) => (artifact.validation_history || []).filter((test) => !test.evidence?.manual_check).forEach((test, index) => events.push({id:`setup-${artifact.id}-${index}`, kind:"test", project_id:artifact.project_id, target:artifact.target, title:"Previous setup check", result:{launch_checked:"starts",user_confirmed_playable:"played",failed:"failed"}[test.level] || "untested", created_at:test.recorded_at, notes:test.notes, origin:"setup_check", package_name:artifact.filename})));
  return newestFirst(events);
}
function activityEvent(event) {
  const project = projectById(event.project_id);
  const title = event.kind === "import" ? "Found in RomM" : event.kind === "test" ? (event.origin === "setup_check" ? "Setup test result" : "Manual test result") : event.title;
  const result = event.kind === "test" ? el("span", `validation-label ${event.result === "failed" ? "failed" : event.result === "played" ? "confirmed" : ""}`, testResultLabels[event.result] + (event.environment === "wine_proton" ? " under Wine/Proton" : "")) : event.kind === "import" ? statusBadge("published", "In RomM") : statusBadge(event.result);
  const header = el("div", "activity-event-heading", [el("h3", "", title), result]);
  const body = el("div", "activity-event-body", [header, el("p", "activity-event-game", [project?.name || event.project_name || "Game", " · ", targetLabels[event.target] || "Update check"]), el("p", "field-help", event.notes || event.message || ""), el("span", "activity-event-date", `${event.device ? event.device + " · " : ""}${dateLabel(event.created_at)}`)]);
  if (event.package_name) body.append(el("p", "activity-package-name", event.package_name));
  const actions = el("div", "inline-actions", button("Open game", "text-button", () => navigateProject(event.project_id)));
  if (event.kind === "build") actions.append(button("Build details", "text-button", () => navigateProject(event.project_id, "activity", event.id)));
  return el("article", "activity-event", [body, actions]);
}
function renderProjectActivity(project, content) {
  const ids = new Set(relatedProjects(project).map((item) => item.id));
  const events = unifiedActivity().filter((event) => ids.has(event.project_id));
  content.append(events.length ? el("div", "activity-timeline", events.map(activityEvent)) : emptyState("◷", "No activity recorded yet", "No import discovery, test result, build or update check has been recorded for this game. Existing RomM imports appear after the library connection is checked; Record test result adds a testing event."));
}
const runnerLabels = { operations: "Automated packaging", profile: "Reviewed build profile", recipe: "Saved recipe", capture: "Preserve only" };
const releaseLabels = { packaged: "Packaged", preserved: "Preserved only", needs_review: "Needs review", incomplete: "Capture incomplete" };
const rommLabels = { uploading: "Uploading to RomM", awaiting_scan: "Uploaded · waiting for RomM", in_romm: "In RomM", failed: "RomM upload failed" };
let buildProjectId = null;
function externalLink(label, url) {
  const node = el("a", "text-button", label);
  if (typeof url === "string" && /^https:\/\//.test(url)) { node.href = url; node.target = "_blank"; node.rel = "noopener noreferrer"; }
  return node;
}
function releaseBadge(release) {
  const tone = { packaged: "ready", preserved: "capture_only", needs_review: "needs_attention", incomplete: "needs_attention" }[release.status] || "idle";
  return statusBadge(tone, releaseLabels[release.status] || release.status);
}
function rommBadge(artifact) {
  const romm = artifact.romm || {};
  if (!romm.status) return null;
  const node = statusBadge({ uploading: "running", awaiting_scan: "watching", in_romm: "ready", failed: "failed" }[romm.status] || "idle", rommLabels[romm.status] || romm.status);
  node.title = romm.status === "failed" ? romm.error || "" : romm.note || (romm.filename ? `${romm.filename}${romm.rom_id ? " · RomM entry " + romm.rom_id : ""}` : "");
  return node;
}
function definitionInstructions(project) {
  const definition = project.packaging, view = project.packaging_view || {};
  const section = el("section", "detail-card packaging-instructions");
  section.append(el("h2", "", "Packaging instructions"), el("p", "detail-note", definition.summary || "Generated from this game's packaging definition."));
  section.append(el("div", "detail-facts", [meta("Method", runnerLabels[definition.runner] || definition.runner), meta("Definition", `${definition.id} · revision ${definition.revision}`), meta("Platforms", definition.targets.map((target) => targetLabels[target] || target).join(", "))]));
  if (view.requirements && !view.requirements.supported) section.append(el("p", "project-error", view.requirements.reason));
  if (project.catalog_update) section.append(el("div", "inline-actions", [el("span", "field-help", `Catalog revision ${project.catalog_update.revision} is available; this game uses revision ${project.catalog_update.current_revision}.`), button("Review catalog changes", "button secondary small", () => applyCatalogDefinition(project.id), busy.has(`catalog-${project.id}`))]));
  if (definition.runner === "capture") section.append(el("p", "detail-note", "Releases are only preserved. No packaging steps are defined, so Portkeeper does not produce a download. Existing RomM imports were assembled separately."));
  const list = el("div", "instruction-sections");
  const group = (heading, nodes) => list.append(el("section", "instruction-section", [el("h3", "", heading), ...nodes]));
  const rules = definition.upstream.release_rules || {};
  group("1. Upstream & release rules", [repoLink(definition.upstream.repository), ...(definition.upstream.documentation || []).map((url) => el("p", "", externalLink(url.replace(/^https:\/\//, "") + " ↗", url))),
    el("p", "field-help", `${rules.stable_only === false ? "Pre-releases are allowed" : "Stable releases only"}${rules.tag_pattern ? ` · the tag must match ${rules.tag_pattern}` : ""}`), ...(rules.notes ? [el("p", "field-help", rules.notes)] : [])]);
  const downloads = Object.entries(definition.downloads || {}).map(([key, item]) => el("p", "", [el("strong", "", `${key}${item.platform && item.platform !== "any" ? " · " + (targetLabels[item.platform] || item.platform) : ""}: `), item.repository ? `${item.repository} ${item.tag} · ${item.asset} · SHA-256 ${item.sha256}` : `the release file matching ${item.asset_pattern}`, item.required_members?.length ? el("span", "field-help", ` · must contain ${item.required_members.join(", ")}`) : "", item.description ? el("span", "field-help", ` · ${item.description}`) : ""]));
  (definition.verified_releases || []).forEach((release) => downloads.push(disclosure(`reviewed-release:${release.release}`, "diagnostics-list", [el("summary", "table-action", `Reviewed release ${release.release}: file checksums`), ...Object.entries(release.assets || {}).map(([name, hash]) => el("p", "field-help", `${name} · SHA-256 ${hash}`)), ...(release.evidence ? [el("p", "field-help", release.evidence)] : [])])));
  if (!downloads.length) downloads.push(el("p", "field-help", definition.runner === "profile" ? "The reviewed profile pins its runtime downloads; see the reviewed release checksums." : "No release files are selected."));
  group("2. Selected downloads & checksums", downloads);
  group("3. Your game files", inputsSection(project));
  group("4. Build steps", view.steps?.length ? [el("ol", "instruction-steps", view.steps.map((step) => el("li", "", step.text)))] : [el("p", "field-help", "No build steps are defined.")]);
  group("5. Platform packages & launch", [...(view.first_run ? [el("p", `first-run-note${view.first_run.mode === "asks_user" ? " warn" : ""}`, "First launch: " + view.first_run.text)] : []), ...definition.targets.map((target) => {
    const output = (definition.outputs || {})[target] || {}, launch = (definition.launch || {})[target] || {}, dependencies = (definition.dependencies || {})[target] || [];
    return el("div", "instruction-platform", [el("h4", "", targetLabels[target] || target), el("p", "field-help", `Launch: ${launch.command || "not recorded"}${launch.instructions ? " — " + launch.instructions : ""}`), el("p", "field-help", `Expected files: ${(output.expected_files || []).join(", ") || "not recorded"}`), el("p", "field-help", `Requirements: ${dependencies.join("; ") || "not recorded"}`)]);
  })]);
  group("6. Saves, new-release checks & known limitations", [el("p", "field-help", `Saves & settings: ${definition.saves || "Not recorded."}`), ...(definition.source_checks || []).map((check) => el("p", "field-help", `New-release check: ${check.description} (${check.path})`)), ...(definition.limitations || []).map((item) => el("p", "review-notes", item))]);
  if ((definition.history || []).length) group("History", definition.history.map((item) => el("p", "field-help", item)));
  section.append(list);
  return section;
}
function inputsSection(project) {
  const definition = project.packaging, bindings = project.input_bindings || {};
  if (definition.runner === "profile") return [el("p", "field-help", "Chosen and verified in the worker wizard: " + (project.rom_source?.member || project.rom_source?.filename || project.rom_source?.path || "not selected"))];
  if (!(definition.inputs || []).length) return [el("p", "field-help", definition.runner === "recipe" ? "Declared in the saved recipe's inputs." : "No game files are needed.")];
  return definition.inputs.map((item) => {
    const binding = bindings[item.id], key = `input-${project.id}`;
    const node = el("div", "instruction-input", [el("h4", "", item.label + (item.required === false ? " (optional)" : "")),
      el("p", "field-help", item.accepted?.length ? `Accepted: ${item.accepted.map((entry) => entry.label).join(", ")}${item.normalize ? " (checked after byte-order normalization)" : ""}` : "Validated after extraction; see the build steps."),
      ...(item.notes ? [el("p", "field-help", item.notes)] : []),
      el("p", "field-help", binding ? `Selected: ${binding.source === "prepared" ? "app data prepared/" : ""}${binding.path}${binding.accepted ? " · verified as " + binding.accepted : ""}` : item.required === false ? "Not selected." : "Not selected yet. Portkeeper searches your game files folder when a build starts.")]);
    const path = input(`input-path-${project.id}-${item.id}`, "text", inputDrafts.get(`${project.id}-${item.id}`) ?? binding?.path ?? "");
    path.placeholder = item.kind === "directory" ? "Folder under the app data prepared/ folder" : "Path below your game files folder, such as n64/roms/game.z64";
    path.setAttribute("aria-label", `${item.label} path`);
    node.append(el("div", "inline-actions input-binding", [path, button("Save path", "button secondary small", () => bindInput(project.id, item.id, { path: path.value.trim(), source: item.kind === "directory" ? "prepared" : "inputs" }), busy.has(key) || !path.value), button("Find automatically", "button secondary small", () => bindInput(project.id, item.id, {}), busy.has(key)), ...(binding ? [button("Clear", "text-button", () => bindInput(project.id, item.id, { clear: true }), busy.has(key))] : [])]));
    path.addEventListener("input", () => { inputDrafts.set(`${project.id}-${item.id}`, path.value); node.querySelector(".input-binding button").disabled = !path.value.trim(); });
    return node;
  });
}
async function bindInput(projectId, inputId, body) {
  const result = await runAction(`input-${projectId}`, `/api/projects/${encodeURIComponent(projectId)}/inputs`, { input: inputId, ...body }, "");
  if (!result) return;
  inputDrafts.delete(`${projectId}-${inputId}`);
  if (result.found && result.found.length !== 1) toast(`${result.message}${result.found.length ? ": " + result.found.map((item) => item.path).join(", ") : ""}`, true);
  else toast(body.clear ? "Game file selection cleared." : "Game file saved for future builds.");
}
async function applyCatalogDefinition(id) {
  try { const project = projectById(id); const definition = await api(`/api/catalog/definitions/${encodeURIComponent(project.packaging.id)}`); openRecipe(id); $("recipe-json").value = JSON.stringify(definition, null, 2); editorDirty = true; switchEditor("changes"); }
  catch(error) { toast(error.message, true); }
}
function releasesCard(project) {
  const card = el("section", "detail-card", [el("h2", "", "Releases & packaging status"), el("p", "detail-note", "Preserved means the upstream release is archived only. Packaged means Portkeeper produced a download from it. In RomM means the upload was indexed by RomM.")]);
  const releases = project.releases || [];
  if (!releases.length) { card.append(el("p", "detail-empty", "No release has been preserved yet. Select Check upstream.")); return card; }
  const rows = releases.slice(0, 10).map((release) => {
    const problems = release.review?.status === "needs_review" ? el("ul", "review-problems", release.review.problems.map((problem) => el("li", "", problem))) : release.review?.notes?.length ? el("p", "field-help", release.review.notes.join(" ")) : "";
    const status = el("div", "", [releaseBadge(release), release.packaged_targets.length ? el("span", "field-help", ` ${release.packaged_targets.map((t) => targetLabels[t] || t).join(", ")}`) : "", release.romm_targets.length ? el("span", "field-help", ` · in RomM: ${release.romm_targets.map((t) => targetLabels[t] || t).join(", ")}`) : ""]);
    const canBuild = project.packaging?.runner !== "capture" && release.complete && release.status !== "needs_review";
    return el("tr", "", [el("td", "", [el("span", "table-title", release.version || "Release"), el("span", "table-detail", `${(release.commit || "").slice(0, 12)} · ${dateLabel(release.created_at)}`)]), el("td", "", [status, problems]), el("td", "", canBuild ? button("Build…", "table-action", () => openBuild(project.id, release.capture_id)) : "")]);
  });
  card.append(table(["Release", "Status", ""], rows));
  return card;
}
function automationCard(project) {
  const card = el("section", "detail-card", [el("h2", "", "Automation"), el("p", "detail-note", "New releases are checked against this game's compatibility rules. Compatible ones can be packaged and published without you; anything unexpected is preserved and reported for review instead.")]);
  const autoBuild = input(`auto-build-${project.id}`, "checkbox"), autoPublish = input(`auto-publish-${project.id}`, "checkbox");
  const draft = automationDrafts.get(project.id) || project;
  autoBuild.checked = Boolean(draft.auto_build); autoPublish.checked = Boolean(draft.auto_publish); autoPublish.disabled = !autoBuild.checked;
  const destination = el("select"); destination.id = `auto-destination-${project.id}`; destination.setAttribute("aria-label", "Automatic publication destination");
  [["romm", "Upload to RomM"], ["library", "Copy to the local library folder only"]].forEach(([value, label]) => { const option = el("option", "", label); option.value = value; destination.append(option); });
  destination.value = draft.auto_publish_destination || "romm"; destination.disabled = !autoPublish.checked;
  autoBuild.addEventListener("change", () => { autoPublish.disabled = !autoBuild.checked; if (!autoBuild.checked) autoPublish.checked = false; destination.disabled = !autoPublish.checked; });
  autoPublish.addEventListener("change", () => { destination.disabled = !autoPublish.checked; });
  const remember = () => automationDrafts.set(project.id, {auto_build: autoBuild.checked, auto_publish: autoPublish.checked, auto_publish_destination: destination.value});
  [autoBuild, autoPublish, destination].forEach(node => node.addEventListener("change", remember));
  const capture = project.packaging?.runner === "capture" || project.recipe?.mode === "capture";
  if (capture) { autoBuild.disabled = true; autoPublish.disabled = true; destination.disabled = true; }
  card.append(el("label", "checkbox-line", [autoBuild, "Automatically build compatible new releases"]), el("label", "checkbox-line", [autoPublish, "Automatically publish successful packages"]), destination,
    el("p", "field-help", "RomM uploads use RomM's upload API and need a token with roms.write. RomM adds the file to its library after its filesystem watcher rescans; Portkeeper confirms the new entry. A local library folder copy reaches RomM only if that folder is RomM's own platform folder."),
    el("p", "field-help", capture ? "This game preserves releases only, so automatic builds are unavailable until it has packaging steps." : `Checks upstream every ${project.poll_minutes || 60} minutes.`),
    el("div", "inline-actions", button("Save automation", "button secondary small", () => saveAutomation(project.id, autoBuild.checked, autoPublish.checked, destination.value), busy.has(`automation-${project.id}`) || capture)));
  return card;
}
async function saveAutomation(id, autoBuild, autoPublish, destination) {
  busy.add(`automation-${id}`); render();
  try { await api(`/api/projects/${encodeURIComponent(id)}`, { method: "PATCH", body: { auto_build: autoBuild, auto_publish: autoBuild && autoPublish, auto_publish_destination: destination } }); automationDrafts.delete(id); toast("Automation settings saved."); await refresh(); }
  catch (error) { toast(error.message, true); }
  finally { busy.delete(`automation-${id}`); render(); }
}
function openBuild(id, captureId) {
  const project = projectById(id); if (!project) return; buildProjectId = id;
  const releases = (project.releases || []).filter((release) => release.complete && release.status !== "needs_review");
  const select = $("build-release"); select.replaceChildren();
  if (!releases.length && project.capture_id) { const option = el("option", "", project.latest_version || "Latest preserved release"); option.value = project.capture_id; select.append(option); }
  releases.forEach((release) => { const option = el("option", "", `${release.version} · ${releaseLabels[release.status] || release.status}`); option.value = release.capture_id; select.append(option); });
  if (captureId) select.value = captureId;
  const targets = $("build-targets"); targets.replaceChildren();
  project.targets.forEach((target) => { const box = input(`build-target-${target}`, "checkbox"); box.value = target; box.checked = true; targets.append(el("label", "", [box, targetLabels[target] || target])); });
  $("build-heading").textContent = `Build ${project.name}`;
  $("build-note").textContent = project.packaging?.runner === "operations" ? "Builds run the packaging definition with your verified game files. Large games need time and free disk space; progress and errors appear under Updates." : "Builds run this game's saved build instructions.";
  $("build-error").textContent = ""; $("build-submit").disabled = !select.options.length; $("build-dialog").showModal();
}
async function submitBuild(event) {
  event.preventDefault(); const project = projectById(buildProjectId); if (!project) return;
  const targets = [...$("build-targets").querySelectorAll("input:checked")].map((node) => node.value);
  if (!targets.length) { $("build-error").textContent = "Choose at least one platform."; return; }
  $("build-submit").disabled = true;
  try { const result = await api(`/api/projects/${encodeURIComponent(project.id)}/build`, { method: "POST", body: { targets, capture_id: $("build-release").value || undefined } }); $("build-dialog").close(); if (result.message) toast(result.message); if (result.jobs?.length) { toast("Build queued. Follow its progress under Updates."); navigateProject(project.id, "activity"); } await refresh(); }
  catch (error) { $("build-error").textContent = error.message; }
  finally { $("build-submit").disabled = false; }
}
function publishToRomM(id) { return runAction(`romm-${id}`, `/api/artifacts/${encodeURIComponent(id)}/publish-romm`, {}, "Upload to RomM queued. Its progress appears under Updates."); }
function refreshCatalog() { return runAction("catalog-refresh", "/api/catalog/refresh", {}, "Catalog refreshed."); }
function renderCatalog() {
  const catalog = state.catalog || {}, node = $("catalog-content"); if (!node) return;
  node.replaceChildren();
  const age = catalog.age_seconds == null ? "never fetched from GitHub" : catalog.age_seconds < 3600 ? `${Math.max(1, Math.round(catalog.age_seconds / 60))} minutes old` : catalog.age_seconds < 172800 ? `${Math.round(catalog.age_seconds / 3600)} hours old` : `${Math.round(catalog.age_seconds / 86400)} days old`;
  node.append(el("div", "detail-facts", [meta("Using", catalog.using || "bundled catalog"), meta("Source", catalog.source || "—", true), meta("Age", age), meta("Last checked", catalog.checked_at ? dateLabel(catalog.checked_at) : "Not yet")]));
  if (catalog.error) node.append(el("p", "project-error", catalog.error));
  node.append(el("div", "inline-actions", button(busy.has("catalog-refresh") ? "Refreshing…" : "Refresh catalog", "button secondary", refreshCatalog, busy.has("catalog-refresh"))), el("p", "field-help", catalog.explanation || ""));
  const definitions = catalog.definitions || [];
  if (definitions.length) node.append(table(["Packaging definition", "Revision", "Status"], definitions.map((item) => el("tr", "", [el("td", "", [el("span", "table-title", item.name), el("span", "table-detail", `${item.id}${item.repository ? " · " + item.repository : ""}`)]), el("td", "", String(item.revision ?? "—")), el("td", "", item.supported ? statusBadge("ready", item.origin === "github" ? "From GitHub" : "Bundled") : [statusBadge("unsupported_version", "Needs a Portkeeper update"), el("p", "field-help", item.reason || "")])]))));
  const scopes = [];
  (catalog.projects || []).forEach((project) => (project.verification_scopes || []).forEach((scope) => scopes.push(el("tr", "", [el("td", "", [el("span", "table-title", project.name), el("span", "table-detail", project.repository)]), el("td", "", `${scope.release} · ${targetLabels[scope.platform] || scope.platform}${scope.package_revision ? " · package " + scope.package_revision : ""}`), el("td", "", [statusBadge(scope.status === "verified" ? "ready" : scope.status === "needs_review" ? "needs_attention" : "watching", { verified: "Verified packaging profile", candidate: "Candidate · not approved", needs_review: "Needs review" }[scope.status] || scope.status), el("p", "field-help", Object.entries(scope.checks || {}).map(([key, value]) => `${key}: ${value}`).join(" · "))])]))));
  if (scopes.length) node.append(el("h3", "", "Verification scopes"), el("p", "field-help", "Verification applies to the named release, package revision and platform only. It never changes a download's own test result."), table(["Project", "Scope", "Status & evidence"], scopes));
}
function packagingInstructions(project) {
  if (project.packaging) return definitionInstructions(project);
  const recipe = project.recipe || captureRecipe, profile = profiles.find(item => item.id === project.profile);
  const capture = project.preserved_release || {}, section = el("section", "detail-card packaging-instructions");
  section.append(el("h2", "", "Packaging instructions"), el("p", "detail-note", recipe.mode === "capture" ? "Packaging instructions not configured yet. This project preserves upstream source and releases; existing RomM downloads were assembled separately and do not prove automatic packaging is configured." : "These instructions explain the saved configuration Portkeeper uses to produce platform downloads."));
  const list = el("div", "instruction-sections");
  const group = (heading, nodes) => list.append(el("section", "instruction-section", [el("h3", "", heading), ...nodes]));
  const link = (label, url) => { const node = el("a", "text-button", label); if (typeof url === "string" && /^https:\/\//.test(url)) { node.href = url; node.target = "_blank"; node.rel = "noopener noreferrer"; } return node; };
  const upstream = [repoLink(project.repo)];
  if (/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(project.repo || "")) upstream.push(link("Upstream README & build documentation ↗", `https://github.com/${project.repo}#readme`));
  if (capture.release_url || profile?.release_url) upstream.push(link("Recorded release ↗", capture.release_url || profile.release_url));
  upstream.push(el("p", "field-help", `Release: ${profile?.tested_release || capture.version || project.latest_version || "Unknown"} · Source commit: ${profile?.source_commit || capture.commit || "Unknown"}`));
  group("1. Upstream source & release", upstream);
  const downloads = [];
  Object.entries(profile?.runtimes || {}).forEach(([target, asset]) => downloads.push(el("p", "", [targetLabels[target] || target, ": ", link(asset.name, asset.url), el("span", "field-help", ` · SHA-256: ${asset.sha256 || "Unknown"}`)])));
  (capture.assets || []).forEach(asset => downloads.push(el("p", "", [link(asset.name || asset.file || "Captured asset", asset.url), el("span", "field-help", ` · SHA-256: ${asset.sha256 || "Unknown"}`)])));
  if (!downloads.length) downloads.push(el("p", "field-help", "No runtime asset downloads are recorded in the packaging configuration. The upstream release link shows available downloads; they are not selected automatically."));
  if (recipe.payload?.asset_patterns) downloads.push(el("pre", "json-preview", JSON.stringify(recipe.payload.asset_patterns, null, 2)));
  group("2. Runtime downloads", downloads);
  const inputs = [];
  if (profile) inputs.push(el("p", "field-help", `Supported input: ${profile.rom_search || "See profile documentation"}. Output assets: ${profile.asset_format || "Unknown"}. Select and verify the game file through the build profile.`));
  (recipe.inputs || []).forEach(item => inputs.push(el("p", "field-help", `${item.name || item.path || "Game input"} → ${item.dest || item.destination || "See saved configuration"}`)));
  if (project.rom_source) inputs.push(el("p", "field-help", `Selected game file: ${project.rom_source.member || project.rom_source.filename || project.rom_source.path || "RomM input"}`));
  if (!inputs.length) inputs.push(el("p", "field-help", "Required game files are not configured. Any historical preparation notes are shown below; they are not executable packaging steps."));
  group("3. Required game files", inputs);
  const steps = [];
  if (profile) steps.push(el("p", "field-help", profile.description), el("p", "field-help", `Build host requirements: ${(profile.runner?.requirements || []).join(", ") || "Unknown"}`));
  else if (recipe.mode === "capture") steps.push(el("p", "field-help", "Preserve the source and release checkpoint. No extraction, compilation or assembly instructions are saved."));
  else { steps.push(el("p", "field-help", `Packaging method: ${recipe.mode || "Unknown"}. Expected outputs: ${(recipe.expected_files || []).join(", ") || "Not recorded"}.`)); if (Array.isArray(recipe.steps) && recipe.steps.length) steps.push(el("pre", "json-preview", JSON.stringify(recipe.steps, null, 2))); if (recipe.command) steps.push(el("pre", "json-preview", Array.isArray(recipe.command) ? JSON.stringify(recipe.command, null, 2) : String(recipe.command))); }
  if (recipe.target_overrides) steps.push(el("p", "field-help", "Platform-specific steps are included in the saved configuration below."));
  if (recipe.notes) steps.push(el("p", "review-notes", recipe.notes));
  group("4. Packaging steps & preparation notes", steps);
  const imports = importedPackages(project);
  group("5. Existing downloads & launch instructions", [el("p", "field-help", imports.length ? "Existing RomM imports are separate from this project's saved packaging configuration. Their launch instructions and test results are beside each platform download." : "A built archive or matching RomM import will appear under Play."), button("View downloads & launch instructions →", "button secondary small", () => navigateProject(project.id, "overview"))]);
  section.append(list); return section;
}
function platformEvidence(detection) {
  const notes = el("div", "platform-evidence");
  if (!detection) return notes;
  if (!detection.platforms?.length) notes.append(el("p", "field-help", "No platform evidence found. Choose platforms manually; Portkeeper will not assume Windows and Linux."));
  (detection.platforms || []).forEach(platform => {
    const evidence = platform.evidence || [], released = evidence.some(item => item.kind === "release");
    notes.append(el("p", "field-help", [el("strong", "", `${platform.label}: `), released ? "Release download found. " : "Documentation or catalog instructions found; no matching release download. ", platform.packaging_supported ? "" : "Detected, but this Portkeeper runner cannot package this OS yet. "]));
    const detail = disclosure(`platform-evidence:${platform.id}`, "diagnostics-list", [el("summary", "table-action", `Why ${platform.label} was detected`), ...evidence.map(item => el("p", "field-help", `${item.source}: ${item.detail}`))]);
    notes.append(detail);
  });
  if (detection.documentation_error) notes.append(el("p", "field-help", detection.documentation_error));
  if (detection.note) notes.append(el("p", "field-help", detection.note));
  return notes;
}
function projectPlatformsCard(project) {
  const key = `platforms-${project.id}`, definition = project.packaging;
  const editable = definition?.runner === "capture" || !definition;
  const allowed = editable ? ["linux", "windows", "handheld"] : definition.targets;
  const selection = platformSelections.get(project.id) || project.targets;
  const card = el("section", "detail-card", [el("h2", "", "Platforms to build"), el("p", "field-help", "Change the platforms you selected when adding this game. Detect reads the latest release and linked build documentation; it does not start a build.")]);
  const choices = el("div", "setup-options");
  ["linux", "windows", "handheld"].forEach(target => {
    const box = input(`project-platform-${project.id}-${target}`, "checkbox"); box.checked = selection.includes(target); box.disabled = !allowed.includes(target) || busy.has(key);
    box.addEventListener("change", () => { const draft = platformSelections.get(project.id) || [...project.targets]; platformSelections.set(project.id, box.checked ? [...new Set([...draft, target])] : draft.filter(value => value !== target)); renderProject(); });
    choices.append(el("label", "", [box, targetLabels[target], allowed.includes(target) ? "" : el("span", "field-help", " — not in saved instructions; edit or redraft them first")]));
  });
  card.append(choices, platformEvidence(platformDetections.get(project.id)), el("div", "inline-actions", [button(busy.has(key) ? "Checking…" : "Detect supported platforms", "button secondary small", () => detectProjectPlatforms(project.id), busy.has(key) || project.demo), button("Save platforms", "button primary small", () => saveProjectPlatforms(project.id), busy.has(key) || !selection.length)]));
  return card;
}
async function detectProjectPlatforms(id) {
  const key = `platforms-${id}`; if (busy.has(key)) return; busy.add(key); render();
  try {
    const project = projectById(id), result = await api(`/api/github/lookup?repo=${encodeURIComponent(project.repo)}`);
    platformDetections.set(id, result.platform_detection);
    const allowed = !project.packaging || project.packaging.runner === "capture" ? ["linux", "windows", "handheld"] : project.packaging.targets;
    const suggested = result.platforms.filter(target => allowed.includes(target));
    if (suggested.length) platformSelections.set(id, suggested);
    toast("Platform evidence loaded. Review the selection and save it when ready.");
  } catch(error) { toast(error.message, true); }
  finally { busy.delete(key); render(); }
}
async function saveProjectPlatforms(id) {
  const project = projectById(id), targets = platformSelections.get(id) || project.targets, body = {targets};
  if (project.packaging?.runner === "capture") body.packaging = {...project.packaging, targets: [...new Set([...project.packaging.targets, ...targets])]};
  const key = `platforms-${id}`; if (busy.has(key)) return; busy.add(key); render();
  try { await api(`/api/projects/${encodeURIComponent(id)}`, {method:"PATCH", body}); platformSelections.delete(id); toast("Platforms saved. Draft again if you need packaging steps for a newly selected platform."); await refresh(); }
  catch(error) { toast(error.message, true); }
  finally { busy.delete(key); render(); }
}

function renderAdvanced(project, content, openJobs) {
  const stack = el("div", "advanced-stack");
  const configuration = el("section", "detail-card", [el("h2", "", project.packaging ? "Your build instructions" : "Saved configuration & automation"), el("p", "detail-note", project.packaging ? "The readable instructions above are generated from this definition. Editing it creates a new definition revision, so new packages and their tests are never confused with older ones." + (project.profile ? " The reviewed profile's build commands stay fixed." : "") : project.profile ? "This game uses fixed, reviewed build instructions for its supported release. Automation settings are editable; changing trusted build execution requires a profile update. New unsupported releases are preserved for review." : "A custom recipe controls required inputs, outputs, and the build runner.")]);
  if (!project.packaging) configuration.append(el("div", "detail-facts", [meta("Packaging method", project.recipe?.mode || "capture"), meta("Automatic build", project.auto_build ? "Enabled" : "Disabled"), meta("Automatic local library copy", project.auto_publish ? "Enabled" : "Disabled"), meta("Polling interval", `${project.poll_minutes || 60} minutes`)]));
  configuration.append(externalLink("Configuration reference ↗", "https://github.com/Crimson3076/portkeeper/blob/main/docs/PACKAGING.md"));
  configuration.append(button(project.packaging ? "Edit packaging definition" : "Edit packaging instructions", "button secondary small", () => openRecipe(project.id)));
  const json = disclosure("configuration-json", "diagnostics-list"); json.append(el("summary", "table-action", project.packaging ? "View definition JSON" : "Executable configuration (JSON)"), el("pre", "json-preview", JSON.stringify(project.packaging || project.recipe || captureRecipe, null, 2))); configuration.append(json);
  if (project.packaging) {
    const source = project.definition_source;
    configuration.append(el("p", "field-help", source ? `Based on ${source.kind} revision ${source.revision}. Current revision ${project.packaging.revision}. Edits apply to this game only.` : "This game's own definition. Edits apply to this game only."));
    const history = disclosure("configuration-history", "diagnostics-list", el("summary", "table-action", "Saved revisions (last 20)"));
    (project.definition_history || []).slice().reverse().forEach(record => history.append(el("div", "inline-actions", [el("span", "field-help", `Revision ${record.definition.revision} · ${dateLabel(record.saved_at)}`), button("Review & restore", "button secondary small", () => { openRecipe(project.id); $("recipe-json").value = JSON.stringify(record.definition, null, 2); editorDirty = true; switchEditor("changes"); })])));
    if (!(project.definition_history || []).length) history.append(el("p", "field-help", "Your previous definition is saved here when you edit it. Restoring creates a new revision; existing packages are unchanged."));
    configuration.append(history);
  }
  stack.append(projectPlatformsCard(project), configuration);
  if (project.packaging && !project.demo && !["recipe", "profile"].includes(project.packaging.runner)) stack.append(draftCard(project));
  stack.append(packagingInstructions(project));
  stack.append(retentionCard(project), preservationCard(project));
  stack.append(button("Edit game metadata link", "button secondary", () => openMetadata(project.id)));
  if (project.packaging?.runner === "recipe") {
    const assistant = el("section", "detail-card", [el("h2", "", "Draft packaging instructions with Ollama"), el("p", "detail-note", project.profile ? "A local model can draft notes for this verified profile. Its reviewed build instructions remain fixed; proposals are reviewed before saving." : "Describe a required change or a build error. A local model can draft a proposal; you review the JSON before saving it.")]);
    const prompt = el("textarea", "assistant-prompt"); prompt.id = "assistant-project-prompt"; prompt.rows = 3; prompt.placeholder = "Explain the change you want to make…"; prompt.value = assistantPromptDrafts.get(project.id) || ""; prompt.addEventListener("input", () => assistantPromptDrafts.set(project.id, prompt.value));
    field(assistant, "Requested change", prompt);
    const assistantConfig = state.assistant || state.assistant_settings || state.settings?.assistant || {};
    const configured = Boolean(assistantConfig.endpoint && assistantConfig.model);
    assistant.append(el("div", "inline-actions", [button(busy.has(`proposal-${project.id}`) ? "Drafting proposal…" : "Draft proposal", "button secondary small", () => draftProposal(project.id), !configured || busy.has(`proposal-${project.id}`)), button("Assistant settings →", "text-button", () => navigateSettings("assistant"))]));
    if (!configured) assistant.append(el("p", "field-help", "Configure your local Ollama endpoint and model in Settings first.")); stack.append(assistant);
  }
  if (project.rom_source?.sha1) stack.append(el("section", "detail-card", [el("h2", "", "Game input identity"), el("div", "detail-facts", [meta("ROM SHA-1", project.rom_source.sha1, true), meta("Cache key", project.rom_source.cache_key || "Not cached", true)])]));
  content.append(stack);
}

function renderBuildDiagnostics(project, content, openJobs) {
  const diagnostic = el("section", "detail-card", [el("h2", "", "Build progress & logs"), el("p", "detail-note", "Detailed logs and errors for troubleshooting a game step.")]);
  const jobs = projectJobs(project.id); const logs = el("div", "diagnostics-list");
  jobs.forEach((job) => { const details = disclosure(`job:${job.id}`, "job-panel"); details.dataset.jobId = job.id; details.open = openJobs.has(String(job.id)) || String(job.id) === String(selectedJobId); details.append(el("summary", "", [targetPill(job.target || "capture"), el("span", "", job.version || "Update check"), statusBadge(jobStage(job)), el("span", "job-date", dateLabel(job.created_at))]), el("pre", "", (Array.isArray(job.logs) ? job.logs.join("\n") : String(job.logs || "Waiting for log output…")) + (job.error ? `\n\nError: ${job.error}` : ""))); if (job.status === "failed") details.append(button("Retry failed step", "button secondary small", () => retryJob(job.id), busy.has(`retry-${job.id}`))); logs.append(details); });
  diagnostic.append(jobs.length ? logs : el("p", "detail-empty", "No diagnostics yet.")); content.append(diagnostic);

}

function openMetadata(id) {
  const project = projectById(id); metadataProjectId = id;
  const identity = project.metadata || {};
  $("metadata-game").value = identity.game || project.name;
  $("metadata-prefix").value = identity.filename_prefix || project.library_basename || "";
  $("metadata-romm").value = identity.romm_rom_id || "";
  $("metadata-igdb").value = identity.igdb_id || "";
  $("metadata-cover").value = identity.cover_policy || "preserve";
  $("metadata-cover-custom").hidden = identity.cover_policy !== "custom";
  $("metadata-error").textContent = ""; $("metadata-dialog").showModal();
}
function openCoverSearch(id) {
  const project = projectById(id); coverProjectId = id; coverChoice = null;
  const identity = project.metadata || {};
  const knownPrefix = identity.filename_prefix || project.library_basename || "";
  $("cover-query").value = identity.game || project.name;
  $("cover-prefix").value = knownPrefix;
  ["cover-prefix", "cover-prefix-label", "cover-prefix-help"].forEach((node) => { $(node).hidden = Boolean(knownPrefix); });
  $("cover-results").replaceChildren(); $("cover-status").textContent = ""; $("cover-error").textContent = "";
  $("cover-apply").disabled = true; $("cover-dialog").showModal();
  if (rommConfig().url) void searchCovers();
  else $("cover-error").textContent = "Connect RomM in Settings first; cover search runs through your RomM server.";
}
async function searchCovers(event) {
  event?.preventDefault();
  const query = $("cover-query").value.trim(); if (!query) return;
  $("cover-search").disabled = true; $("cover-error").textContent = ""; $("cover-status").textContent = "Searching…";
  $("cover-results").replaceChildren(); coverChoice = null; $("cover-apply").disabled = true;
  try {
    const result = await api(`/api/romm/covers?q=${encodeURIComponent(query)}`);
    const covers = result.covers || [];
    $("cover-status").textContent = covers.length ? `${covers.length} covers found. Pick one to apply.` : "No covers found. Try a shorter or different title.";
    covers.forEach((cover) => {
      const option = el("button", "cover-option"); option.type = "button"; option.setAttribute("aria-pressed", "false");
      const image = el("img"); image.src = `/api/romm/cover-preview?url=${encodeURIComponent(cover.thumb)}`; image.alt = `${cover.game} cover`; image.loading = "lazy"; image.decoding = "async";
      option.append(image, el("span", "", cover.game), el("span", "", [cover.width && cover.height ? `${cover.width}×${cover.height}` : cover.provider, cover.author ? ` · ${cover.author}` : ""].join("")));
      option.addEventListener("click", () => {
        $("cover-results").querySelectorAll(".cover-option").forEach((node) => node.setAttribute("aria-pressed", "false"));
        option.setAttribute("aria-pressed", "true"); coverChoice = cover.url; $("cover-apply").disabled = false;
      });
      $("cover-results").append(option);
    });
  } catch (error) { $("cover-status").textContent = ""; $("cover-error").textContent = error.message; }
  finally { $("cover-search").disabled = false; }
}
async function applyCover() {
  if (!coverChoice) return;
  $("cover-apply").disabled = true; $("cover-error").textContent = "";
  try {
    const body = {url_cover: coverChoice};
    if (!$("cover-prefix").hidden) body.filename_prefix = $("cover-prefix").value.trim();
    const result = await api(`/api/projects/${coverProjectId}/cover`, {method: "POST", body});
    $("cover-dialog").close(); await refresh();
    if (result.warning) toast(result.warning, true);
    else if (result.matched) toast(`Cover applied to ${result.matched} RomM ${result.matched === 1 ? "entry" : "entries"}. New imports get it automatically.`);
    else toast("Cover saved. Linux and Windows imports get it once RomM scans them.");
  } catch (error) { $("cover-error").textContent = error.message; $("cover-apply").disabled = false; }
}
async function saveMetadata(event) {
  event.preventDefault(); $("metadata-save").disabled = true;
  try {
    const identity = {game: $("metadata-game").value.trim(), filename_prefix: $("metadata-prefix").value.trim(), cover_policy: $("metadata-cover").value};
    if (identity.cover_policy === "custom") identity.url_cover = projectById(metadataProjectId).metadata?.url_cover;
    const reference = $("metadata-romm").value.trim();
    if (reference) {
      const match = reference.match(/^(?:https?:\/\/[^/]+\/rom\/)?([0-9]+)\/?$/);
      if (!match) throw new Error("Enter a RomM entry ID or its /rom/123 link.");
      await api(`/api/projects/${metadataProjectId}/metadata-link`, {method: "POST", body: {...identity, romm_rom_id: Number(match[1])}});
    } else {
      const igdb_id = Number($("metadata-igdb").value);
      const optional = identity.cover_policy === "custom" && !$("metadata-igdb").value.trim();
      if (!optional && (!Number.isSafeInteger(igdb_id) || igdb_id < 1)) throw new Error("Link a matched RomM entry or enter its IGDB game ID.");
      await api(`/api/projects/${metadataProjectId}`, {method: "PATCH", body: {metadata: optional ? identity : {...identity, igdb_id}}});
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
function editorValue() {
  const value = JSON.parse($("recipe-json").value);
  if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("Configuration must be a JSON object.");
  return value;
}
function openRecipe(id) {
  const project = projectById(id); if (!project) return; recipeProjectId = id;
  editorOriginal = JSON.parse(JSON.stringify(project.packaging || project.recipe || captureRecipe)); editorDirty = false;
  $("recipe-heading").textContent = `${project.name} build instructions`;
  $("recipe-json").value = JSON.stringify(editorOriginal, null, 2);
  $("recipe-json").readOnly = Boolean(project.profile && !project.packaging);
  $("recipe-template").disabled = Boolean(project.packaging || project.profile); $("apply-template").disabled = $("recipe-template").disabled;
  $("recipe-template").value = ""; $("recipe-form-error").textContent = ""; $("recipe-validation").textContent = "";
  const source = project.definition_source;
  $("recipe-origin").textContent = `Applies to ${project.name} only. ${project.packaging ? `Revision ${project.packaging.revision}. ` : ""}${source ? `Based on ${source.kind} revision ${source.revision}. ` : ""}Saving changes future builds, not existing downloads.`;
  switchEditor(project.packaging ? "settings" : "json"); $("recipe-dialog").showModal();
}
function renderEditorFields() {
  const value = editorValue(), parent = $("recipe-fields"); parent.replaceChildren();
  if (!projectById(recipeProjectId)?.packaging) { parent.append(el("p", "field-help", "This legacy recipe is edited in JSON. All saved fields remain available.")); return; }
  const edit = (label, path, multiline = false) => {
    let current = value; path.forEach(key => { current = current?.[key]; });
    const node = multiline ? el("textarea") : input(`editor-${path.join("-")}`, "text");
    node.id = `editor-${path.join("-")}`; node.value = current || ""; if (multiline) node.rows = 3;
    node.addEventListener("input", () => {
      const draft = editorValue(); let owner = draft;
      path.slice(0,-1).forEach(key => { owner[key] ||= {}; owner = owner[key]; });
      if (!node.value && path.at(-1) === "package_prefix") delete owner[path.at(-1)]; else owner[path.at(-1)] = node.value; $("recipe-json").value = JSON.stringify(draft, null, 2); editorDirty = true; $("recipe-validation").textContent = "";
    });
    field(parent, label, node);
  };
  edit("Name", ["name"]); edit("Description", ["summary"], true); edit("Package filename prefix", ["package_prefix"]);
  edit("Release tag pattern", ["upstream", "release_rules", "tag_pattern"]);
  edit("Save files & settings location", ["saves"], true);
  for (const target of value.targets || []) { parent.append(el("h3", "", targetLabels[target] || target)); edit("Launch command", ["launch", target, "command"]); edit("Launch instructions", ["launch", target, "instructions"], true); }
  parent.append(el("h3", "", "Build steps"));
  if (!(value.operations || []).length) parent.append(el("p", "field-help", value.runner === "profile" ? "A reviewed profile implements these steps. Its execution is fixed." : "No operations are defined. Add supported operations in JSON."));
  (value.operations || []).forEach((step, index) => parent.append(button(`${index+1}. ${step.op}${step.tool ? " · " + step.tool : ""} — edit in JSON`, "button secondary step-edit", () => { switchEditor("json"); const node = $("recipe-json"); node.focus(); const text = JSON.stringify(step, null, 2).split("\n").map((line,i) => i ? "    " + line : line).join("\n"); const start = node.value.indexOf(text); if (start >= 0) node.setSelectionRange(start, start+text.length); })));
}
function configurationChanges(before, after, path = "") {
  if (JSON.stringify(before) === JSON.stringify(after)) return [];
  if (before && after && typeof before === "object" && typeof after === "object" && !Array.isArray(before) && !Array.isArray(after)) return [...new Set([...Object.keys(before), ...Object.keys(after)])].flatMap(key => configurationChanges(before[key], after[key], path ? `${path}.${key}` : key));
  return [`${path || "configuration"}\n− ${JSON.stringify(before) ?? "(absent)"}\n+ ${JSON.stringify(after) ?? "(removed)"}`];
}
function switchEditor(view) {
  try {
    const draft = editorValue();
    if (view === "settings") renderEditorFields();
    if (view === "changes") $("recipe-diff").textContent = configurationChanges(editorOriginal, draft).join("\n\n") || "No changes.";
    editorView = view;
    ["settings", "json", "changes"].forEach(name => { $( `editor-${name}`).hidden = name !== view; const tab = $(`editor-${name}-tab`); tab.classList.toggle("selected", name === view); tab.setAttribute("aria-selected", String(name === view)); tab.tabIndex = name === view ? 0 : -1; });
    $("recipe-form-error").textContent = "";
  } catch (error) { $("recipe-form-error").textContent = `Fix the JSON before switching views: ${error.message}`; }
}
async function validateEditor() {
  $("recipe-form-error").textContent = ""; $("recipe-validation").textContent = "";
  try {
    const configuration = editorValue(), key = projectById(recipeProjectId)?.packaging ? "packaging" : "recipe";
    await api(`/api/projects/${encodeURIComponent(recipeProjectId)}/configuration/validate`, {method:"POST", body:{[key]: configuration}});
    $("recipe-validation").textContent = "Configuration is valid. This checks structure and supported settings; it does not run a build.";
  } catch (error) { $("recipe-form-error").textContent = error.message; }
}
async function submitRecipe(event) {
  event.preventDefault(); const submit = $("recipe-submit"); if (submit.disabled) return; $("recipe-form-error").textContent = "";
  try {
    const recipe = editorValue(); submit.disabled = true; submit.textContent = "Saving…";
    const definitionBacked = Boolean(projectById(recipeProjectId)?.packaging);
    await api(`/api/projects/${encodeURIComponent(recipeProjectId)}`, { method: "PATCH", body: definitionBacked ? {packaging:recipe} : {recipe} }); editorDirty = false; $("recipe-dialog").close(); toast("Configuration saved. Existing downloads are unchanged."); await refresh();
  } catch (error) { $("recipe-form-error").textContent = error.message; }
  finally { submit.disabled = false; submit.textContent = "Save configuration"; }
}
function openValidation(artifact) { openReview(projectById(artifact.project_id), artifact, null); }
function openReview(project, artifact, rom) {
  reviewContext = {projectId: project.id, artifact, rom};
  $("validation-form").reset();
  const target = artifact?.target || (rom.platform_slug === "win" ? "windows" : "linux");
  $("validation-package").textContent = `${project.name} · ${targetLabels[target]} · ${artifact ? "Portkeeper build " + artifact.version : rom.name}`;
  const review = reviewFor(artifact, rom);
  $("validation-device").value = review?.device || "";
  $("validation-notes").value = review?.notes || "";
  $("validation-result").value = review?.result || "";
  $("validation-environment-field").hidden = target !== "windows"; $("validation-environment").value = review?.environment || "native";
  $("validation-error").textContent = ""; syncReviewFields(); $("validation-dialog").showModal();
}
function syncReviewFields() {
  const result = $("validation-result").value;
  $("validation-device").required = Boolean(result && result !== "untested");
  $("validation-confirm").required = Boolean(result && result !== "untested");
  $("validation-confirm").parentElement.hidden = result === "untested";
  $("validation-notes").required = result === "failed";
}
async function submitValidation(event) {
  event.preventDefault(); const submit = $("validation-submit"); if (submit.disabled || !reviewContext) return; submit.disabled = true; $("validation-error").textContent = "";
  const {artifact,rom,projectId} = reviewContext;
  try {
    await api(`/api/projects/${projectId}/playtest`, {method:"POST",body:{...(artifact ? {artifact_id:artifact.id,signature:artifact.sha256} : {romm_rom_id:rom.id,signature:rom.package_signature}),result:$("validation-result").value,environment:$("validation-environment-field").hidden ? "native" : $("validation-environment").value,device:$("validation-device").value.trim(),notes:$("validation-notes").value.trim(),confirmed:$("validation-confirm").checked}});
    $("validation-dialog").close(); toast("Test result saved for this download and added to Activity.");
    rommImports.clear(); await refresh(true);
  } catch (error) { $("validation-error").textContent = error.message; }
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
let assistantModelsSerial = 0;
function setAssistantModel(name) {
  const select = $("assistant-model");
  if (name && ![...select.options].some(option => option.value === name)) {
    const option = el("option", "", `${name} (saved — refresh to check availability)`); option.value = name; select.append(option);
  }
  select.value = name;
}
async function refreshAssistantModels() {
  const endpoint = $("assistant-endpoint").value.trim(), serial = ++assistantModelsSerial;
  const control = $("assistant-models-refresh"), status = $("assistant-models-status");
  control.disabled = true; control.textContent = "Loading models…"; status.textContent = "Connecting to Ollama…";
  try {
    const result = await api("/api/assistant/models", {method:"POST", body:{endpoint}});
    if (serial !== assistantModelsSerial || $("assistant-endpoint").value.trim() !== endpoint) return;
    const select = $("assistant-model"), selected = select.value;
    select.replaceChildren(el("option", "", result.models.length ? "Choose a model…" : "No models installed")); select.firstChild.value = "";
    result.models.forEach(name => { const option = el("option", "", name); option.value = name; select.append(option); });
    setAssistantModel(selected);
    status.textContent = result.models.length ? `${result.models.length} installed model${result.models.length === 1 ? "" : "s"} loaded.${selected && !result.models.includes(selected) ? " Your selected model was not returned; choose an installed model before saving." : " Choose one and save your settings."}` : "Ollama is reachable, but no models are installed. Install a model in Ollama, then refresh.";
  } catch (error) {
    if (serial === assistantModelsSerial && $("assistant-endpoint").value.trim() === endpoint) status.textContent = error.message + ". Your selection has been kept.";
  } finally {
    if (serial === assistantModelsSerial) { control.disabled = false; control.textContent = "Refresh models"; }
  }
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
// Add a game ------------------------------------------------------------------
let addGame = null;
const draftPrompts = new Map();
const draftImports = new Map();
function assistantSettings() { return state.assistant || {}; }
function pendingProposal(project) { return (state.proposals || []).find((proposal) => String(proposal.project_id) === String(project.id)); }
async function openAddGame() {
  addGame = { step: "pick", games: [], loading: true, pick: null, files: null, choices: {}, targets: [], keep: false, publish: false, autoPublish: false, busy: false, rommConnected: rommReady(),
    custom: { repo: "", lookup: null, name: "", targets: [], method: assistantSettings().configured ? "assistant" : "handoff" } };
  $("add-game-error").textContent = ""; renderAddGame(); $("add-game-dialog").showModal();
  try { const result = await api("/api/catalog/games"); const groups = new Map();
    (result.games || []).forEach(game => { const key = game.game || game.name; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(game); });
    addGame.games = [...groups.values()].map(variants => ({...variants[0], name: variants[0].game || variants[0].name, variants})); addGame.rommConnected = Boolean(result.romm_connected); }
  catch (error) { $("add-game-error").textContent = error.message; }
  finally { if (addGame) { addGame.loading = false; renderAddGame(); } }
}
function renderAddGame() {
  if (!addGame) return;
  const custom = addGame.step === "custom";
  const labels = custom ? ["Choose a game", "Where it lives", "How it's packaged"] : ["Choose a game", "Your game files", "Set up"];
  const position = addGame.step === "pick" ? 0 : 1;
  $("add-game-steps").replaceChildren(...labels.map((text, index) => el("li", index === position ? "active" : index < position ? "complete" : "", [el("b", "", index < position ? "✓" : String(index + 1)), el("span", "", text)])));
  const content = $("add-game-content"), actions = $("add-game-actions"); content.replaceChildren(); actions.replaceChildren();
  if (addGame.step === "pick") renderGamePicker(content, actions);
  else if (addGame.step === "files") renderGameFiles(content, actions);
  else renderCustomGame(content, actions);
}
function renderGamePicker(content, actions) {
  $("add-game-heading").textContent = "Which game do you want to play?";
  if (addGame.loading) { content.append(el("p", "detail-empty", "Loading supported games…")); return; }
  const grid = el("div", "game-pick-grid");
  addGame.games.forEach((game) => {
    const card = el("button", `game-pick${addGame.pick?.id === game.id ? " selected" : ""}`); card.type = "button";
    const needs = game.needs.length ? "Needs your " + game.needs.map((need) => need.label.replace(/^Your /, "")).join(" and ") : game.runner === "profile" ? "Needs your ROM, chosen from RomM" : "No game files needed";
    card.append(el("strong", "", game.name), el("span", "field-help", needs), el("span", "game-pick-tags", [...[...new Set((game.variants || [game]).flatMap(item => item.targets))].map((target) => el("span", "status-badge", targetLabels[target] || target)), ...(game.in_library ? [statusBadge("ready", "In your library")] : [])]));
    if (game.first_run) card.append(el("span", "field-help", game.first_run.mode === "asks_user" ? "Asks for something on first launch" : "No setup on first launch"));
    card.addEventListener("click", () => {
      if (game.in_library && !(game.variants || []).some(item => !item.in_library)) { $("add-game-dialog").close(); navigateProject(game.in_library); toast(`${game.name} is already in your library.`); return; }
      addGame.pick = game.variants ? {...(game.variants.find(item => !item.in_library) || game), variants: game.variants} : game; renderAddGame();
    });
    grid.append(card);
  });
  const custom = el("button", `game-pick custom${addGame.pick?.id === "custom" ? " selected" : ""}`); custom.type = "button";
  custom.append(el("strong", "", "＋ Custom game"), el("span", "field-help", "Any port or recompilation on GitHub. Write its packaging steps yourself, or have a model draft them for your review."));
  custom.addEventListener("click", () => { addGame.pick = { id: "custom", name: "a custom game" }; renderAddGame(); });
  grid.append(custom);
  content.append(grid);
  actions.append(button(addGame.pick ? `Continue with ${addGame.pick.name}` : "Choose a game", "button primary", continueAddGame, !addGame.pick));
}
function continueAddGame() {
  const pick = addGame.pick; if (!pick) return;
  $("add-game-error").textContent = "";
  if (pick.id === "custom") { addGame.step = "custom"; renderAddGame(); return; }
  if (pick.runner === "profile") { $("add-game-dialog").close(); openWizard(); return; }
  addGame.step = "files"; addGame.variants = pick.variants || [pick]; addGame.targets = [...pick.targets]; addGame.choices = {}; renderAddGame(); void findGameFiles();
}
async function findGameFiles() {
  const pick = addGame.pick; addGame.files = null; addGame.searching = true; $("add-game-error").textContent = ""; renderAddGame();
  try {
    const result = await api(`/api/catalog/games/${encodeURIComponent(pick.id)}/files`, { method: "POST", body: {} });
    if (addGame?.pick !== pick) return;
    addGame.files = result.inputs || [];
    addGame.files.forEach((item) => { if (item.found.length === 1 && item.kind === "file") addGame.choices[item.id] = item.found[0].path; });
  } catch (error) { $("add-game-error").textContent = error.message; addGame.files = null; }
  finally { if (addGame) { addGame.searching = false; renderAddGame(); } }
}
function fileChoicePanel(item) {
  const panel = el("div", "setup-panel", el("h3", "", item.label + (item.required ? "" : " (optional)")));
  const chosen = addGame.choices[item.id];
  const accepted = item.accepted.length ? `This game accepts: ${item.accepted.join(", ")}.` : "";
  if (item.found.length === 1) {
    const found = item.found[0];
    panel.append(el("div", "setup-file", [el("span", "mark ok", "✓"), el("div", "", [el("strong", "", found.name || found.path), el("p", "", `Found in your configured game files folder · ${found.accepted === "checked during the build" ? "checked during the build" : "verified as " + found.accepted}`)])]));
  } else if (item.found.length > 1) {
    panel.append(el("div", "setup-file", [el("span", "mark choose", "?"), el("div", "", [el("strong", "", "Several files could be this one"), el("p", "", "Choose the one to use. Portkeeper checks it again before building.")])]));
    const options = el("div", "setup-options");
    item.found.forEach((found, index) => {
      const radio = input(`file-${item.id}-${index}`, "radio", found.path); radio.name = `file-${item.id}`; radio.checked = chosen === found.path;
      radio.addEventListener("change", () => { addGame.choices[item.id] = found.path; renderAddGame(); });
      options.append(el("label", "", [radio, `${found.name} · ${found.accepted}`]));
    });
    panel.append(options);
  } else {
    const rejected = item.rejected[0];
    const where = item.platform_hint ? `RomM's ${item.platform_hint} folder` : "your RomM library folder";
    panel.append(el("div", "setup-file", [el("span", "mark no", "!"), el("div", "", rejected
      ? [el("strong", "", `${rejected.name} can't be used`), el("p", "", `It is ${rejected.reason}. ${accepted} Add the right file to ${where}, then search again.`)]
      : [el("strong", "", `No ${item.label.replace(/^Your /, "")} found`), el("p", "", `Add it to ${where}, then search again. ${accepted}`)])]));
    const path = input(`file-path-${item.id}`, "text", typeof chosen === "string" ? chosen : "", "Or type its path, such as n64/roms/Game (USA).z64");
    path.setAttribute("aria-label", `${item.label} path`);
    path.addEventListener("input", () => { addGame.choices[item.id] = path.value.trim(); syncAddGameSubmit(); });
    panel.append(el("div", "inline-actions", [path, button("Search again", "button secondary small", findGameFiles, addGame.searching)]));
  }
  if (item.notes) panel.append(el("p", "field-help", item.notes));
  return panel;
}
function addGameReady() { return Boolean(addGame.files) && !addGame.searching && (addGame.files || []).every((item) => item.kind !== "file" || !item.required || Boolean(addGame.choices[item.id])) && addGame.targets.length > 0; }
function syncAddGameSubmit() { const submit = $("add-game-submit"); if (submit) submit.disabled = !addGameReady() || addGame.busy; }
function renderGameFiles(content, actions) {
  const game = addGame.pick; $("add-game-heading").textContent = game.name;
  actions.append(button("← Back", "button subtle left-action", () => { addGame.step = "pick"; renderAddGame(); }, addGame.busy));
  if (addGame.variants?.length > 1) {
    const variant = el("select"); variant.id = "add-game-variant";
    addGame.variants.forEach(item => { const option = el("option", "", item.targets.map(target => targetLabels[target] || target).join(" / ")); option.value = item.id; option.disabled = Boolean(item.in_library); variant.append(option); });
    variant.value = game.id;
    variant.addEventListener("change", () => { addGame.pick = addGame.variants.find(item => item.id === variant.value); addGame.targets = [...addGame.pick.targets]; addGame.choices = {}; void findGameFiles(); });
    field(content, "Platform build instructions", variant, "These platforms use separate build instructions. Add each platform you want using this same game card.");
  }
  if (addGame.searching || !addGame.files) { content.append(el("p", "detail-empty", addGame.searching ? "Looking for your game files in the configured inputs folder…" : "Game files could not be searched.")); if (!addGame.searching) content.append(button("Search again", "button secondary", findGameFiles)); return; }
  addGame.files.filter((item) => item.kind === "file").forEach((item) => content.append(fileChoicePanel(item)));
  const platforms = el("div", "setup-panel", el("h3", "", "Platforms"));
  const options = el("div", "setup-options");
  game.targets.forEach((target) => {
    const box = input(`add-game-target-${target}`, "checkbox"); box.checked = addGame.targets.includes(target);
    box.addEventListener("change", () => { addGame.targets = game.targets.filter((t) => t === target ? box.checked : addGame.targets.includes(t)); syncAddGameSubmit(); });
    options.append(el("label", "", [box, targetLabels[target] || target]));
  });
  platforms.append(options);
  const publishing = el("div", "setup-panel", el("h3", "", "Where should these packages go?"));
  const destination = el("select"); destination.id = "add-game-destination";
  [["local", "Keep in Portkeeper — download when ready"], ...(addGame.rommConnected ? [["romm", "Build and send to RomM"]] : [])].forEach(([value,label]) => { const option = el("option", "", label); option.value = value; destination.append(option); });
  destination.value = addGame.publish ? "romm" : "local";
  field(publishing, "Package destination", destination);
  destination.addEventListener("change", () => { addGame.publish = destination.value === "romm"; renderAddGame(); });
  const updates = el("div", "setup-panel", el("h3", "", "Future releases"));
  const mode = el("select"); mode.id = "add-game-updates";
  [["notify", "Check for updates; let me choose when to build"], ["build", "Automatically build compatible updates"], ...(addGame.rommConnected ? [["publish", "Automatically build and send updates to RomM"]] : [])].forEach(([value,label]) => { const option = el("option", "", label); option.value = value; mode.append(option); });
  mode.value = addGame.autoPublish ? "publish" : addGame.keep ? "build" : "notify";
  mode.addEventListener("change", () => { addGame.keep = mode.value !== "notify"; addGame.autoPublish = mode.value === "publish"; renderAddGame(); });
  field(updates, "Update behavior", mode, "Unexpected releases are preserved for review. Existing packages stay available.");
  content.append(platforms, publishing, updates);
  if (game.first_run) content.append(el("div", "setup-panel", [el("h3", "", "First launch"), el("p", `first-run-note${game.first_run.mode === "asks_user" ? " warn" : ""}`, game.first_run.text || "")]));
  content.append(el("p", "info-note", `Build ${addGame.targets.map(target => targetLabels[target] || target).join(" and ") || "— choose a platform"}. ${addGame.publish ? "Upload to RomM after building." : "Keep packages here for download."} ${addGame.keep ? addGame.autoPublish ? "Future compatible releases build and upload automatically." : "Future compatible releases build automatically without uploading." : "Future releases wait for your build decision."}`));
  if (!addGame.rommConnected) content.append(el("p", "field-help", "RomM is optional. Connect it in Settings when you want to upload packages."));
  const submit = button(addGame.busy ? "Starting build…" : addGame.publish ? "Build and send to RomM" : "Build packages", "button primary", submitAddGame, !addGameReady() || addGame.busy); submit.id = "add-game-submit";
  actions.append(submit);
}
async function submitAddGame() {
  if (!addGame || addGame.busy || !addGameReady()) return;
  addGame.busy = true; $("add-game-error").textContent = ""; renderAddGame();
  try {
    const files = Object.fromEntries(Object.entries(addGame.choices).filter(([, value]) => value));
    const result = await api("/api/setup", { method: "POST", body: { definition_id: addGame.pick.id, targets: addGame.targets, files, keep_updated: addGame.keep, auto_publish: addGame.autoPublish, publish: addGame.publish } });
    $("add-game-dialog").close(); await refresh(); navigateProject(result.project.id);
    toast(`Building ${result.project.name}. Progress shows under Updates.`);
  } catch (error) { $("add-game-error").textContent = error.message; }
  finally { if (addGame) { addGame.busy = false; renderAddGame(); } }
}
function renderCustomGame(content, actions) {
  const custom = addGame.custom; $("add-game-heading").textContent = "Custom game";
  actions.append(button("← Back", "button subtle left-action", () => { addGame.step = "pick"; renderAddGame(); }, addGame.busy));
  const where = el("div", "setup-panel", el("h3", "", "Where is it on GitHub?"));
  const repo = input("custom-repo", "text", custom.repo, "owner/repository or a GitHub link"); repo.setAttribute("aria-label", "GitHub repository");
  repo.addEventListener("input", () => { custom.repo = repo.value; });
  repo.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); void lookupCustomGame(); } });
  where.append(el("div", "inline-actions", [repo, button(custom.looking ? "Looking up…" : "Look up", "button secondary small", lookupCustomGame, custom.looking)]));
  const found = custom.lookup;
  if (found) {
    where.append(el("div", "setup-file", [el("span", "mark ok", "✓"), el("div", "", [el("strong", "", found.repository), el("p", "", `Latest release ${found.version} · ${found.assets.length} release file${found.assets.length === 1 ? "" : "s"}`)])]));
    if (found.catalog_id) {
      const game = addGame.games.find((item) => item.id === found.catalog_id);
      if (game) where.append(el("p", "field-help", `Portkeeper already supports this game.`), button(`Set up ${game.name} instead`, "button secondary small", () => { addGame.pick = game; continueAddGame(); }));
    }
  } else where.append(el("p", "field-help", "Portkeeper reads release files, the README and linked build guides to detect platform support."));
  content.append(where);
  if (!found) return;
  const details = el("div", "setup-panel", el("h3", "", "Name and platforms"));
  const name = input("custom-name", "text", custom.name); name.setAttribute("aria-label", "Name in your library"); name.addEventListener("input", () => { custom.name = name.value; syncCustomSubmit(); });
  details.append(name);
  const options = el("div", "setup-options");
  ["linux", "windows", "handheld"].forEach((target) => {
    const box = input(`custom-target-${target}`, "checkbox"); box.checked = custom.targets.includes(target);
    box.addEventListener("change", () => { custom.targets = ["linux", "windows", "handheld"].filter((t) => t === target ? box.checked : custom.targets.includes(t)); syncCustomSubmit(); });
    options.append(el("label", "", [box, targetLabels[target]]));
  });
  details.append(options, platformEvidence(found.platform_detection));
  content.append(details);
  const settings = assistantSettings();
  const methods = [
    ...(settings.configured ? [["assistant", `Draft with ${settings.drafting_model}`, "The model reads the release, README and source, then proposes the packaging steps. Nothing builds until you approve them."]] : []),
    ["handoff", "Draft with my own chat or agent", "Portkeeper prepares a prompt or a folder for Claude, ChatGPT or another assistant you use. You paste the answer back. No API key needed."],
    ["manual", "Write the packaging steps myself", "Opens the packaging definition editor."],
    ["keep", "Only keep copies of its releases", "Portkeeper saves each release and its source but doesn't build a download. You can add packaging steps later."]];
  const method = el("div", "setup-panel", el("h3", "", "How should it be packaged?"));
  const list = el("div", "choice-list");
  methods.forEach(([value, label, help]) => {
    const radio = input(`custom-method-${value}`, "radio", value); radio.name = "custom-method"; radio.checked = custom.method === value;
    radio.addEventListener("change", () => { custom.method = value; renderAddGame(); });
    list.append(el("label", "choice-card", [radio, el("div", "", [el("h3", "", label), el("p", "", help)])]));
  });
  if (!settings.configured) list.append(button("Connect a local or cloud model in Settings →", "text-button", () => { $("add-game-dialog").close(); navigateSettings("assistant"); }));
  method.append(list);
  content.append(method, el("p", "first-run-note warn", "A custom game is marked as not packaged until its packaging steps are approved and a build succeeds."));
  const submit = button(addGame.busy ? "Adding…" : "Add game", "button primary", submitCustomGame, addGame.busy || !custom.targets.length || !custom.name.trim()); submit.id = "custom-add-submit"; actions.append(submit);
}
function syncCustomSubmit() { const submit = $("custom-add-submit"); if (submit && addGame?.custom) submit.disabled = addGame.busy || !addGame.custom.targets.length || !addGame.custom.name.trim(); }
async function lookupCustomGame() {
  const custom = addGame.custom; let repo;
  try { repo = normalizedRepo(custom.repo || ""); } catch (error) { $("add-game-error").textContent = error.message; return; }
  custom.looking = true; $("add-game-error").textContent = ""; renderAddGame();
  try {
    const result = await api(`/api/github/lookup?repo=${encodeURIComponent(repo)}`);
    custom.lookup = result; custom.repo = result.repository; custom.name = custom.name || result.repository.split("/")[1]; custom.targets = [...result.platforms];
  } catch (error) { $("add-game-error").textContent = error.message; }
  finally { custom.looking = false; renderAddGame(); }
}
async function submitCustomGame() {
  const custom = addGame.custom; if (addGame.busy) return;
  addGame.busy = true; $("add-game-error").textContent = ""; renderAddGame();
  try {
    const result = await api("/api/projects", { method: "POST", body: { name: custom.name.trim(), repo: custom.lookup.repository, targets: custom.targets, poll_minutes: 60, auto_build: false, auto_publish: false, recipe: captureRecipe } });
    const project = result.project || result;
    await api(`/api/projects/${encodeURIComponent(project.id)}/check`, { method: "POST", body: { draft: custom.method === "assistant" } });
    $("add-game-dialog").close(); await refresh();
    navigateProject(project.id, custom.method === "keep" ? "overview" : "advanced");
    if (custom.method === "manual") openRecipe(project.id);
    toast({ assistant: `${project.name} added. Its release is being saved, then ${assistantSettings().drafting_model} drafts the packaging steps.`, handoff: `${project.name} added. When its release is saved, copy the prompt under Draft packaging steps.`, manual: `${project.name} added. Write its packaging steps in the editor.`, keep: `${project.name} added. Its releases are being saved.` }[custom.method]);
  } catch (error) { $("add-game-error").textContent = error.message; }
  finally { if (addGame) { addGame.busy = false; renderAddGame(); } }
}

// Game page: the one next step -------------------------------------------------
function latestPackages(project) {
  const all = projectArtifacts(project.id), version = project.latest_version || all[0]?.version;
  const artifacts = all.filter((artifact) => artifact.version === version);
  return gameTargets(project).map((target) => artifacts.find((artifact) => artifact.target === target)).filter(Boolean);
}
function setupProject(id) { return runAction(`setup-${id}`, `/api/projects/${encodeURIComponent(id)}/setup`, { publish: false }, "Build started. Progress shows under Updates."); }
function nextStepCard(project) {
  const card = el("section", "detail-card next-step-card");
  const active = projectActiveJob(project), proposal = pendingProposal(project), packages = latestPackages(project);
  const runner = project.packaging?.runner || (project.profile ? "profile" : project.recipe?.mode === "capture" ? "capture" : "recipe");
  const inRomm = packages.filter((artifact) => ["in_romm", "awaiting_scan", "uploading"].includes(artifact.romm?.status));
  const untested = packages.filter((artifact) => testStatus(artifact, null) === "untested");
  const parts = [project.latest_version || "No release saved yet"];
  if (packages.length) parts.push(packages.map((artifact) => targetLabels[artifact.target] || artifact.target).join(" & "), inRomm.length === packages.length ? "in RomM" : inRomm.length ? "partly in RomM" : "not in RomM", untested.length ? "not tested yet" : "tested");
  let heading, note = "", action = null;
  const working = busy.has(`setup-${project.id}`);
  if (active) { heading = "Working on it"; note = `${String(jobStage(active)).replaceAll("_", " ")}…`; action = button("View build progress", "button primary small", () => navigateProject(project.id, "activity", active.id)); }
  else if (projectJobs(project.id)[0]?.status === "failed") { heading = "A build needs your attention"; note = projectJobs(project.id)[0].error || "Review the failed step and retry when the cause is resolved."; action = button("View failure & logs", "button primary small", () => navigateProject(project.id, "activity", projectJobs(project.id)[0].id)); }
  else if (project.packaging?.inputs?.some(item => item.required !== false && !project.input_bindings?.[item.id]) && runner === "operations") { heading = "Choose your game files"; note = "Select the required files before building packages."; action = button("Choose game files", "button primary small", () => navigateProject(project.id, "advanced")); }
  else if (proposal) { heading = "Review the drafted packaging steps"; note = "Nothing is built until you approve them."; action = button("Review draft", "button primary small", () => navigateProject(project.id, "advanced")); }
  else if (runner === "capture" && !project.demo) { heading = "Add packaging steps"; note = "Portkeeper keeps this game's releases, but doesn't know how to package it yet."; action = button("Draft packaging steps", "button primary small", () => navigateProject(project.id, "advanced")); }
  else if (!packages.length && !project.demo) { heading = "Build packages"; note = "Check the latest release and build your selected platforms. Packages stay here; uploading is a separate action unless automatic publishing is enabled."; action = button(working ? "Starting…" : "Build packages", "button primary small", () => setupProject(project.id), working); }
  else if (rommReady() && !project.demo && inRomm.length < packages.length) { heading = "Send to RomM"; note = "These packages are built but aren't in RomM yet."; action = button("Send to RomM", "button primary small", () => packages.filter((artifact) => !inRomm.includes(artifact)).forEach((artifact) => publishToRomM(artifact.id))); }
  else if (untested.length) { heading = "Try it and record how it ran"; note = "A result helps you and others know this exact download works."; action = button("Record test result", "button primary small", () => openReview(project, untested[0], null)); }
  else { heading = "Downloads available"; note = project.auto_build ? "New compatible releases are built automatically." : "Check upstream to look for a new release."; }
  card.append(el("h2", "", heading), el("p", "next-step-line", parts.join(" · ")));
  if (note) card.append(el("p", "field-help", note));
  if (action) card.append(el("div", "inline-actions", action));
  return card;
}

// Drafting packaging steps ----------------------------------------------------------
function draftCard(project) {
  const card = el("section", "detail-card draft-card", el("h2", "", "Draft packaging steps"));
  const proposal = pendingProposal(project);
  if (proposal) { renderProposalReview(card, project, proposal); return card; }
  const settings = assistantSettings(), id = project.id, drafting = projectJobs(id).find((job) => job.kind === "draft");
  card.append(el("p", "detail-note", project.packaging?.runner === "operations" ? "Ask for a revised draft, for example after a new release changed something. The current steps stay in use until you approve a draft." : "A model reads this game's latest release, README and source, then proposes the packaging steps. Nothing is built until you approve them."));
  if (drafting && (jobActive(drafting) || drafting.status === "failed")) card.append(el("div", "inline-actions", [statusBadge(jobActive(drafting) ? "building" : "failed", jobActive(drafting) ? "Drafting…" : "Draft failed"), el("span", "field-help", drafting.error || (drafting.logs || []).slice(-1)[0] || "")]));
  const prompt = el("textarea", "assistant-prompt"); prompt.id = `draft-instruction-${id}`; prompt.rows = 2; prompt.placeholder = "Anything the model should know, for example “Windows only” (optional)"; prompt.value = assistantPromptDrafts.get(id) || "";
  prompt.addEventListener("input", () => assistantPromptDrafts.set(id, prompt.value));
  field(card, "Notes for the model", prompt);
  const noRelease = !project.capture_id;
  if (noRelease) card.append(el("p", "field-help", "Check upstream first, so there is a release to read."));
  const actions = el("div", "inline-actions");
  if (settings.configured) actions.append(button(busy.has(`draft-${id}`) ? "Starting…" : `Draft with ${settings.drafting_model}`, "button primary small", () => queueDraft(id), noRelease || busy.has(`draft-${id}`) || Boolean(drafting && jobActive(drafting))));
  actions.append(button(busy.has(`prompt-${id}`) ? "Preparing…" : "Copy prompt for my own chat", settings.configured ? "button secondary small" : "button primary small", () => copyDraftPrompt(id), noRelease || busy.has(`prompt-${id}`)));
  const folder = el("a", "button secondary small", "Download folder for an agent"); folder.href = `/api/projects/${encodeURIComponent(id)}/draft-workspace`; folder.setAttribute("download", "");
  if (!noRelease) actions.append(folder);
  card.append(actions);
  if (draftPrompts.has(id)) {
    const preview = el("textarea", "prompt-preview"); preview.id = `draft-prompt-${id}`; preview.readOnly = true; preview.value = draftPrompts.get(id);
    card.append(disclosure("draft-prompt", "diagnostics-list", [el("summary", "table-action", "Show the prompt"), preview]));
  }
  const paste = el("textarea", "assistant-prompt"); paste.id = `draft-import-${id}`; paste.rows = 4; paste.placeholder = "Paste the JSON answer here"; paste.value = draftImports.get(id) || "";
  paste.addEventListener("input", () => draftImports.set(id, paste.value));
  field(card, "Paste the answer from your chat or agent", paste);
  card.append(el("div", "inline-actions", button(busy.has(`import-${id}`) ? "Checking…" : "Check and review", "button secondary small", () => importDraft(id), busy.has(`import-${id}`))));
  const where = settings.provider === "ollama" ? " Drafts run on your local model." : ["anthropic", "openai"].includes(settings.provider) ? ` Drafts are sent to ${settings.provider_label}.` : "";
  card.append(el("p", "quiet-note", "A model sees only public project material: the release's file list, the README and documentation, and short source excerpts. Never your game files, their locations or any token." + where));
  if (!settings.configured) card.append(button("Connect a local or cloud model →", "text-button", () => navigateSettings("assistant")));
  return card;
}
function renderProposalReview(card, project, proposal) {
  const definition = proposal.definition, view = proposal.view || {};
  card.append(el("p", "detail-note", `Drafted by ${proposal.source} · ${dateLabel(proposal.created_at)}. Nothing has run. Review it before approving.`));
  if (proposal.explanation) card.append(el("p", "review-notes", proposal.explanation));
  card.append(el("div", "detail-facts", [meta("Platforms", definition.targets.map((target) => targetLabels[target] || target).join(", ")), meta("Your files", definition.inputs.filter((item) => item.required !== false).map((item) => item.label).join("; ") || "None"), meta("First launch", view.first_run?.text || "Not stated", true)]));
  if (view.steps?.length) card.append(el("h3", "", "Build steps"), el("ol", "instruction-steps", view.steps.map((step) => el("li", "", step.text))));
  if (proposal.warnings?.length) card.append(el("h3", "", "Check before approving"), el("ul", "review-problems", proposal.warnings.map((warning) => el("li", "", warning))));
  card.append(disclosure(`draft-definition:${proposal.id}`, "diagnostics-list", [el("summary", "table-action", "View definition JSON"), el("pre", "proposal-json", JSON.stringify(definition, null, 2))]));
  card.append(el("div", "inline-actions", [button(busy.has(`approve-${proposal.id}`) ? "Approving…" : "Approve", "button primary small", () => approveDraft(proposal, project), busy.has(`approve-${proposal.id}`)), button("Discard", "button secondary small", () => runAction(`discard-${proposal.id}`, `/api/proposals/${encodeURIComponent(proposal.id)}/discard`, {}, "Draft discarded."))]));
  card.append(el("p", "quiet-note", "After approving, use Build packages to run a test build. A failed build changes nothing else."));
}
async function approveDraft(proposal, project) {
  const result = await runAction(`approve-${proposal.id}`, `/api/proposals/${encodeURIComponent(proposal.id)}/apply`, {}, "Packaging steps approved. Use Build packages to build it.");
  if (result) navigateProject(project.id, "overview");
}
function queueDraft(id) { return runAction(`draft-${id}`, `/api/projects/${encodeURIComponent(id)}/draft`, { instruction: (assistantPromptDrafts.get(id) || "").trim() }, "Drafting started. The draft appears here when it's ready."); }
async function copyDraftPrompt(id) {
  const result = await runAction(`prompt-${id}`, `/api/projects/${encodeURIComponent(id)}/draft-prompt`, { instruction: (assistantPromptDrafts.get(id) || "").trim() }, "");
  if (!result) return;
  draftPrompts.set(id, result.prompt); render();
  try { await navigator.clipboard.writeText(result.prompt); toast("Prompt copied. Paste it into your chat, then paste the answer below."); }
  catch (_) { const preview = $(`draft-prompt-${id}`); if (preview) { preview.closest("details").open = true; preview.select(); } toast("Copy the prompt shown under “Show the prompt”, then paste the answer below."); }
}
async function importDraft(id) {
  const text = (draftImports.get(id) || "").trim(); if (!text) { toast("Paste the answer from your chat or agent first.", true); return; }
  const result = await runAction(`import-${id}`, `/api/projects/${encodeURIComponent(id)}/draft-import`, { text }, "Draft checked. Review it below.");
  if (result) { draftImports.delete(id); render(); }
}

// Settings: drafting assistant ----------------------------------------------------
let assistantProviderDirty = false;
function selectedProvider() { return document.querySelector('[name="assistant-provider"]:checked')?.value || "handoff"; }
function syncAssistantFields() {
  const provider = selectedProvider(), saved = assistantSettings();
  $("assistant-ollama-fields").hidden = provider !== "ollama";
  $("assistant-cloud-fields").hidden = !["anthropic", "openai"].includes(provider);
  $("assistant-base-url-field").hidden = provider !== "openai";
  $("assistant-test").disabled = !saved.configured || provider !== saved.provider;
  const sameProvider = saved.provider === provider && saved.has_api_key;
  $("assistant-api-key").placeholder = sameProvider ? "Saved key · leave blank to keep" : provider === "anthropic" ? "Paste a Claude API key" : "Paste an API key (optional for local servers)";
  $("assistant-clear-key-line").hidden = !sameProvider;
  $("assistant-cloud-model").placeholder = provider === "anthropic" ? "claude-opus-5-5" : "Model name";
}
function renderAssistantSettings() {
  const saved = assistantSettings();
  if (!assistantProviderDirty) document.querySelectorAll('[name="assistant-provider"]').forEach((node) => { node.checked = node.value === (saved.provider || "handoff"); });
  if (!$("assistant-endpoint").dataset.dirty) $("assistant-endpoint").value = saved.endpoint || "";
  if (!$("assistant-model").dataset.dirty) setAssistantModel(saved.model || "");
  if (!$("assistant-context").dataset.dirty) $("assistant-context").value = saved.context_tokens || 32768;
  if (!$("assistant-base-url").dataset.dirty) $("assistant-base-url").value = saved.base_url || "";
  if (!$("assistant-cloud-model").dataset.dirty) $("assistant-cloud-model").value = saved.cloud_model || "";
  syncAssistantFields();
}
async function submitAssistant(event) {
  event.preventDefault(); const submit = $("assistant-save"); if (submit.disabled) return; submit.disabled = true; $("assistant-result").textContent = "";
  const provider = selectedProvider();
  const body = { provider };
  if (provider === "ollama") Object.assign(body, { endpoint: $("assistant-endpoint").value.trim(), model: $("assistant-model").value.trim(), context_tokens: Number($("assistant-context").value) || 32768 });
  if (["anthropic", "openai"].includes(provider)) Object.assign(body, { model: $("assistant-cloud-model").value.trim(), api_key: $("assistant-api-key").value.trim(), clear_api_key: $("assistant-clear-key").checked, ...(provider === "openai" ? { base_url: $("assistant-base-url").value.trim() } : {}) });
  try {
    const saved = await api("/api/assistant/provider", { method: "POST", body });
    $("assistant-result").classList.remove("error");
    $("assistant-result").textContent = provider === "handoff" ? "Saved. Each game's Draft packaging steps card prepares a prompt for your own chat or agent." : `Saved. ${saved.drafting_model} is ready to draft. Use Test connection to check it answers.`;
    ["assistant-endpoint", "assistant-model", "assistant-context", "assistant-base-url", "assistant-cloud-model"].forEach((id) => { $(id).dataset.dirty = ""; });
    assistantProviderDirty = false; $("assistant-api-key").value = ""; $("assistant-clear-key").checked = false; await refresh();
  } catch (error) { $("assistant-result").classList.add("error"); $("assistant-result").textContent = error.message; }
  finally { submit.disabled = false; }
}
async function testAssistant() {
  const control = $("assistant-test"); control.disabled = true; control.textContent = "Testing…"; $("assistant-result").classList.remove("error"); $("assistant-result").textContent = "Sending a short request to the model…";
  try { const result = await api("/api/assistant/test", { method: "POST", body: {} }); $("assistant-result").textContent = `Connected. ${result.model} answered in ${result.seconds} s.`; }
  catch (error) { $("assistant-result").classList.add("error"); $("assistant-result").textContent = error.message; }
  finally { control.textContent = "Test connection"; syncAssistantFields(); }
}
async function loadCloudModels() {
  const provider = selectedProvider(), control = $("assistant-cloud-models-refresh"); control.disabled = true; control.textContent = "Loading…";
  try {
    const result = await api("/api/assistant/models", { method: "POST", body: { provider, base_url: $("assistant-base-url").value.trim(), api_key: $("assistant-api-key").value.trim() } });
    $("assistant-cloud-models").replaceChildren(...result.models.map((name) => { const option = el("option"); option.value = name; return option; }));
    $("assistant-result").classList.remove("error"); $("assistant-result").textContent = result.models.length ? `${result.models.length} models available. Type to pick one.` : "The service returned no models.";
  } catch (error) { $("assistant-result").classList.add("error"); $("assistant-result").textContent = error.message; }
  finally { control.disabled = false; control.textContent = "Load models"; }
}
function openInbox() { renderNotifications($("inbox-content")); $("inbox-dialog").showModal(); }

$("add-project").addEventListener("click", openAddGame);
$("manual-project").addEventListener("click", openCustomProject);
$("refresh").addEventListener("click", () => refresh(true));
$("metadata-form").addEventListener("submit", saveMetadata);
$("build-form").addEventListener("submit", submitBuild);
$("cover-form").addEventListener("submit", searchCovers);
$("cover-apply").addEventListener("click", applyCover);
document.querySelectorAll('[name="library-layout"]').forEach((node) => node.addEventListener("change", () => {
  if (!node.checked) return;
  libraryLayout = node.value; const saved = savePreference("portkeeper.library-layout", libraryLayout);
  $("layout-save-status").textContent = saved ? "Layout saved for this browser." : "Layout selected for this session. Browser storage is unavailable.";
  lastRender = null; renderRoute(); render();
}));
$("project-form").addEventListener("submit", submitProject);
$("settings-advanced-tab").addEventListener("click", () => { settingsView = "storage"; renderSettingsTabs(); });
$("recipe-form").addEventListener("submit", submitRecipe);
["settings", "json", "changes"].forEach(view => $(`editor-${view}-tab`).addEventListener("click", () => switchEditor(view)));
$("recipe-json").addEventListener("input", () => { editorDirty = true; $("recipe-validation").textContent = ""; });
$("recipe-format").addEventListener("click", () => { try { $("recipe-json").value = JSON.stringify(editorValue(), null, 2); } catch(error) { $("recipe-form-error").textContent = error.message; } });
$("recipe-validate").addEventListener("click", validateEditor);
$("recipe-import").addEventListener("click", () => $("recipe-file").click());
$("recipe-file").addEventListener("change", async () => {
  const file = $("recipe-file").files[0]; if (!file) return;
  try { if (file.size > 1024*1024) throw new Error("Choose a JSON file smaller than 1 MiB."); const draft = JSON.parse(await file.text()); if (!draft || Array.isArray(draft) || typeof draft !== "object") throw new Error("Configuration must be a JSON object."); $("recipe-json").value = JSON.stringify(draft, null, 2); editorDirty = true; switchEditor("changes"); } catch(error) { $("recipe-form-error").textContent = error.message; }
  $("recipe-file").value = "";
});
$("recipe-export").addEventListener("click", () => { try { const url = URL.createObjectURL(new Blob([JSON.stringify(editorValue(), null, 2)+"\n"], {type:"application/json"})); const link = el("a"); link.href = url; link.download = `${projectById(recipeProjectId)?.packaging?.id || "portkeeper"}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); } catch(error) { $("recipe-form-error").textContent = error.message; } });
window.addEventListener("beforeunload", event => { if (editorDirty && $("recipe-dialog").open) { event.preventDefault(); event.returnValue = ""; } });
$("validation-result").addEventListener("change", syncReviewFields);
$("activity-game-filter").addEventListener("change", () => { activityGameFilter = $("activity-game-filter").value; renderActivity(); });
$("activity-type-filter").addEventListener("change", () => { activityTypeFilter = $("activity-type-filter").value; renderActivity(); });
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
["romm-url", "assistant-endpoint", "assistant-model", "assistant-context", "assistant-base-url", "assistant-cloud-model"].forEach((id) => $(id).addEventListener("input", () => { $(id).dataset.dirty = "true"; }));
document.querySelectorAll('[name="assistant-provider"]').forEach((node) => node.addEventListener("change", () => { assistantProviderDirty = true; $("assistant-result").textContent = ""; syncAssistantFields(); }));
$("assistant-test").addEventListener("click", testAssistant);
$("assistant-cloud-models-refresh").addEventListener("click", loadCloudModels);
[ ["jobs-tab", "jobs"], ["packages-tab", "packages"], ["notifications-tab", "notifications"] ].forEach(([id, view]) => $(id).addEventListener("click", () => { activityView = view; renderActivity(); }));
document.querySelectorAll("[data-settings-tab]").forEach((node) => node.addEventListener("click", () => { settingsView = node.dataset.settingsTab; renderSettingsTabs(); }));
document.querySelectorAll("[data-project-tab]").forEach((node) => node.addEventListener("click", () => navigateProject(route.projectId, node.dataset.projectTab)));
$("apply-template").addEventListener("click", () => {
  const template = recipes.find((item) => item.id === $("recipe-template").value);
  if (!template) { $("recipe-form-error").textContent = "Choose a recipe template first."; return; }
  editorDirty = true; $("recipe-json").value = JSON.stringify(template.recipe, null, 2); $("recipe-form-error").textContent = "";
});
function discardEditor() { return !editorDirty || window.confirm("Discard your unsaved configuration changes?"); }
$("recipe-dialog").addEventListener("cancel", event => { if (!discardEditor()) event.preventDefault(); });
document.querySelectorAll("[data-close]").forEach((node) => node.addEventListener("click", () => { if (node.dataset.close !== "recipe-dialog" || discardEditor()) $(node.dataset.close).close(); }));
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

$("assistant-models-refresh").addEventListener("click", refreshAssistantModels);
$("assistant-model").addEventListener("change", () => { $("assistant-model").dataset.dirty = "true"; });
$("assistant-endpoint").addEventListener("input", () => { assistantModelsSerial++; $("assistant-models-refresh").disabled = false; $("assistant-models-refresh").textContent = "Refresh models"; $("assistant-models-status").textContent = "Endpoint changed. Refresh to load models from this endpoint."; });
