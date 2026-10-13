"""Tools a model can use to draft, test and submit packaging definitions.

The same tools serve the built-in assistant and outside agents connected over MCP.
Models can read public project material and their own test results. They cannot
approve, publish, choose game files, change settings or delete anything, and they
never see where the person's game files are stored.
"""
import copy
import json
import re
import zipfile
from pathlib import Path, PurePosixPath

from . import drafting, packaging
from .safety import TARGETS, Invalid
from .storage import identifier, now

MAX_TEXT = 60_000


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


GAME = {"type": "string", "description": "Game id from list_games"}
DEFINITION = {"type": "object", "description": "A packaging definition object (see packaging_format)"}

TOOLS = [
    {"name": "investigation_capabilities", "description": "Fixed packaging operations, extraction adapters, environment prerequisites and source-build limits. Read before proposing a plan.", "inputSchema": _schema({})},
    {"name": "read_input_inventory", "description": "Sanitized metadata of already selected game inputs: formats, sizes, standard layout markers and recognized identities. No file locations or game contents.", "inputSchema": _schema({"game_id": GAME}, ["game_id"])},
    {"name": "packaging_format",
     "description": "The packaging definition format, the fixed operations and tools Portkeeper can run, and approved example definitions. Read this before drafting.",
     "inputSchema": _schema({})},
    {"name": "list_games",
     "description": "Games in this Portkeeper library with their platforms, status and whether a draft is waiting for review.",
     "inputSchema": _schema({})},
    {"name": "read_game",
     "description": "One game's preserved release (assets, archive layouts), current definition, recent build errors, pending draft, and which game files are declared and selected.",
     "inputSchema": _schema({"game_id": GAME}, ["game_id"])},
    {"name": "read_material",
     "description": "README, documentation, file tree and source excerpts of the preserved release that the built-in drafting prompt uses, plus a starter definition with known release facts filled in.",
     "inputSchema": _schema({"game_id": GAME}, ["game_id"])},
    {"name": "read_source",
     "description": "Read one file from the preserved release source, or search the source with a regular expression. Give path to read, or pattern to search (optionally limited to paths matching path_filter).",
     "inputSchema": _schema({"game_id": GAME, "path": {"type": "string"}, "pattern": {"type": "string"},
                             "path_filter": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}}, ["game_id"])},
    {"name": "list_release_archive",
     "description": "Full file listing of one preserved release asset archive (ZIP or tar), with sizes.",
     "inputSchema": _schema({"game_id": GAME, "asset": {"type": "string", "description": "Asset file name from read_game"}}, ["game_id", "asset"])},
    {"name": "validate_definition",
     "description": "Run Portkeeper's checks on a definition without building. Returns the exact problems, or the readable build steps and review warnings when it passes.",
     "inputSchema": _schema({"game_id": GAME, "definition": DEFINITION}, ["game_id", "definition"])},
    {"name": "test_build_draft",
     "description": "Build a definition in a throwaway area against the preserved release and report what was produced. Nothing is archived, published or kept. Steps needing game files the person has not selected are skipped and reported. Returns a test id; poll get_test_build.",
     "inputSchema": _schema({"game_id": GAME, "definition": DEFINITION,
                             "targets": {"type": "array", "items": {"type": "string", "enum": list(TARGETS)}}},
                            ["game_id", "definition"])},
    {"name": "get_test_build",
     "description": "Status, log and per-platform results of a test build started with test_build_draft.",
     "inputSchema": _schema({"test_id": {"type": "string"}}, ["test_id"])},
    {"name": "submit_draft",
     "description": "Send a definition to the person's review queue. It is not used until they approve it in Portkeeper. Replaces any earlier pending draft for the game.",
     "inputSchema": _schema({"game_id": GAME, "definition": DEFINITION, "explanation": {"type": "string", "description": "What the definition does, what you verified (cite test ids) and what is uncertain"},
                             "agent": {"type": "string", "description": "Your agent and model name, e.g. 'Codex · gpt-5'"}}, ["game_id", "definition", "explanation"])},
]


