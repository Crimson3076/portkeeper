"""Start new drafts from definitions that already work, and share a single definition as JSON.

Examples come from the user's own saved packaging and the built-in catalog. A "template fit"
is deliberately strict: it only happens when the new release has the same shape as a reference
with nothing game-specific to carry over (no hashes, tools or source checks). Anything looser is
offered to the model as a bounded example instead. Candidates are always reviewed and approved
by a person, and nothing here runs a build.
"""
import copy
import json
import re

from . import packaging
from .safety import Invalid

MAX_EXAMPLES = 2
EXAMPLE_CHARS = 3500
MIN_SCORE = 6
EXPORT_KIND = 'portkeeper-definition'
MAX_IMPORT_BYTES = 256 * 1024
# Operations that carry over to another project without game-specific facts.
TEMPLATE_OPS = {'unpack_download', 'launcher', 'write_text', 'touch', 'stage_input', 'copy'}
ROM_WORDS = re.compile(r'(?i)\b(?:rom|\.z64|\.n64|\.v64|\.sfc|\.smc|\.nes|\.gba|\.nds|\.iso|\.wbfs)\b')


def candidates(catalog, user, exclude_project=None):
    """Reference definitions: the person's own saved packaging first, then the built-in catalog."""
    seen, result = set(), []
    for project_id, definition in user:
        if project_id == exclude_project or not isinstance(definition, dict) or definition.get('runner') != 'operations':
            continue
        result.append({'origin': 'your approved definitions', 'definition': definition})
        seen.add(definition.get('id'))
    for key, definition in sorted(catalog.items()):
        if key not in seen and definition.get('runner') == 'operations':
            result.append({'origin': 'built-in catalog', 'definition': definition})
    return result


def _kind(name):
    return 'tar' if re.search(r'(?i)\.(tar(\.\w+)?|tgz|txz)$', name) else 'zip' if name.lower().endswith('.zip') else 'other'


def _download_kind(download):
    if download.get('format') in ('zip', 'tar'):
        return download['format']
    return _kind(re.sub(r'\\|\$|\^|\(.*?\)\??', '', download.get('asset_pattern', '')))


def _data_kind(context):
    preparation = context.get('game_file_preparation', {})
    if any(preparation.get('ps4', {}).values()):
        return 'ps4'
    return 'rom' if ROM_WORDS.search(context.get('material', {}).get('readme', '')) else 'none'


def _reference_kind(definition):
    if any(op.get('op') == 'tool' for op in definition.get('operations', [])):
        return 'ps4' if any(op.get('tool') == 'ps4_pkg_extract' for op in definition['operations']) else 'tool'
    return 'rom' if definition.get('inputs') else 'none'


def _assets(context, target):
    from . import platforms
    from .drafting import SKIP_ASSET
    return [a['name'] for a in context['release']['assets']
            if a.get('name') and not SKIP_ASSET.search(a['name']) and target in platforms.detect([a])['suggested_targets']]


def _members(context, name, prefix):
    names = context.get('archive_evidence', {}).get(name, context['release'].get('archive_contents', {}).get(name, []))
    return [p[len(prefix) + 1:] if prefix else p for p in names
            if not p.endswith('/') and (not prefix or p.startswith(prefix + '/'))]


def _launchers(definition, target):
    return [op for op in definition['operations'] if op['op'] == 'launcher' and target in (op.get('targets') or definition['targets'])]


def _extension(name):
    """Only .exe and .AppImage identify a launch file without guessing."""
    lowered = name.lower()
    return '.exe' if lowered.endswith('.exe') else '.appimage' if lowered.endswith('.appimage') else None


def _resolve_exec(old, members):
    """The launch file in the new archive, or None when it cannot be named without guessing."""
    if old in members:
        return old
    extension = _extension(old)
    if '/' in old or not extension:
        return None
    pool = [m for m in members if '/' not in m and _extension(m) == extension and not re.search(r'(?i)unins|setup|crash|redist', m)]
    return pool[0] if len(pool) == 1 else None


def _target_fit(context, definition, target):
    """Facts about one target in the new release against the reference, or None if they differ."""
    names = _assets(context, target)
    download = definition['downloads'].get(target)
    if len(names) != 1 or not download:
        return None
    name = names[0]
    layout = context['release'].get('archive_layouts', {}).get(name, {})
    if not layout.get('complete') or _kind(name) != _download_kind(download) or _kind(name) == 'other':
        return None
    prefix = layout.get('enclosing_folder')
    if bool(prefix) != bool(download.get('strip_prefix')):
        return None
    members = _members(context, name, prefix)
    execs = {}
    for op in _launchers(definition, target):
        found = _resolve_exec(op['exec'], members)
        if not found:
            return None
        execs[op['exec']] = found
    if not execs:
        return None
    return {'asset': name, 'prefix': prefix, 'execs': execs, 'download': download}


