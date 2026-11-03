"""Deterministic, model-free fact sheet for a preserved release.

Built from data Portkeeper already holds (release asset list, archive member
listings, README/documentation text) before any model call. Every fact has a
stable ID (f1, f2, ...) and a source, so a plan can cite IDs instead of
re-quoting text. Nothing here executes, extracts or fetches anything.
"""
import re
from pathlib import PurePosixPath

from . import packaging

MAX_FACTS = 120
EXECUTABLE = re.compile(r'(?i)\.(?:exe|appimage|x86_64|x86|bat|cmd|sh)$')
LIBRARY = re.compile(r'(?i)\.(?:dll|dylib|so(?:\.\d+)*)$')
HASH = re.compile(r'(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{64}|[0-9A-Fa-f]{40}|[0-9A-Fa-f]{32})(?![0-9A-Fa-f])')
HASH_NAMES = {32: 'md5', 40: 'sha1', 64: 'sha256'}
DATA_FILE = re.compile(r'(?i)(?<![\w.-])(?:[\w.+()-]+/)*[\w.+()-]+\.(?:z64|n64|v64|rom|iso|ciso|bin|cue|chd|sfc|smc|nes|gba|nds|wad|pkg|wud|wux|rpx|o2r|otr)\b')
VERSION_NAME = re.compile(r'(?i)\b(?:ntsc|pal|jpn)-[a-z0-9][a-z0-9.]*')
SETUP_LINE = re.compile(r'(?i)\b(?:place|put|copy|folder|directory|rom|required|requires|need|needs)\b')
BACKTICKED = re.compile(r'`([^`\n]{1,120})`')
BUILD_TOOLS = re.compile(r'(?i)(?<![\w-])(cmake|ninja|make|cargo|gradle|msbuild|meson|python3?|node|npm|dotnet)(?![\w-])')
# Mentions that point at an existing fixed tool; a mention is not proof the tool can run.
FIXED_TOOL_ALIASES = {'ship_o2r_headless': r'ship of harkinian|o2r', 'opengoal_extract': r'opengoal',
                      'ps4_pkg_extract': r'ps4|\.pkg\b', 'wwhd_extract': r'wind waker|wwhd', 'torch_o2r': r'torch'}


def _platform(name, size=None):
    from . import platforms
    return platforms.detect([{'name': name, 'size': size}])['suggested_targets']


def _exec_platform(path):
    lower = path.lower()
    if lower.endswith(('.exe', '.bat', '.cmd')):
        return 'windows'
    if lower.endswith(('.appimage', '.x86_64', '.x86', '.sh')):
        return 'linux'
    return None


class _Sheet:
    def __init__(self):
        self.facts = []

    def add(self, kind, text, file, lines=None, doc=False, **data):
        if len(self.facts) >= MAX_FACTS:
            return None
        source = {'file': file}
        if lines:
            source['lines'] = list(lines)
        fact = {'id': 'f' + str(len(self.facts) + 1), 'kind': kind, 'text': text[:300], 'source': source, 'doc': doc}
        if data:
            fact['data'] = data
        self.facts.append(fact)
        return fact


def _layout(sheet, asset, names):
    names = [name.replace('\\', '/').removeprefix('./') for name in names if name]
    files = [name for name in names if not name.endswith('/')]
    if not files:
        return
    complete = len(names) <= 30_000 and not any(name.startswith('(') for name in names)
    tops = sorted({name.split('/', 1)[0] for name in files if '/' in name} | {name.rstrip('/').split('/', 1)[0] for name in names if name.endswith('/') and '/' not in name.rstrip('/')})
    enclosing = ''
    if len(tops) == 1 and all(name.startswith(tops[0] + '/') for name in files):
        enclosing = tops[0] + '/'
    inner = [name[len(enclosing):] for name in files]
    folders = sorted({name.split('/', 1)[0] + '/' for name in inner if '/' in name})
    loose = sorted(name for name in inner if '/' not in name)
    text = f'{asset}: ' + (f"top-level folder {enclosing}; " if enclosing else 'no single top-level folder (files are at the archive root); ')
    text += 'folders: ' + (', '.join(folders[:12]) or 'none') + '; root files: ' + (', '.join(loose[:12]) or 'none')
    text += f'; {len(names)} members, listing {"complete" if complete else "partial"}'
    sheet.add('archive_layout', text, asset, enclosing_folder=enclosing or None, folders=folders[:30],
              root_files=loose[:30], member_count=len(names), complete=complete)
    executables = [(path, rel) for path, rel in zip(files, inner) if EXECUTABLE.search(rel)]
    for path, rel in executables[:20]:
        where = path if path == rel else f'{path} (relative to the folder: {rel})'
        sheet.add('executable', f'{asset}: executable {where}', asset, path=path, relative=rel, platform=_exec_platform(rel))
    libraries = [rel for rel in inner if LIBRARY.search(rel)]
    if libraries:
        sheet.add('libraries', f'{asset}: {len(libraries)} bundled libraries, e.g. ' + ', '.join(libraries[:10]), asset,
                  count=len(libraries), examples=libraries[:30])
    return [(path, rel) for path, rel in executables if rel.count('/') <= 2]


