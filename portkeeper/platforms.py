"""Evidence-based OS hints from release filenames and first-party documentation.

Hints do not prove gameplay, architecture compatibility, or packaging support. An
Android mention is never translated into the generic handheld packaging target.
"""
import re
import urllib.parse

from .safety import Invalid, relative

LABELS = {"linux": "Linux", "windows": "Windows", "android": "Android", "macos": "macOS", "ios": "iOS"}
PACKAGING = {"linux", "windows"}
WORDS = {
    "linux": re.compile(r"\blinux\b|\bsteam\s*deck\b", re.I),
    "windows": re.compile(r"\bwindows\b|\bwin(?:32|64)\b", re.I),
    "android": re.compile(r"\bandroid\b", re.I),
    "macos": re.compile(r"\bmacos\b|\bmac\b|\bmac os\b|\bos ?x\b|\bdarwin\b", re.I),
    "ios": re.compile(r"\bios\b|\biphone\b|\bipad\b", re.I),
}
ASSETS = {
    "linux": re.compile(r"linux|appimage|steam[-_]?deck|\.(?:deb|rpm)$", re.I),
    "windows": re.compile(r"windows|(?:^|[-_.])win(?:32|64)?(?:[-_.]|$)|\.(?:exe|msi)$", re.I),
    "android": re.compile(r"android|\.(?:apk|aab)$", re.I),
    "macos": re.compile(r"macos|darwin|osx|(?:^|[-_.])mac(?:[-_.]|$)|\.dmg$", re.I),
    "ios": re.compile(r"(?:^|[-_.])ios(?:[-_.]|$)|\.ipa$", re.I),
}
IGNORE_ASSET = re.compile(r"(?:^|[-_.])(?:source|src|symbols|debug)(?:[-_.]|$)|\.(?:sig|asc|sha\d+|txt)$", re.I)
DOC_LINK = re.compile(r"\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)")
DOC_PATH = re.compile(r"(?:readme|build|install|setup|platform|support|linux|windows|android|macos|ios)", re.I)
NEGATIVE = re.compile(r"not (?:yet )?supported|unsupported|not available|no support|not implemented|planned|roadmap|todo|coming soon|future support", re.I)
POSITIVE = re.compile(r"support|build|install|download|requirements?|run(?:ning)?|port\b|available|release|instructions|compil|tested|play\b", re.I)


def read_documents(github, repo, commit):
    """Read bounded first-party files at the release commit, never arbitrary URLs."""
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        return [], "The release commit is unavailable; only release files were inspected."
    documents = []
    errors = []

    def fetch(path):
        url = f"https://raw.githubusercontent.com/{repo}/{commit}/{urllib.parse.quote(path, safe='/')}"
        try:
            with github.open(url, "text/plain") as response:
                content = response.read(128_001)
            if len(content) > 128_000:
                errors.append(f"{path} exceeds the documentation size limit")
                return None
            return {"path": path, "text": content.decode("utf-8", "replace"), "url": url}
        except (Invalid, OSError) as error:
            errors.append(str(error))
            return None

    for path in ("README.md", "readme.md", "README.rst", "README"):
        document = fetch(path)
        if document:
            documents.append(document)
            break
    if not documents:
        return [], "README could not be read; detection uses release files only."
    # First-party linked build guides often document platforms absent from the README.
    candidates = []
    for link in DOC_LINK.findall(documents[0]["text"]):
        parsed = urllib.parse.urlsplit(link)
        if parsed.scheme or parsed.netloc or not DOC_PATH.search(parsed.path):
            continue
        path = urllib.parse.unquote(parsed.path).removeprefix("./")
        try:
            relative(path)
        except Invalid:
            continue
        if not path.lower().endswith((".md", ".txt", ".rst")) or path in candidates or path == documents[0]["path"]:
            continue
        candidates.append(path)
    for path in candidates[:5]:
        document = fetch(path)
        if document:
            documents.append(document)
    return documents, None


def detect(assets, documents=(), catalog_targets=()):
    records = {}

    def evidence(platform, kind, detail, source):
        record = records.setdefault(platform, {"id": platform, "label": LABELS[platform],
                                              "packaging_supported": platform in PACKAGING, "evidence": []})
        entry = {"kind": kind, "detail": detail[:240], "source": source}
        if entry not in record["evidence"] and len(record["evidence"]) < 5:
            record["evidence"].append(entry)

    for asset in assets:
        name = asset.get("name", "") if isinstance(asset, dict) else str(asset)
        if not name or IGNORE_ASSET.search(name):
            continue
        for platform, pattern in ASSETS.items():
            if pattern.search(name):
                evidence(platform, "release", name, "Release files")
    for document in documents:
        heading = ""
        document_scope = {key for key, pattern in WORDS.items() if pattern.search(document.get("path", "").replace("_", " "))}
        section_scope = set()
        for raw in document.get("text", "")[:128_000].splitlines():
            line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", raw).replace("**", "").replace("`", "").strip()
            if line.startswith("#"):
                heading = line.lstrip("# ")
                named = {key for key, pattern in WORDS.items() if pattern.search(heading)}
                if line.startswith("# ") and len(named) == 1 and not document_scope:
                    document_scope = named
                section_scope = named if len(named) == 1 or named and re.search(r"build|install|download|requirements?|support|setup|running", heading, re.I) else set()
            # Dependency credits and future plans aren't evidence of current support.
            if NEGATIVE.search(heading) or re.search(r"credits|acknowledg|roadmap|planned|future|todo", heading, re.I):
                continue
            if len(line) > 1000 or not line or NEGATIVE.search(line) or re.search(r"\|\s*(?:no|false|❌|✗)\s*\|", line, re.I):
                continue
            for platform, pattern in WORDS.items():
                mention = pattern.search(line)
                scoped = section_scope or document_scope
                comparison = bool(re.search(r"different from|compar|porting|internals|architecture|how it works|diagnostic", heading, re.I))
                platform_section = platform in scoped and not comparison
                if mention and (POSITIVE.search(line) or re.search(r"requirements?|supported platforms?|operating systems?", heading, re.I)):
                    kind = "mention" if comparison or (scoped and platform not in scoped) else "documentation"
                    evidence(platform, kind, line, document.get("path", "README"))
                elif platform_section and POSITIVE.search(line) and not any(pattern.search(line) for key, pattern in WORDS.items() if key != platform):
                    evidence(platform, "documentation", f"{heading}: {line}", document.get("path", "README"))
    for target in catalog_targets:
        if target in LABELS:
            evidence(target, "catalog", "Bundled packaging instructions", "Portkeeper catalog")
    platforms = [records[key] for key in LABELS if key in records]
    for record in platforms:
        kinds = {item["kind"] for item in record["evidence"]}
        record["support"] = "released" if "release" in kinds else "documented" if kinds & {"documentation", "catalog"} else "mentioned"
    released = [record["id"] for record in platforms if record["packaging_supported"] and record["support"] == "released"]
    suggested = released or [record["id"] for record in platforms if record["packaging_supported"] and record["support"] == "documented"]
    return {"platforms": platforms, "suggested_targets": suggested,
            "note": "Detected from release filenames and project documentation. This does not verify launch, gameplay or hardware compatibility."}