def score(context, reference):
    """How closely a reference matches the new release; 0 when it cannot serve as an example."""
    project, definition = context['project'], reference['definition']
    if not set(project['targets']) <= set(definition['targets']):
        return 0
    points = 3 if set(project['targets']) == set(definition['targets']) else 1
    for target in project['targets']:
        names = _assets(context, target)
        download = definition['downloads'].get(target)
        if len(names) != 1 or not download:
            continue
        layout = context['release'].get('archive_layouts', {}).get(names[0], {})
        points += 1 if _kind(names[0]) == _download_kind(download) else 0
        points += 1 if bool(layout.get('enclosing_folder')) == bool(download.get('strip_prefix')) else 0
        points += 2 if _target_fit(context, definition, target) else 0
    return points + (2 if _data_kind(context) == _reference_kind(definition) else 0)


def select(context, references, limit=MAX_EXAMPLES):
    """The closest references, best first. Same repository as the project is never the reason for a match."""
    ranked = sorted(((score(context, r), i, r) for i, r in enumerate(references)), key=lambda t: (-t[0], t[1]))
    return [{**r, 'score': s} for s, _, r in ranked if s >= MIN_SCORE][:limit]


def summarize(selected):
    """What was used, for the drafting record: no definition bodies."""
    return [{'id': r['definition']['id'], 'name': r['definition']['name'], 'revision': r['definition']['revision'],
             'origin': r['origin'], 'score': r['score']} for r in selected]


def example_material(selected):
    """Compact, size-limited examples for the prompt with a note on what carries over."""
    result = []
    for reference in selected:
        definition = reference['definition']
        compact = {key: definition[key] for key in ('targets', 'downloads', 'inputs', 'operations', 'outputs', 'launch', 'first_run')
                   if definition.get(key)}
        text = json.dumps(compact, separators=(',', ':'))
        while len(text) > EXAMPLE_CHARS and compact.get('operations'):
            compact['operations'] = compact['operations'][:-1]
            compact['truncated'] = True
            text = json.dumps(compact, separators=(',', ':'))
        result.append({'name': definition['name'], 'origin': reference['origin'], 'definition': compact if len(text) <= EXAMPLE_CHARS else None,
                       'likely_to_carry_over': 'Archive unpacking, launcher writing, staging a user-supplied file.',
                       'must_be_checked': 'Asset names, strip_prefix, required members, hashes, launch executable and first-launch text for THIS project.'})
    return [item for item in result if item['definition']]


def fit(context, references):
    """A candidate definition made directly from a close reference, or None. Never calls a model."""
    project = context['project']
    if _data_kind(context) not in ('none', 'rom'):
        return None
    for reference in references:
        definition = reference['definition']
        if (not set(project['targets']) <= set(definition['targets']) or _reference_kind(definition) != _data_kind(context)
                or definition.get('source_checks') or any(op['op'] not in TEMPLATE_OPS for op in definition['operations'])
                or any(item.get('accepted') or item.get('verify') != 'downstream' or item['kind'] != 'file' for item in definition.get('inputs', []))
                or any(op.get('prepare_once') or op.get('run_python') for op in definition['operations'] if op['op'] == 'launcher')
                or any(not op.get('targets') for op in definition['operations'] if op['op'] in ('launcher', 'unpack_download'))
                # Launch files are matched against archive members, so they must land at the package root.
                or any(op.get('to') != '$package' for op in definition['operations'] if op['op'] == 'unpack_download')):
            continue
        facts = {target: _target_fit(context, definition, target) for target in project['targets']}
        if not all(facts.values()):
            continue
        built = _build(context, definition, facts)
        if built and _consistent(built, context):
            return {'definition': built, 'template': {**summarize([{**reference, 'score': score(context, reference)}])[0]},
                    'unresolved': ['Built from a template, not from this project\'s own evidence. Check the launch files, first-launch text and any staged file names against this project before approving.']}
    return None


def _consistent(built, context):
    """The same archive-layout check a model draft must pass."""
    from . import drafting
    try:
        drafting.check_archive_evidence(built, context)
    except Invalid:
        return False
    return True