def _documents(context):
    material = context.get('material', {})
    yield 'README.md', material.get('readme', '')
    for item in material.get('documentation', []):
        yield item.get('path', 'documentation'), item.get('text', '')
    for item in material.get('runtime_documentation', []):
        yield item['asset'] + '!/' + item['path'], item.get('text', '')


def _scan(sheet, file, text, seen):
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        for match in HASH.finditer(line):
            value = match.group(0).lower()
            key = ('hash', value)
            if key not in seen:
                seen.add(key)
                algorithm = HASH_NAMES[len(value)]
                sheet.add('hash', f'{algorithm} {value}: {stripped[:160]}', file, (number, number), True,
                          algorithm=algorithm, value=value, context=stripped[:200])
        for match in DATA_FILE.finditer(line):
            key = ('file', match.group(0).lower())
            if key not in seen:
                seen.add(key)
                sheet.add('data_file', f'Documented data file name {match.group(0)}: {stripped[:160]}', file, (number, number), True, name=match.group(0))
        for match in VERSION_NAME.finditer(line):
            key = ('version', match.group(0).lower())
            if key not in seen:
                seen.add(key)
                sheet.add('version_name', f'Documented version name {match.group(0)}: {stripped[:160]}', file, (number, number), True, name=match.group(0))
        if SETUP_LINE.search(line) and BACKTICKED.search(line) and ('setup', number, file) not in seen:
            seen.add(('setup', number, file))
            sheet.add('setup_line', f'Setup/data requirement mentioned: {stripped[:200]}', file, (number, number), True,
                      names=BACKTICKED.findall(line)[:6])
        for match in BUILD_TOOLS.finditer(line):
            key = ('tool', match.group(1).lower())
            if key not in seen:
                seen.add(key)
                sheet.add('tool_mention', f'Mentions build/runtime tool {match.group(1)}: {stripped[:160]}', file, (number, number), True,
                          tool=match.group(1).lower(), fixed_tool=None)
        for tool, pattern in FIXED_TOOL_ALIASES.items():
            key = ('fixed', tool)
            if key not in seen and tool in packaging.TOOLS and re.search(r'(?i)' + pattern, line):
                seen.add(key)
                sheet.add('tool_mention', f'Text may refer to fixed tool {tool} (a mention does not prove it can run): {stripped[:160]}',
                          file, (number, number), True, tool=tool, fixed_tool=tool)


def build(capture, context):
    """Return {'facts': [...]}: same inputs always give the same facts and IDs."""
    sheet, seen = _Sheet(), set()
    assets = {item['name']: item for item in capture.get('assets', []) if isinstance(item.get('name'), str)}
    for name, item in assets.items():
        guess = _platform(name, item.get('size'))
        sheet.add('asset', f"Release asset {name}, {item.get('size', 'unknown')} bytes, platform guess: {', '.join(guess) or 'unknown'}",
                  'release assets', name=name, size=item.get('size'), platforms=guess)
    candidates = []
    for name, members in sorted(context.get('archive_evidence', {}).items()):
        launchable = _layout(sheet, name, members) or []
        guess = _platform(name)
        for path, rel in sorted(launchable, key=lambda pair: (pair[1].count('/'), pair[1].lower())):
            platform = _exec_platform(rel)
            candidates.append((name, path, platform or (guess[0] if len(guess) == 1 else None), guess))
    for name, path, platform, guess in candidates[:20]:
        sheet.add('launch_candidate', f"Launch candidate for {platform or 'unknown platform'}: {path} in {name}", name,
                  asset=name, path=path, platform=platform, asset_platforms=guess)
    for file, text in _documents(context):
        _scan(sheet, file, text, seen)
    return {'facts': sheet.facts}


def by_id(sheet):
    return {fact['id']: fact for fact in sheet['facts']}


def cited(ids, sheet):
    """Facts for cited IDs, or None plus the unknown IDs."""
    known = by_id(sheet)
    unknown = [item for item in ids if item not in known]
    return [known[item] for item in ids if item in known], unknown


def compact(sheet, limit=None):
    """Prompt form: ID, kind, text and source only."""
    facts = sheet['facts'][:limit] if limit is not None else sheet['facts']
    return [{'id': f['id'], 'kind': f['kind'], 'text': f['text'],
             'source': f['source']['file'] + (':%d-%d' % tuple(f['source']['lines']) if f['source'].get('lines') else '')} for f in facts]
