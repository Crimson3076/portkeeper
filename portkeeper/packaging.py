"""Packaging definitions: the editable source of truth for how a project becomes a download.

A definition (schema 2) documents upstream release rules, selected downloads, required
user-provided files, build operations, platform outputs, launchers, saves, dependencies,
checksums and limitations. The UI's readable instructions are generated from it.

Operations and tools form a fixed vocabulary interpreted by Portkeeper. A definition can
select and parameterize them; it cannot add shell commands. A definition that needs an
operation, tool or runner version this build does not know is reported as requiring a
Portkeeper update instead of being run.
"""
import copy
import json
import re
import tarfile
import zipfile
from pathlib import Path

from .safety import Invalid, TARGETS, relative, validate_recipe

SCHEMA = 2
RUNNER_VERSION = 1
RUNNERS = ("operations", "profile", "recipe", "capture")
OPERATIONS = ("unpack_download", "stage_input", "tool", "copy", "include_tree", "launcher", "helper", "write_text", "touch")
TOOLS = ("ship_o2r_headless", "opengoal_extract", "ps4_pkg_extract", "wwhd_extract", "torch_o2r")
HELPERS = ("bbport_portable_paths", "bbport_nvidia_libraries")
NORMALIZERS = ("n64_big_endian",)
# How a package handles what the game would otherwise ask for on its first launch.
FIRST_RUN = {"ready": "Nothing to set up on first launch.",
             "preloaded": "Your game file is placed where the game looks for it, so it doesn't ask on first launch.",
             "generated": "Files the game needs are generated from your game file during the build.",
             "configured": "The package's settings already point the game at its data.",
             "asks_user": "The game asks for something on first launch; Portkeeper can't do it in advance."}
GIB = 1024 ** 3
MAX_INPUT_BYTES = 64 * GIB
MAX_PACKAGE_BYTES = 96 * GIB
BUNDLED = Path(__file__).resolve().parent.parent / "catalog" / "definitions"