def _mentions(value, names):
    """True when any string inside value names the reference project or its game."""
    if isinstance(value, str):
        lowered = value.lower()
        return any(name.lower() in lowered for name in names if len(name) > 2)
    if isinstance(value, dict):
        return any(_mentions(item, names) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_mentions(item, names) for item in value)
    return False


def _build(context, reference, facts):
    project = context['project']
    names = {reference.get('name', ''), reference.get('game', ''), reference['id']} - {''}
    result = copy.deepcopy(reference)
    result.update(id=project['definition_id'], revision=1, name=project['name'], package_prefix=project['definition_id'],
                  targets=list(project['targets']), history=[], verified_releases=[],
                  upstream={'repository': project['repository']}, limitations=[], dependencies={})
    if _mentions([reference.get('inputs', []), {k: v for t in reference.get('outputs', {}).values() for k, v in t.items() if k != 'expected_files'}], names):
        return None
    for key in ('game', 'summary', 'saves', 'notes'):
        result.pop(key, None)
    result['downloads'] = {}
    result['outputs'], result['launch'] = {}, {}
    result['operations'] = []
    for op in copy.deepcopy(reference['operations']):
        if op.get('targets'):
            op['targets'] = [t for t in op['targets'] if t in project['targets']]
            if not op['targets']:
                continue
        if op['op'] == 'unpack_download' and op['download'] not in project['targets']:
            continue
        if _mentions({key: value for key, value in op.items() if key != 'exec'}, names):
            return None
        if op['op'] == 'launcher':
            target = (op.get('targets') or project['targets'])[0]
            op['exec'] = facts[target]['execs'][op['exec']]
        result['operations'].append(op)
    for target in project['targets']:
        old, fact = reference['downloads'][target], facts[target]
        item = {'asset_pattern': '^' + re.escape(fact['asset']) + '$', 'platform': target, 'format': old.get('format', 'auto')}
        if fact['prefix']:
            item['strip_prefix'] = fact['prefix']
        item['required_members'] = sorted(set(fact['execs'].values()))
        result['downloads'][target] = item
        generated = [op['path'] for op in result['operations'] if op['op'] == 'launcher' and target in (op.get('targets') or project['targets'])]
        generated += [op['path'].split('/', 1)[1] for op in result['operations']
                      if op['op'] in ('write_text', 'touch') and target in (op.get('targets') or project['targets']) and op['path'].startswith('$package/')]
        output = {key: value for key, value in reference['outputs'].get(target, {}).items() if key != 'expected_files'}
        output['expected_files'] = list(dict.fromkeys([*item['required_members'], *generated]))
        result['outputs'][target] = output
        paths = [op['path'] for op in result['operations'] if op['op'] == 'launcher' and target in (op.get('targets') or project['targets'])]
        if paths:
            result['launch'][target] = {'command': paths[0], 'instructions': f'Extract the whole archive, open the package folder and run {paths[0]}.'}
    if reference.get('first_run'):
        result['first_run'] = {'mode': reference['first_run']['mode']}
    try:
        return packaging.validate_definition(result)
    except Invalid:
        return None


# Sharing one definition between Portkeeper instances ------------------------------------
def export_definition(definition):
    return {'kind': EXPORT_KIND, 'format': 1, 'definition': copy.deepcopy(definition)}


def read_import(text, repository=None, definition_id=None):
    """Validate a shared definition. Verified-release claims never travel: they were not verified here."""
    if not isinstance(text, str) or not text.strip():
        raise Invalid('Paste or choose an exported definition (JSON) first')
    if len(text.encode('utf-8', 'ignore')) > MAX_IMPORT_BYTES:
        raise Invalid('That file is too large to be a packaging definition')
    try:
        data = json.loads(text)
    except ValueError as error:
        raise Invalid(f'That is not valid JSON: {error}') from None
    if isinstance(data, dict) and data.get('kind') == EXPORT_KIND:
        if data.get('format') != 1:
            raise Invalid('This export uses a format this Portkeeper does not understand')
        data = data.get('definition')
    definition = packaging.validate_definition(data)
    if definition['runner'] != 'operations':
        raise Invalid('Only definitions that use the operations runner can be imported')
    if repository and definition['upstream']['repository'].lower() != repository.lower():
        raise Invalid(f"This definition is for {definition['upstream']['repository']}, not {repository}")
    definition['verified_releases'] = []
    if definition_id:
        definition['id'] = definition['package_prefix'] = definition_id
        definition['history'] = []
        definition = packaging.validate_definition(definition)
    return definition