class ModelTools:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    # Helpers

    def _project(self, game_id):
        if not isinstance(game_id, str) or not re.fullmatch(r"[a-f0-9]{32}", game_id):
            raise Invalid("Unknown game id; use list_games")
        try:
            return self.store.get("projects", game_id)
        except KeyError:
            raise Invalid("Unknown game id; use list_games") from None

    def _capture(self, project):
        if not project.get("capture_id"):
            raise Invalid("No release is preserved for this game yet. Ask the person to check upstream in Portkeeper first.")
        return self.store.get("captures", project["capture_id"])

    @staticmethod
    def _definition(value):
        if isinstance(value, str):
            value = drafting.extract_json(value)
        if not isinstance(value, dict):
            raise Invalid("definition must be a JSON object")
        return value.get("definition", value) if isinstance(value.get("definition"), dict) else value

    def _source_zip(self, capture):
        path = Path(capture["root"]) / capture["source"]["file"]
        if not zipfile.is_zipfile(path):
            raise Invalid("The preserved source is not a ZIP archive")
        return path

    def _bindings(self, project, definition):
        bindings = project.get("input_bindings") or {}
        return [{"id": item["id"], "label": item["label"], "required": item.get("required", True), "selected": item["id"] in bindings}
                for item in definition.get("inputs", [])]

    def check(self, project, definition, capture):
        """Checks shared by validate, test and submit."""
        definition = drafting.check(self._definition(definition), project)
        assets = [{"name": a["name"], "size": a.get("size")} for a in capture.get("assets", [])]
        _, problems = packaging.download_assets(definition, assets)
        if problems:
            raise Invalid("; ".join(problems) + ". Use asset names from read_game.")
        return definition

    # Tools

    def call(self, name, arguments):
        if not isinstance(arguments, dict):
            raise Invalid("Tool arguments must be an object")
        handler = getattr(self, "tool_" + name, None)
        if name not in {tool["name"] for tool in TOOLS} or handler is None:
            raise Invalid(f"Unknown tool {name}")
        return handler(**arguments)

    def tool_packaging_format(self):
        return {"format": drafting.FORMAT, "simple_example": drafting.SIMPLE_EXAMPLE,
                "approved_examples": drafting.examples(self.engine.catalog.definitions()),
                "rules": ["Only operations and tools named in the format exist. There is no shell.",
                          "inputs are game files the person selects in Portkeeper; Portkeeper uses them while packaging. If the game can use a file the person owns (ROM, ISO, disc image), declare it so Portkeeper asks for it.",
                          "Use inputs: [] and first_run.mode asks_user only when Portkeeper cannot use the files while packaging.",
                          "Test with test_build_draft before submit_draft and fix what it reports."]}

    def tool_investigation_capabilities(self):
        from .investigation import capabilities
        return capabilities(self.engine)

    def tool_read_input_inventory(self, game_id):
        from .investigation import input_inventory
        return input_inventory(self.engine, self._project(game_id))

    def tool_list_games(self):
        pending = {p["project_id"] for p in self.store.all("proposals") if p.get("kind") == "definition" and p.get("status") == "pending_review"}
        games = []
        for project in self.store.all("projects"):
            definition = project.get("packaging") or {}
            games.append({"id": project["id"], "name": project["name"], "repository": project["repo"], "targets": project["targets"],
                          "status": project.get("status"), "latest_release": project.get("latest_version"),
                          "runner": definition.get("runner"), "definition_revision": definition.get("revision"),
                          "draft_waiting_for_review": project["id"] in pending})
        return {"games": games}

    def tool_read_game(self, game_id):
        project = self._project(game_id)
        definition = project.get("packaging")
        result = {"id": project["id"], "name": project["name"], "repository": project["repo"], "targets": project["targets"],
                  "status": project.get("status"), "last_error": project.get("last_error"), "definition": definition,
                  "game_files": self._bindings(project, definition or {})}
        if project.get("capture_id"):
            capture = self._capture(project)
            result["release"] = {"version": capture["version"], "commit": capture["commit"], "complete": capture.get("complete", True),
                                 "assets": [{"name": a["name"], "size": a.get("size")} for a in capture.get("assets", [])]}
        builds = [j for j in self.store.all("jobs") if j["project_id"] == game_id and j.get("kind") != "draft"][:4]
        if builds and builds[0].get('kind') == 'automation' and result.get('last_error'):
            result['last_error'] = 'Automatic setup needs attention; private diagnostics are available only in the local app.'
        result["recent_builds"] = [{"target": j.get("target"), "status": j.get("status"), "error": j.get("error"),
                                    "log_tail": [line[:400] for line in (j.get("logs") or [])[-15:]]} for j in builds]
        for job, view in zip(builds, result['recent_builds']):
            if job.get('kind') == 'automation':
                view['error'] = 'Inspect the local automation record' if job.get('error') else None
                view['log_tail'] = []
        proposal = next((p for p in self.store.all("proposals") if p["project_id"] == game_id and p.get("kind") == "definition" and p.get("status") == "pending_review"), None)
        if proposal:
            result["pending_draft"] = {"source": proposal["source"], "created_at": proposal["created_at"], "definition": proposal["definition"],
                                       "explanation": proposal.get("explanation"), "warnings": proposal.get("warnings")}
        return result

    def tool_read_material(self, game_id):
        project = self._project(game_id)
        _, context = self.engine._draft_material(project, 150_000)
        return {"starter_definition": drafting.starter_definition(context),
                **{key: value for key, value in context.items() if key != "archive_evidence"}}

    def tool_read_source(self, game_id, path=None, pattern=None, path_filter=None, offset=0):
        capture = self._capture(self._project(game_id))
        with zipfile.ZipFile(self._source_zip(capture)) as archive:
            members = {drafting._strip_top(m.filename): m for m in archive.infolist() if not m.is_dir()}
            if path:
                member = members.get(str(PurePosixPath(path)))
                if member is None:
                    near = [name for name in members if name.lower().endswith(PurePosixPath(path).name.lower())][:10]
                    raise Invalid(f"{path} is not in the source." + (" Similar: " + ", ".join(near) if near else ""))
                text = archive.read(member).decode("utf-8", "replace")
                offset = max(0, int(offset or 0))
                return {"path": path, "size": member.file_size, "offset": offset, "text": text[offset:offset + MAX_TEXT],
                        "truncated": len(text) > offset + MAX_TEXT}
            if not pattern:
                names = [n for n in members if not path_filter or re.search(path_filter, n)]
                return {"files": names[:2000], "total": len(names)}
            try:
                expression = re.compile(pattern, re.I)
                scope = re.compile(path_filter) if path_filter else None
            except re.error as error:
                raise Invalid(f"Invalid regular expression: {error}") from None
            hits = []
            for name, member in members.items():
                if (scope and not scope.search(name)) or member.file_size > 2_000_000 or drafting.VENDORED.search(name):
                    continue
                for number, line in enumerate(archive.read(member).decode("utf-8", "replace").splitlines(), 1):
                    if expression.search(line):
                        hits.append({"path": name, "line": number, "text": line[:300]})
                        if len(hits) >= 200:
                            return {"matches": hits, "truncated": True}
            return {"matches": hits, "truncated": False}

    def tool_list_release_archive(self, game_id, asset):
        capture = self._capture(self._project(game_id))
        entry = next((a for a in capture.get("assets", []) if a["name"] == asset), None)
        if not entry:
            raise Invalid(f"{asset} is not a preserved asset of {capture['version']}")
        path = Path(capture["root"]) / entry["file"]
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                files = [{"path": m.filename, "size": m.file_size} for m in archive.infolist()]
        else:
            import tarfile
            if not tarfile.is_tarfile(path):
                return {"asset": asset, "single_file": True, "size": path.stat().st_size}
            with tarfile.open(path) as archive:
                files = [{"path": m.name.removeprefix("./"), "size": m.size} for m in archive]
        return {"asset": asset, "count": len(files), "files": files[:5000], "truncated": len(files) > 5000}

    def tool_validate_definition(self, game_id, definition):
        project = self._project(game_id)
        try:
            checked = self.check(project, definition, self._capture(project))
        except Invalid as error:
            return {"valid": False, "problem": str(error)}
        return {"valid": True, "steps": packaging.view(checked), "warnings": drafting.warnings(checked),
                "game_files": self._bindings(project, checked)}

    def tool_test_build_draft(self, game_id, definition, targets=None, source="model"):
        trial = self.start_trial(game_id, definition, targets, source)
        return {"test_id": trial["id"], "status": trial["status"], "note": "Poll get_test_build with this test_id."}

    def tool_get_test_build(self, test_id):
        try:
            trial = self.store.get("trials", str(test_id))
        except KeyError:
            raise Invalid("Unknown test id") from None
        return {key: trial.get(key) for key in ("id", "project_id", "status", "targets", "result", "error", "started_at", "finished_at")} | {"log": trial.get("logs", [])[-80:]}

    def tool_submit_draft(self, game_id, definition, explanation, agent="Outside agent"):
        project = self._project(game_id)
        capture = self._capture(project)
        checked = self.check(project, definition, capture)
        tests = [t for t in self.store.all("trials") if t["project_id"] == game_id and t.get("definition_sha256") == _sha(checked)]
        label = re.sub(r"[^\w .:/@·()-]", "", str(agent))[:80] or "Outside agent"
        details = {"model_tests": [{"id": t["id"], "status": t["status"], "finished_at": t.get("finished_at")} for t in tests[:5]]}
        proposal = self.engine.save_definition_proposal(project, checked, str(explanation)[:8000], label + " (connected agent)", capture, draft_details=details)
        self.engine.notify(game_id, "ready", f"A packaging draft for {project['name']} from {label} is ready for your review", event_key="draft:" + proposal["id"])
        return {"submitted": True, "proposal_id": proposal["id"], "tested": [t["status"] for t in tests[:5]],
                "note": "The person reviews and approves it in Portkeeper. It is not used until then."}

    # Test builds

    def start_trial(self, game_id, definition, targets=None, source="model", background=True, allow_selected_inputs=True, capture_id=None, capture_snapshot=None):
        with self.engine.lock:
            project = self._project(game_id)
            capture = self._capture(project)
            if capture_id is not None:
                capture = self.store.get('captures', capture_id)
                if capture['project_id'] != game_id:
                    raise Invalid('The selected capture belongs to another game')
            if capture_snapshot is not None:
                if capture_snapshot['id'] != capture['id'] or capture_snapshot['project_id'] != game_id or capture_snapshot['source'] != capture['source'] or capture_snapshot['root'] != capture['root']:
                    raise Invalid('The investigation runtime snapshot does not match this preserved source')
                capture = capture_snapshot
            if not capture.get("complete", True):
                raise Invalid("The preserved release is incomplete; the person must check upstream again")
            checked = self.check(project, definition, capture)
            chosen = targets or checked["targets"]
            if not isinstance(chosen, list) or not chosen or any(t not in checked["targets"] for t in chosen):
                raise Invalid("targets must be platforms in the definition")
            if any(t["project_id"] == game_id and t["status"] in ("queued", "running") for t in self.store.all("trials")):
                raise Invalid("A test build for this game is already running; poll get_test_build")
            trial = self.store.put("trials", {"id": identifier(), "project_id": game_id, "status": "queued", "targets": chosen, "source": str(source)[:80],
                                              "definition_sha256": _sha(checked), "definition": checked, "capture_id": capture['id'], "capture_snapshot": capture_snapshot, "allow_selected_inputs": allow_selected_inputs, "started_at": now(), "logs": []})
        if not background:
            return self.run_trial(trial["id"])
        self.engine.executor.submit(self.run_trial, trial["id"])
        return trial

    def run_trial(self, trial_id):
        trial = self.store.update("trials", trial_id, status="running")
        def log(message):
            with self.store.lock:
                record = self.store.get("trials", trial_id)
                record["logs"] = (record.get("logs") or [])[-400:] + [f"{now()}  {str(message)[:1500]}"]
                self.store.put("trials", record)
        try:
            project = self.store.get("projects", trial["project_id"])
            if not trial.get('allow_selected_inputs', True):
                project = {**project, 'input_bindings': {}}
            capture = trial.get('capture_snapshot') or self.store.get("captures", trial.get('capture_id') or project["capture_id"])
            review = packaging.evaluate_release(trial["definition"], capture, self.engine.input_hashes(project), targets=trial["targets"])
            if review.get("problems"):
                log("Release review: " + "; ".join(review["problems"]))
            result = self.engine.packager.trial(project, trial["definition"], capture, trial["targets"], log)
            result["release_review"] = {"status": review.get("status"), "problems": review.get("problems", [])}
            self.store.update("trials", trial_id, status=result["status"], result=result, finished_at=now())
        except Exception as error:
            log(str(error))
            self.store.update("trials", trial_id, status="failed", error=str(error), finished_at=now())
        return self.store.get("trials", trial_id)

    def trial_feedback(self, trial):
        """Plain feedback for a model, or None when nothing needs fixing."""
        result = trial.get("result") or {}
        if trial["status"] == "passed" or (trial["status"] == "partial" and not any(r.get("missing") for r in result.get("targets", {}).values())):
            return None
        lines = [f"Portkeeper test-built your definition: {trial['status']}."]
        if trial.get("error") or result.get("error"):
            lines.append("Error: " + (trial.get("error") or result.get("error")))
        for target, item in result.get("targets", {}).items():
            if item["status"] == "failed":
                lines.append(f"{target}: {item.get('error')}")
                if item.get("top_level"):
                    lines.append(f"{target} package top-level entries: {', '.join(item['top_level'])}")
                if item.get("sample_files"):
                    lines.append(f"{target} package files (first {len(item['sample_files'][:60])}): " + ", ".join(item["sample_files"][:60]))
        lines += [line.split("  ", 1)[-1] for line in trial.get("logs", [])[-10:]]
        return "\n".join(lines)[:12000]


def _sha(definition):
    import hashlib
    body = copy.deepcopy(definition)
    body.pop("history", None)
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