_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_SHA1 = re.compile(r"[a-f0-9]{40}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
# Launcher values are written into shell/cmd scripts; keep them to plain path-like text.
_SAFE_ARG = re.compile(r"[A-Za-z0-9_.,:=+@/\\ -]{0,200}")
_TOKENS = {"posix": ("$PWD",), "windows_cmd": ("%CD%", "%~dp0")}


def _safe_value(value, template):
    if not isinstance(value, str):
        return False
    for token in _TOKENS[template]:
        value = value.replace(token, "")
    # A trailing backslash would escape the closing quote in sh.
    return bool(_SAFE_ARG.fullmatch(value)) and not (template == "posix" and "\\" in value)
_PLACEHOLDER = re.compile(r"\{(version|target|name|repository|launch_command)\}")


class NeedsUpdate(Invalid):
    """The definition is well formed but needs runner support this installation lacks."""


def _text(value, field, limit=4000, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit or "\x00" in value:
        raise Invalid(f"{field} must be text up to {limit} characters")
    return value


def _https(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"https://[^\s\"<>]{1,500}", value):
        raise Invalid(f"{field} must be an https:// link")
    return value


def _regex(value, field):
    if not isinstance(value, str) or not value or len(value) > 300:
        raise Invalid(f"{field} must be a regular expression up to 300 characters")
    try:
        re.compile(value)
    except re.error as error:
        raise Invalid(f"{field} is not a valid regular expression: {error}") from error
    return value


def _list(value, field, limit=100):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise Invalid(f"{field} must be a list of at most {limit} items")
    return value


def _path(value, field):
    try:
        return relative(value)
    except Invalid as error:
        raise Invalid(f"{field}: {error}. Use forward slashes and a relative filename, such as game/eboot.bin; omit optional empty path fields.") from None


def _literal_path(value, field):
    path = _path(value, field)
    if "*" in value or "?" in value:
        raise Invalid(f"{field}: {value!r} must be a literal path, not a wildcard. Use an inspected filename or a directory such as bin/; glob patterns are not expanded.")
    return path


def split_root(value, field, roots=("package", "shared")):
    """Return (root, relative path) for $package/... and $shared/... locations."""
    if not isinstance(value, str) or not value.startswith("$"):
        raise Invalid(f"{field} must start with $package or $shared")
    root, _, rest = value[1:].partition("/")
    if root not in roots:
        raise Invalid(f"{field} must start with " + " or ".join("$" + r for r in roots))
    if rest:
        _path(rest, field)
    return root, rest


def _check_operation(op, definition, index):
    where = f"Operation {index + 1}"
    if not isinstance(op, dict) or not isinstance(op.get("op"), str):
        raise Invalid(f"{where} must name an operation")
    if op["op"] not in OPERATIONS:
        raise NeedsUpdate(f"{where} uses operation {op['op']!r}, which needs a newer Portkeeper runner")
    kind = op["op"]
    allowed = {"op", "targets", "description"}
    targets = op.get("targets")
    if targets is not None and (not isinstance(targets, list) or not targets or any(t not in definition["targets"] for t in targets)):
        raise Invalid(f"{where} targets must be a subset of the definition's targets")
    _text(op.get("description"), f"{where} description", 500)
    downloads, inputs = definition.get("downloads", {}), {item["id"]: item for item in definition.get("inputs", [])}
    if kind == "unpack_download":
        allowed |= {"download", "to", "file_name"}
        if op.get("download") not in downloads:
            raise Invalid(f"{where} refers to an unknown download")
        split_root(op.get("to"), f"{where} destination")
        if "file_name" in op:
            _path(op["file_name"], f"{where} file_name")
    elif kind == "stage_input":
        allowed |= {"input", "to"}
        if op.get("input") not in inputs or inputs[op["input"]]["kind"] != "file":
            raise Invalid(f"{where} input must stage a declared file input (IDs: " + ", ".join(key for key, item in inputs.items() if item["kind"] == "file") + "). stage_input copies one file, not a directory. For a launcher that asks for a game folder, omit this step, use first_run.mode: asks_user, and describe choosing the folder; do not invent a directory-copy operation.")
        split_root(op.get("to"), f"{where} destination")
    elif kind == "copy":
        allowed |= {"from", "to"}
        split_root(op.get("from"), f"{where} source")
        split_root(op.get("to"), f"{where} destination")
    elif kind == "include_tree":
        allowed |= {"from", "as"}
        split_root(op.get("from"), f"{where} source", roots=("shared",))
        _path(op.get("as"), f"{where} as")
    elif kind == "launcher":
        allowed |= {"path", "template", "exec", "args", "env", "mkdir", "run_python", "chmod_exec", "prepare_once"}
        _path(op.get("path"), f"{where} path")
        if op.get("template") not in ("posix", "windows_cmd"):
            raise Invalid(f"{where} launcher template must be posix or windows_cmd")
        _path(op.get("exec"), f"{where} exec")
        for arg in _list(op.get("args"), f"{where} args", 32):
            if not _safe_value(arg, op["template"]):
                raise Invalid(f"{where} launcher arguments may contain only letters, digits and path characters")
        env = op.get("env", {})
        if not isinstance(env, dict) or len(env) > 32 or any(not isinstance(k, str) or not _ENV_NAME.fullmatch(k) or not _safe_value(v, op["template"]) for k, v in env.items()):
            raise Invalid(f"{where} launcher environment must map NAMES to plain path-like values")
        for path in _list(op.get("mkdir"), f"{where} mkdir", 32):
            _path(path, f"{where} mkdir")
        for script in _list(op.get("run_python"), f"{where} run_python", 8):
            if not isinstance(script, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.py", script):
                raise Invalid(f"{where} may only run Python helper scripts in the package folder")
        if "chmod_exec" in op and not isinstance(op["chmod_exec"], bool):
            raise Invalid(f"{where} chmod_exec must be true or false")
        prepare = op.get("prepare_once")
        if prepare is not None:
            if not isinstance(prepare, dict) or set(prepare) - {"exec", "args", "unless_exists"} or not prepare.get("exec") or not prepare.get("unless_exists"):
                raise Invalid(f"{where} prepare_once needs exec, unless_exists and optional args")
            _path(prepare["exec"], f"{where} prepare_once.exec")
            _literal_path(prepare["unless_exists"], f"{where} prepare_once.unless_exists")
            for arg in _list(prepare.get("args"), f"{where} prepare_once.args", 32):
                if not _safe_value(arg, op["template"]):
                    raise Invalid(f"{where} prepare_once arguments may contain only letters, digits and path characters")
    elif kind == "helper":
        allowed |= {"name", "to"}
        if op.get("name") not in HELPERS:
            raise NeedsUpdate(f"{where} uses helper {op.get('name')!r}, which needs a newer Portkeeper runner")
        split_root(op.get("to"), f"{where} destination", roots=("package",))
    elif kind == "write_text":
        allowed |= {"path", "text"}
        split_root(op.get("path"), f"{where} path")
        _text(op.get("text"), f"{where} text", 65536)
    elif kind == "touch":
        allowed |= {"path"}
        split_root(op.get("path"), f"{where} path")
    elif kind == "tool":
        allowed |= _check_tool(op, definition, inputs, downloads, where)
    unknown = set(op) - allowed
    if unknown:
        raise Invalid(f"{where} has unsupported fields: " + ", ".join(sorted(unknown)))
    return op


def _check_tool(op, definition, inputs, downloads, where):
    tool = op.get("tool")
    if tool not in TOOLS:
        raise NeedsUpdate(f"{where} uses tool {tool!r}, which needs a newer Portkeeper runner")
    timeout = op.get("timeout_minutes", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 240:
        raise Invalid(f"{where} timeout_minutes must be 1–240")
    if split_root(op.get("to"), f"{where} destination")[0] != "shared":
        raise Invalid(f"{where} tools write to $shared")
    if tool == "torch_o2r":
        if op.get("download") not in downloads:
            raise Invalid(f"{where} refers to an unknown configuration download")
        if split_root(op.get("rom"), f"{where} ROM")[0] != "shared":
            raise Invalid(f"{where} ROM must be staged under $shared")
        if not isinstance(op.get("output"), str) or not re.fullmatch(r"[a-z0-9_]+\.o2r", op["output"]):
            raise Invalid(f"{where} output must be an .o2r archive name")
        entries = op.get("min_entries", 1)
        if isinstance(entries, bool) or not isinstance(entries, int) or entries < 1:
            raise Invalid(f"{where} min_entries must be positive")
        return {"tool", "download", "rom", "output", "to", "min_entries", "timeout_minutes"}
    if tool == "ship_o2r_headless":
        if op.get("download") not in downloads:
            raise Invalid(f"{where} refers to an unknown download")
        _path(op.get("appimage"), f"{where} appimage")
        if split_root(op.get("rom"), f"{where} ROM")[0] != "shared":
            raise Invalid(f"{where} ROM must be staged under $shared")
        if not isinstance(op.get("output"), str) or not re.fullmatch(r"[a-z0-9_]+\.o2r", op["output"]):
            raise Invalid(f"{where} output must be an .o2r archive name")
        entries = op.get("min_entries", 1)
        if isinstance(entries, bool) or not isinstance(entries, int) or entries < 1:
            raise Invalid(f"{where} min_entries must be a positive integer")
        return {"tool", "download", "appimage", "rom", "output", "to", "min_entries", "timeout_minutes"}
    if tool == "opengoal_extract":
        if op.get("download") not in downloads:
            raise Invalid(f"{where} refers to an unknown download")
        if op.get("game") not in ("jak1", "jak2", "jak3"):
            raise Invalid(f"{where} game must be jak1, jak2 or jak3")
        if op.get("iso") not in inputs or inputs[op["iso"]]["kind"] != "file":
            raise Invalid(f"{where} iso must be a declared file input")
        collect = _list(op.get("collect"), f"{where} collect", 16)
        if not collect:
            raise Invalid(f"{where} must collect at least one output directory")
        for path in collect:
            _path(path, f"{where} collect")
        return {"tool", "download", "game", "iso", "collect", "to", "timeout_minutes"}
    if tool == "wwhd_extract":
        if op.get("download") not in downloads:
            raise Invalid(f"{where} refers to an unknown download")
        _path(op.get("extractor"), f"{where} extractor")
        if op.get("archive") not in inputs or inputs[op["archive"]]["kind"] != "file":
            raise Invalid(f"{where} archive must be a declared file input (the person's .wua)")
        titles = _list(op.get("titles"), f"{where} titles", 8)
        if not titles or any(not isinstance(t, str) or not re.fullmatch(r"[0-9a-f]{16}", t) for t in titles):
            raise Invalid(f"{where} titles must be 16-digit lowercase Wii U title IDs")
        accepted = _list(op.get("accepted_rpx_sha256"), f"{where} accepted_rpx_sha256", 16)
        if not accepted or any(not isinstance(v, str) or not _SHA256.fullmatch(v) for v in accepted):
            raise Invalid(f"{where} needs at least one accepted code/cking.rpx SHA-256")
        prepared = op.get("prepared_input")
        if prepared is not None and (prepared not in inputs or inputs[prepared]["kind"] != "directory"):
            raise Invalid(f"{where} prepared_input must be a declared directory input")
        return {"tool", "download", "extractor", "archive", "prepared_input", "titles", "accepted_rpx_sha256", "to", "timeout_minutes"}
    # ps4_pkg_extract
    if op.get("tool_download") not in downloads or not downloads[op["tool_download"]].get("sha256"):
        raise Invalid(f"{where} must use a pinned PkgTool download")
    packages = _list(op.get("packages"), f"{where} packages", 8)
    if not packages or any(not isinstance(p, str) or p not in inputs or inputs[p]["kind"] != "file" for p in packages) or len(set(packages)) != len(packages):
        raise Invalid(f"{where} packages must be declared file inputs, base game first")
    if inputs[packages[0]].get('required', True) is False:
        raise Invalid(f'{where} the base game PKG must be required; later update PKGs may be optional for an already updated dump')
    prepared = op.get("prepared_input")
    if prepared is not None and (prepared not in inputs or inputs[prepared]["kind"] != "directory"):
        raise Invalid(f"{where} prepared_input must be a declared directory input")
    if not isinstance(op.get("title_id"), str) or not re.fullmatch(r"[A-Z]{4}[0-9]{5}", op["title_id"]):
        raise Invalid(f"{where} title_id must look like CUSA00900")
    if not isinstance(op.get("app_version"), str) or not re.fullmatch(r"[0-9]{2}\.[0-9]{2}", op["app_version"]):
        raise Invalid(f"{where} app_version must look like 01.09")
    accepted = _list(op.get("accepted_eboot_sha256"), f"{where} accepted_eboot_sha256", 16)
    if not accepted or any(not isinstance(v, str) or not _SHA256.fullmatch(v) for v in accepted):
        raise Invalid(f"{where} needs at least one accepted eboot.bin SHA-256")
    return {"tool", "tool_download", "packages", "prepared_input", "title_id", "app_version", "accepted_eboot_sha256", "to", "timeout_minutes"}


def validate_definition(definition):
    """Validate and return a detached copy. Unknown fields are rejected."""
    if not isinstance(definition, dict) or definition.get("schema") != SCHEMA:
        raise Invalid("Packaging definition must be an object with schema: 2")
    d = copy.deepcopy(definition)
    fields = {"schema", "id", "revision", "name", "game", "summary", "runner", "runner_version", "upstream", "targets",
              "downloads", "inputs", "source_checks", "operations", "outputs", "launch", "saves", "dependencies",
              "limitations", "verified_releases", "profile", "recipe", "notes", "history", "package_prefix", "legacy_recipe",
              "first_run"}
    unknown = set(d) - fields
    if unknown:
        raise Invalid("Packaging definition has unsupported fields: " + ", ".join(sorted(unknown)))
    if not isinstance(d.get("id"), str) or not _ID.fullmatch(d["id"]):
        raise Invalid("Definition id must be lowercase letters, digits, dot, dash or underscore")
    revision = d.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= 100000:
        raise Invalid("Definition revision must be a positive integer")
    _text(d.get("name"), "Definition name", 100, required=True)
    for key in ("game", "summary", "saves", "notes"):
        _text(d.get(key), key.capitalize(), 4000)
    if d.get("package_prefix") is not None and (not isinstance(d["package_prefix"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", d["package_prefix"])):
        raise Invalid("package_prefix must be a filename prefix such as 2-Ship-2-Harkinian")
    first_run = d.get("first_run")
    if first_run is not None:
        if not isinstance(first_run, dict) or set(first_run) - {"mode", "summary"} or first_run.get("mode") not in FIRST_RUN:
            raise Invalid("first_run needs a mode (" + ", ".join(FIRST_RUN) + ") and an optional summary")
        _text(first_run.get("summary"), "first_run.summary", 1000)
    if d.get("runner") not in RUNNERS:
        raise Invalid("Definition runner must be operations, profile, recipe or capture")
    version = d.get("runner_version", RUNNER_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise Invalid("runner_version must be a positive integer")
    if version > RUNNER_VERSION:
        raise NeedsUpdate(f"This definition needs Portkeeper runner version {version}; this installation provides {RUNNER_VERSION}. Update Portkeeper first.")
    upstream = d.get("upstream")
    if not isinstance(upstream, dict) or set(upstream) - {"repository", "documentation", "release_rules"}:
        raise Invalid("upstream must describe repository, documentation and release_rules")
    if not isinstance(upstream.get("repository"), str) or not _REPO.fullmatch(upstream["repository"]):
        raise Invalid("upstream.repository must be owner/repo")
    for link in _list(upstream.get("documentation"), "upstream.documentation", 20):
        _https(link, "Documentation link")
    rules = upstream.get("release_rules", {})
    if not isinstance(rules, dict) or set(rules) - {"stable_only", "tag_pattern", "notes"}:
        raise Invalid("release_rules supports stable_only, tag_pattern and notes")
    if "stable_only" in rules and not isinstance(rules["stable_only"], bool):
        raise Invalid("release_rules.stable_only must be true or false")
    if "tag_pattern" in rules:
        _regex(rules["tag_pattern"], "release_rules.tag_pattern")
    _text(rules.get("notes"), "release_rules.notes", 2000)
    targets = d.get("targets")
    if not isinstance(targets, list) or not targets or len(set(targets)) != len(targets) or any(t not in TARGETS for t in targets):
        raise Invalid("Definition targets must be unique: linux, windows, handheld")
    downloads = d.setdefault("downloads", {})
    if not isinstance(downloads, dict) or len(downloads) > 20:
        raise Invalid("downloads must map up to 20 download IDs")
    for key, item in downloads.items():
        if not isinstance(key, str) or not _ID.fullmatch(key) or not isinstance(item, dict):
            raise Invalid("Each download needs a lowercase ID and an object")
        if set(item) - {"asset_pattern", "platform", "format", "required_members", "strip_prefix", "repository", "tag", "asset", "sha256", "description"}:
            raise Invalid(f"Download {key} has unsupported fields")
        if item.get("repository"):
            if not _REPO.fullmatch(str(item["repository"])) or not isinstance(item.get("tag"), str) or not isinstance(item.get("asset"), str) or not _SHA256.fullmatch(str(item.get("sha256", ""))):
                raise Invalid(f"Download {key} from another repository must pin repository, tag, asset and sha256")
            _path(item["asset"], f"downloads.{key}.asset")
        else:
            _regex(item.get("asset_pattern"), f"Download {key} asset_pattern")
        if item.get("platform", "any") not in (*TARGETS, "any"):
            raise Invalid(f"Download {key} platform must be a target or any")
        if item.get("format", "auto") not in ("auto", "zip", "tar", "file"):
            raise Invalid(f"Download {key} format must be auto, zip, tar or file")
        members = item.get("required_members")
        if members is not None and (not isinstance(members, list) or len(members) > 50):
            received = f"{len(members)} items" if isinstance(members, list) else type(members).__name__
            raise Invalid(f"downloads.{key}.required_members must be a JSON array with at most 50 items (received {received}). Keep only 2-10 essential file checks, not the whole archive listing. All archive files are extracted regardless of required_members.")
        for member in _list(members, f"Download {key} required_members", 50):
            _literal_path(member, f"downloads.{key}.required_members")
        if item.get("strip_prefix") is not None:
            _path(item["strip_prefix"], f"downloads.{key}.strip_prefix")
        _text(item.get("description"), f"Download {key} description", 500)
    inputs = _list(d.setdefault("inputs", []), "inputs", 20)
    seen = set()
    for item in inputs:
        if not isinstance(item, dict) or set(item) - {"id", "label", "kind", "platform_hint", "name_hint", "normalize", "accepted", "verify", "max_bytes", "notes", "required"}:
            raise Invalid("Each input needs id, label, kind and validation fields only")
        if not isinstance(item.get("id"), str) or not _ID.fullmatch(item["id"]) or item["id"] in seen:
            raise Invalid("Input IDs must be unique lowercase identifiers")
        seen.add(item["id"])
        _text(item.get("label"), "Input label", 200, required=True)
        if item.get("kind") not in ("file", "directory"):
            raise Invalid("Input kind must be file or directory")
        if item.get("normalize") is not None and item["normalize"] not in NORMALIZERS:
            raise Invalid("Input normalize must be n64_big_endian")
        if item.get("platform_hint") is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", str(item["platform_hint"])):
            raise Invalid("Input platform_hint must be a RomM platform folder name")
        if item.get("name_hint") is not None:
            _regex(item["name_hint"], "Input name_hint")
        if item.get("verify", "hash") not in ("hash", "downstream"):
            raise Invalid("Input verify must be hash or downstream")
        accepted = _list(item.get("accepted"), "Input accepted", 50)
        for entry in accepted:
            if not isinstance(entry, dict) or set(entry) - {"label", "sha1", "sha256"} or not (_SHA1.fullmatch(str(entry.get("sha1", ""))) or _SHA256.fullmatch(str(entry.get("sha256", "")))):
                raise Invalid("Accepted inputs need a label and lowercase sha1 or sha256")
        if item["kind"] == "file" and item.get("verify", "hash") == "hash" and not accepted:
            raise Invalid(f"Input {item['id']} needs accepted hashes, or verify: downstream with a later validation step")
        size = item.get("max_bytes", 1 * GIB)
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_INPUT_BYTES:
            raise Invalid("Input max_bytes must be at most 64 GiB")
        if "required" in item and not isinstance(item["required"], bool):
            raise Invalid("Input required must be true or false")
        _text(item.get("notes"), "Input notes", 2000)
    for check in _list(d.setdefault("source_checks", []), "source_checks", 20):
        if not isinstance(check, dict) or set(check) - {"description", "path", "contains", "contains_input_hash"}:
            raise Invalid("Source checks need description, path and contains or contains_input_hash")
        _text(check.get("description"), "Source check description", 500, required=True)
        _path(check.get("path"), "source_checks.path")
        if ("contains" in check) == ("contains_input_hash" in check):
            raise Invalid("Each source check needs exactly one of contains or contains_input_hash")
        if "contains" in check:
            _text(check["contains"], "Source check text", 500, required=True)
        elif check["contains_input_hash"] not in seen:
            raise Invalid("contains_input_hash must refer to a declared input")
    outputs = d.setdefault("outputs", {})
    if not isinstance(outputs, dict) or any(t not in targets for t in outputs):
        raise Invalid("outputs must be keyed by the definition's targets")
    for target, output in outputs.items():
        if not isinstance(output, dict) or set(output) - {"expected_files", "compression", "max_bytes"}:
            raise Invalid(f"Output {target} supports expected_files, compression and max_bytes")
        for path in _list(output.get("expected_files"), f"Output {target} expected_files"):
            _literal_path(path, f"outputs.{target}.expected_files")
        if output.get("compression", "deflate") not in ("deflate", "stored"):
            raise Invalid("Output compression must be deflate or stored")
        size = output.get("max_bytes", 2 * GIB)
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_PACKAGE_BYTES:
            raise Invalid("Output max_bytes must be at most 96 GiB")
    for key in ("launch", "dependencies"):
        value = d.setdefault(key, {})
        if not isinstance(value, dict) or any(t not in targets for t in value):
            raise Invalid(f"{key} must be keyed by the definition's targets")
    for target, launch in d["launch"].items():
        if not isinstance(launch, dict) or set(launch) - {"command", "instructions"}:
            raise Invalid("Launch entries support command and instructions")
        if launch.get("command") is not None:
            _path(launch["command"], f"launch.{target}.command")
        _text(launch.get("instructions"), "Launch instructions", 2000)
    for target, items in d["dependencies"].items():
        for item in _list(items, f"Dependencies for {target}", 30):
            _text(item, "Dependency", 300, required=True)
    for item in _list(d.setdefault("limitations", []), "limitations", 40):
        _text(item, "Limitation", 1000, required=True)
    for item in _list(d.setdefault("history", []), "history", 40):
        _text(item, "History note", 2000, required=True)
    for release in _list(d.setdefault("verified_releases", []), "verified_releases", 50):
        if not isinstance(release, dict) or set(release) - {"release", "commit", "assets", "evidence"} or not isinstance(release.get("release"), str):
            raise Invalid("verified_releases entries need release, optional commit, assets and evidence")
        if release.get("commit") is not None and not re.fullmatch(r"[a-f0-9]{40}", str(release["commit"])):
            raise Invalid("verified release commit must be a 40-character SHA")
        assets = release.get("assets", {})
        if not isinstance(assets, dict) or any(not isinstance(k, str) or not _SHA256.fullmatch(str(v)) for k, v in assets.items()):
            raise Invalid("verified release assets map names to SHA-256 values")
        _text(release.get("evidence"), "Verified release evidence", 2000)
    runner = d["runner"]
    operations = _list(d.setdefault("operations", []), "operations", 100)
    if runner == "operations":
        if not operations:
            raise Invalid("An operations definition needs at least one operation")
        for index, op in enumerate(operations):
            _check_operation(op, d, index)
        prepared = {op.get("prepared_input") for op in operations if op["op"] == "tool" and op.get("tool") in ("ps4_pkg_extract", "wwhd_extract")}
        for item in inputs:
            if item["kind"] == "directory" and item["id"] not in prepared:
                raise Invalid(f"inputs.{item['id']}: directory inputs must be consumed as prepared_input by ps4_pkg_extract or wwhd_extract. For a game folder chosen on the player's PC, remove this input, use first_run.mode asks_user, and describe folder selection. Packaging does not need that folder on the server.")
        for target in targets:
            if not outputs.get(target, {}).get("expected_files"):
                raise Invalid(f"Declare expected files for the {target} package")
    elif operations:
        raise Invalid("Only the operations runner accepts operations")
    if runner == "profile":
        from .profiles import profile
        profile(d.get("profile"))
    elif d.get("profile") is not None:
        raise Invalid("Only the profile runner names a profile")
    if runner == "recipe":
        recipe = validate_recipe(d.get("recipe"))
        if recipe["mode"] in ("capture", "profile", "definition"):
            raise Invalid("The recipe runner wraps an assemble or Docker recipe")
    elif d.get("recipe") is not None:
        raise Invalid("Only the recipe runner embeds a legacy recipe")
    if d.get("legacy_recipe") is not None:
        validate_recipe(d["legacy_recipe"])
    return d


def requirements(definition):
    """Explain whether this installation can run the definition without an update."""
    try:
        validate_definition(definition)
        return {"supported": True, "reason": None, "needs_update": False}
    except NeedsUpdate as error:
        return {"supported": False, "reason": str(error), "needs_update": True}
    except Invalid as error:
        return {"supported": False, "reason": str(error), "needs_update": False}


def builds(definition):
    return definition["runner"] != "capture"


def bundled():
    """Definitions shipped with this image, keyed by id."""
    result = {}
    if BUNDLED.is_dir():
        for path in sorted(BUNDLED.glob("*.json")):
            definition = validate_definition(json.loads(path.read_text(encoding="utf-8")))
            result[definition["id"]] = definition
    return result


def for_repository(repo, definitions=None):
    matches = [d for d in (definitions or bundled()).values() if d["upstream"]["repository"].lower() == str(repo).lower()]
    return matches[0] if len(matches) == 1 else None


def capture_definition(project, notes=None):
    return {"schema": SCHEMA, "id": re.sub(r"[^a-z0-9_.-]+", "-", project["repo"].lower()).strip("-.")[:64] or "project",
            "revision": 1, "name": project["name"], "runner": "capture", "runner_version": RUNNER_VERSION,
            "upstream": {"repository": project["repo"], "documentation": [f"https://github.com/{project['repo']}#readme"],
                         "release_rules": {"stable_only": True}},
            "targets": list(project.get("targets") or ["linux"]),
            "summary": "Preserves upstream source and selected release files. No packaging steps are defined yet.",
            "notes": notes, "downloads": {}, "inputs": [], "outputs": {}, "launch": {}, "dependencies": {},
            "limitations": ["No packaging operations are defined. A release is preserved only; no download is produced."]}


def migrate(project, definitions=None):
    """Create a definition for a project saved before schema 2. Existing behavior is kept.

    Capture-only projects whose repository has a bundled operations definition adopt it,
    which makes them buildable; automatic build and publication settings are not changed.
    The previous recipe is kept in legacy_recipe for audit.
    """
    definitions = definitions if definitions is not None else bundled()
    recipe = project.get("recipe") or {"version": 1, "mode": "capture", "payload": {"kind": "source"}}
    notes = recipe.get("notes")
    if project.get("profile"):
        base = copy.deepcopy(definitions.get(project["profile"])) if project["profile"] in definitions else None
        if base is None:
            base = capture_definition(project)
            base.update(runner="profile", profile=project["profile"], limitations=[])
        return validate_definition(base)
    known = for_repository(project["repo"], definitions)
    if recipe.get("mode") == "capture":
        if known and known["runner"] == "operations":
            adopted = copy.deepcopy(known)
            if notes:
                adopted["history"] = adopted.get("history", []) + ["Before automation: " + notes]
            adopted["legacy_recipe"] = copy.deepcopy(recipe)
            return validate_definition(adopted)
        return validate_definition({**capture_definition(project, notes), "legacy_recipe": copy.deepcopy(recipe)})
    if recipe.get("mode") in ("assemble", "docker"):
        # Keep the saved recipe exactly; borrow only documentation from a known definition.
        base = capture_definition(project)
        base.update(runner="recipe", recipe=copy.deepcopy(recipe), targets=list(project["targets"]), operations=[], limitations=[],
                    summary="Saved recipe from an earlier Portkeeper version.")
        if known:
            for key in ("id", "name", "game", "summary", "package_prefix", "saves", "limitations", "history"):
                if known.get(key) is not None:
                    base[key] = copy.deepcopy(known[key])
            base["upstream"]["documentation"] = copy.deepcopy(known["upstream"].get("documentation", []))
            for key in ("launch", "dependencies"):
                base[key] = {t: v for t, v in copy.deepcopy(known.get(key, {})).items() if t in base["targets"]}
            base["history"] = base.get("history", []) + ["Uses the recipe saved before packaging definitions; the bundled definition's operations are not applied automatically."]
        return validate_definition(base)
    if recipe.get("mode") == "definition":
        raise Invalid("Project uses a packaging definition that is missing; restore it from the catalog")
    return validate_definition(capture_definition(project, notes))


def recipe_for(definition):
    """The schema 1 recipe kept beside a definition for components that read recipes."""
    if definition["runner"] == "recipe":
        return copy.deepcopy(definition["recipe"])
    if definition["runner"] == "profile":
        from .profiles import profile
        return copy.deepcopy(profile(definition["profile"])["default_recipe"])
    if definition["runner"] == "operations":
        return {"version": 1, "mode": "definition", "notes": f"Built from packaging definition {definition['id']} revision {definition['revision']}."}
    return {"version": 1, "mode": "capture", "payload": {"kind": "source"}, "notes": definition.get("notes") or "Preserve upstream releases only."}


def download_assets(definition, assets, targets=None):
    """Map definition download IDs to captured upstream assets. Pinned dependencies are excluded."""
    result, problems = {}, []
    for key, item in definition.get("downloads", {}).items():
        if item.get("repository"):
            continue
        platform = item.get("platform", "any")
        if targets is not None and platform != "any" and platform not in targets:
            continue
        matches = [a for a in assets if re.search(item["asset_pattern"], a.get("name", ""))]
        if len(matches) == 1:
            result[key] = matches[0]
        else:
            problems.append(f"Download {key} ({item['asset_pattern']}) matched {len(matches)} release files; exactly one is required")
    return result, problems


def asset_patterns(definition):
    return [item["asset_pattern"] for item in definition.get("downloads", {}).values() if not item.get("repository")]


def _members(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as bundle:
            return {name.replace("\\", "/").rstrip("/") for name in bundle.namelist()}
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as bundle:
            return {name[2:] if name.startswith("./") else name for name in (m.name.rstrip("/") for m in bundle.getmembers())}
    return None


def evaluate_release(definition, capture, input_hashes=None, targets=None):
    """Apply the definition's compatibility rules to a captured release.

    Returns status compatible, verified_release (matches a reviewed release exactly) or
    needs_review with the exact problems found. Nothing here executes upstream code.
    """
    problems, notes = [], []
    rules = definition["upstream"].get("release_rules", {})
    version = capture.get("version", "")
    if rules.get("tag_pattern") and not re.fullmatch(rules["tag_pattern"], version):
        problems.append(f"Release tag {version!r} does not match the release rule {rules['tag_pattern']!r}")
    root = Path(capture["root"])
    mapping, missing = download_assets(definition, capture.get("assets", []), targets)
    problems.extend(missing)
    verified = next((r for r in definition.get("verified_releases", []) if r["release"] == version), None)
    for key, asset in mapping.items():
        item = definition["downloads"][key]
        if verified and asset["name"] in verified.get("assets", {}) and verified["assets"][asset["name"]] != asset.get("sha256"):
            problems.append(f"{asset['name']} differs from the reviewed {version} file (SHA-256 {asset.get('sha256', 'unknown')[:12]}…)")
        required = item.get("required_members", [])
        if required:
            path = root / asset["file"]
            members = _members(path) if path.is_file() else None
            if members is None:
                problems.append(f"{asset['name']} is not a ZIP or TAR archive, so its expected files cannot be checked")
                continue
            prefix = (item.get("strip_prefix") + "/") if item.get("strip_prefix") else ""
            absent = [m for m in required if prefix + m not in members]
            if absent:
                problems.append(f"{asset['name']} no longer contains: " + ", ".join(absent))
    if definition.get("source_checks"):
        source = root / capture["source"]["file"]
        try:
            with zipfile.ZipFile(source) as bundle:
                names = bundle.namelist()
                top = names[0].split("/", 1)[0] + "/" if names and all(n.startswith(names[0].split("/", 1)[0] + "/") for n in names) else ""
                for check in definition["source_checks"]:
                    member = top + check["path"]
                    if member not in names:
                        problems.append(f"{check['description']}: {check['path']} is missing from the release source")
                        continue
                    text = bundle.read(member)[:4 * 1024 * 1024].decode("utf-8", "replace")
                    if "contains" in check:
                        if check["contains"] not in text:
                            problems.append(f"{check['description']}: {check['path']} no longer contains {check['contains']!r}")
                    else:
                        wanted = [e.get("sha1") or e.get("sha256") for e in next(i for i in definition["inputs"] if i["id"] == check["contains_input_hash"]).get("accepted", [])]
                        if (input_hashes or {}).get(check["contains_input_hash"]):
                            wanted = [input_hashes[check["contains_input_hash"]]]
                        if not any(value and value in text for value in wanted):
                            problems.append(f"{check['description']}: {check['path']} no longer lists the accepted input hash")
        except (OSError, zipfile.BadZipFile, KeyError):
            problems.append("The preserved source archive could not be read for source checks")
    if verified and not problems:
        status = "verified_release"
        notes.append(f"Matches the reviewed {version} release files.")
    elif problems:
        status = "needs_review"
    else:
        status = "compatible"
        notes.append("New release passed the compatibility rules. Packages from it start untested.")
    return {"status": status, "problems": problems, "notes": notes, "definition_id": definition["id"],
            "definition_revision": definition["revision"], "release": version}


def render_text(text, context):
    return _PLACEHOLDER.sub(lambda m: str(context.get(m.group(1), "")), text)


def _where(value):
    root, rest = split_root(value, "path")
    return ("the package" if root == "package" else "the shared work area") + (f" at {rest}" if rest else "")


def _named(value):
    root, rest = split_root(value, "path")
    return f"{rest} in " + ("the package" if root == "package" else "the shared work area")


def describe_operation(op, definition):
    """One readable sentence per operation. Kept server-side so it is tested with the schema."""
    targets = f" ({', '.join(op['targets'])})" if op.get("targets") else ""
    kind = op["op"]
    downloads = definition.get("downloads", {})
    inputs = {i["id"]: i for i in definition.get("inputs", [])}
    if op.get("description"):
        lead = op["description"].rstrip(".") + ". "
    else:
        lead = ""
    if kind == "unpack_download":
        item = downloads[op["download"]]
        what = item.get("asset") or item.get("asset_pattern")
        return lead + f"Unpack the {op['download']} download ({what}) into {_where(op['to'])}" + (f", stripping {item['strip_prefix']}/" if item.get("strip_prefix") else "") + targets + "."
    if kind == "stage_input":
        item = inputs[op["input"]]
        normal = " after converting it to big-endian .z64 byte order" if item.get("normalize") == "n64_big_endian" else ""
        return lead + f"Copy your {item['label']} to {_where(op['to'])}{normal}, then check it against the accepted hashes{targets}."
    if kind == "copy":
        return lead + f"Copy {_where(op['from'])} to {_where(op['to'])}{targets}."
    if kind == "include_tree":
        return lead + f"Add {_where(op['from'])} to the package as {op['as']}/ without making another copy{targets}."
    if kind == "launcher":
        args = " ".join(op.get("args", []))
        env = ", ".join(f"{k}={v}" for k, v in op.get("env", {}).items())
        prepare = op.get("prepare_once")
        first = f"; on a start where {prepare['unless_exists']} is missing it first runs {prepare['exec']} " + " ".join(prepare.get("args", [])) if prepare else ""
        return lead + f"Write the {op['path']} launcher, which runs {op['exec']}" + (f" {args}" if args else "") + (f" with {env}" if env else "") + first.rstrip() + targets + "."
    if kind == "helper":
        return lead + f"Add the reviewed {op['name']} helper as {_where(op['to'])}{targets}."
    if kind == "write_text":
        return lead + f"Write {_named(op['path'])}{targets}."
    if kind == "touch":
        return lead + f"Create the empty marker file {_named(op['path'])}{targets}."
    tool = op["tool"]
    if tool == "torch_o2r":
        return lead + f"Generate {op['output']} from your checked ROM with Portkeeper's pinned Torch CLI and the release's extraction configuration. Validate the resulting archive before including it; originals stay unchanged."
    if tool == "ship_o2r_headless":
        return lead + f"Run the official {op['appimage']} once under a virtual display with your ROM to generate {op['output']}; stop it after the archive is complete and contains at least {op.get('min_entries', 1)} entries."
    if tool == "opengoal_extract":
        return lead + f"Run the release's OpenGOAL extractor on your {inputs[op['iso']]['label']} to extract, validate, decompile and compile {op['game']}, then keep " + ", ".join(op["collect"]) + "."
    if tool == "wwhd_extract":
        alternative = ' Alternatively validate your selected decrypted game folder without modifying it.' if op.get('prepared_input') else ''
        return lead + f"Prepare your {inputs[op['archive']]['label']}: use the release's {op['extractor']} for WUA, or safely extract a decrypted ZIP. Require title " + " or ".join(op["titles"]) + " (version 0 only) and an accepted code/cking.rpx SHA-256. Originals stay unchanged." + alternative
    return lead + "Check and copy the PS4 packages to a private work folder, then extract the base game first and overlay its updates in order with the pinned PkgTool (" + " → ".join(inputs[p]["label"] for p in op["packages"]) + f"). Originals stay unchanged. Require title {op['title_id']}, version {op['app_version']} and an accepted eboot.bin SHA-256. Reuse a validated earlier extraction when available."


def view(definition):
    """Data the UI renders as the readable packaging instructions."""
    steps = []
    if definition["runner"] == "operations":
        steps = [{"text": describe_operation(op, definition), "op": op["op"], "targets": op.get("targets")} for op in definition["operations"]]
    elif definition["runner"] == "profile":
        steps = [{"text": "Run the fixed reviewed build profile " + definition["profile"] + ": capture pinned dependencies, compile its extractor, verify your ROM, generate assets and assemble the official runtime.", "op": "profile"}]
    elif definition["runner"] == "recipe":
        recipe = definition["recipe"]
        steps = [{"text": f"Run the saved {recipe['mode']} recipe from an earlier Portkeeper version. Expected files: {', '.join(recipe.get('expected_files', [])) or 'not recorded'}.", "op": "recipe"}]
    first_run = definition.get("first_run")
    first = {"mode": first_run["mode"], "text": first_run.get("summary") or FIRST_RUN[first_run["mode"]]} if first_run else None
    return {"runner": definition["runner"], "builds": builds(definition), "steps": steps, "requirements": requirements(definition),
            "first_run": first}
