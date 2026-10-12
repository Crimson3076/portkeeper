"""Bounded, read-only PS4 metadata inspection. This does not decrypt game data.

PKG offsets follow LibOrbisPkg's PKG/Header and MetaEntry structures (v0.2).
Metadata identifies candidates; extracted title, version and executable checks
remain mandatory before a folder is accepted.
"""
import re
import struct
from pathlib import Path

from .safety import Invalid

MAX_SFO = 1024 * 1024

# Named, non-generated header files from LibOrbisPkg v0.2 EntryNames.
# File paths come from this fixed map, never from untrusted package strings.
SYSTEM_ENTRIES = {
    0x402: 'nptitle.dat', 0x403: 'npbind.dat', 0x404: 'selfinfo.dat', 0x406: 'imageinfo.dat',
    0x407: 'target-deltainfo.dat', 0x408: 'origin-deltainfo.dat', 0x1000: 'param.sfo',
    0x1004: 'pronunciation.xml', 0x1005: 'pronunciation.sig', 0x1006: 'pic1.png',
    0x1007: 'pubtoolinfo.dat', 0x1008: 'app/playgo-chunk.dat', 0x1009: 'app/playgo-chunk.sha',
    0x100A: 'app/playgo-manifest.xml', 0x100B: 'shareparam.json', 0x100C: 'shareoverlayimage.png',
    0x100D: 'save_data.png', 0x100E: 'shareprivacyguardimage.png', 0x1200: 'icon0.png',
    0x1220: 'pic0.png', 0x1240: 'snd0.at9', 0x1260: 'changeinfo/changeinfo.xml',
    0x1280: 'icon0.dds', 0x12A0: 'pic0.dds', 0x12C0: 'pic1.dds',
}
for _index in range(31):
    for _start, _prefix, _suffix in [(0x1201, 'icon0', 'png'), (0x1281, 'icon0', 'dds'),
                                    (0x1241, 'pic1', 'png'), (0x12C1, 'pic1', 'dds')]:
        SYSTEM_ENTRIES[_start + _index] = f'{_prefix}_{_index:02d}.{_suffix}'
    SYSTEM_ENTRIES[0x1261 + _index] = f'changeinfo/changeinfo_{_index:02d}.xml'
    if _index < 10:
        SYSTEM_ENTRIES[0x1600 + _index] = f'keymap_rp/{_index + 1:03d}.png'
    for _key in range(10):
        SYSTEM_ENTRIES[0x1610 + 16 * _index + _key] = f'keymap_rp/{_index:02d}/{_key + 1:03d}.png'


def sfo_values(data):
    if len(data) < 20 or data[:4] != b'\x00PSF':
        raise Invalid('param.sfo is not a PS4 SFO file')
    key_start, data_start, count = struct.unpack_from('<III', data, 8)
    if count > 512 or key_start < 20 + count * 16 or not key_start <= data_start <= len(data):
        raise Invalid('param.sfo has invalid table offsets')
    result = {}
    for index in range(count):
        key_offset, fmt, length, maximum, data_offset = struct.unpack_from('<HHIII', data, 20 + index * 16)
        start = key_start + key_offset
        end = data.find(b'\x00', start, data_start)
        value_start = data_start + data_offset
        if start >= data_start or end < start or length > maximum or value_start + length > len(data):
            raise Invalid('param.sfo has an out-of-bounds entry')
        key = data[start:end].decode('ascii', 'replace')
        value = data[value_start:value_start + length]
        if fmt in (0x0004, 0x0204):
            result[key] = value.rstrip(b'\x00').decode('utf-8', 'replace')
        elif fmt == 0x0404 and length == 4:
            result[key] = struct.unpack('<I', value)[0]
    return result


def pkg_metadata(path):
    path = Path(path)
    size = path.stat().st_size
    with path.open('rb') as stream:
        header = stream.read(0x5A0)
        if len(header) < 0x5A0 or header[:4] != b'\x7fCNT':
            raise Invalid('Not a readable PS4 PKG (expected a PS4 CNT header)')
        count = struct.unpack_from('>I', header, 0x10)[0]
        table = struct.unpack_from('>I', header, 0x18)[0]
        package_size = struct.unpack_from('>Q', header, 0x430)[0]
        if package_size > size:
            raise Invalid('PKG is incomplete; its header declares more bytes than the file contains')
        if count > 4096 or table < 0x20 or table + count * 32 > size:
            raise Invalid('PKG has an invalid entry table')
        content_id = header[0x40:0x70].split(b'\x00', 1)[0].decode('ascii', 'replace')
        title = re.search(r'(?:^|-)CUSA\d{5}(?:_|$)', content_id)
        title_id = title.group(0).lstrip('-').rstrip('_') if title else None
        content_type, flags = struct.unpack_from('>II', header, 0x74)
        stream.seek(table)
        entries = stream.read(count * 32)
        if len(entries) != count * 32:
            raise Invalid('PKG entry table changed or is incomplete')
        values, system_entries = {}, []
        for index in range(count):
            entry_id, _name, entry_flags, _flags2, offset, length = struct.unpack_from('>IIIIII', entries, index * 32)
            if entry_id in SYSTEM_ENTRIES:
                if length > 64 * 1024 * 1024 or offset + length > size:
                    raise Invalid('PKG system entry is too large or out of bounds')
                system_entries.append({'index': index, 'path': SYSTEM_ENTRIES[entry_id]})
            if entry_id != 0x1000:
                continue
            if not 20 <= length <= MAX_SFO or offset + length > size:
                raise Invalid('PKG param.sfo entry is out of bounds')
            if not entry_flags & 0x80000000:
                stream.seek(offset)
                values = sfo_values(stream.read(length))
    sfo_title = values.get('TITLE_ID')
    if any(key in values and not isinstance(values[key], str) for key in ('TITLE_ID', 'APP_VER', 'CATEGORY', 'TITLE')):
        raise Invalid('PKG param.sfo has non-text identification fields')
    if sfo_title and title_id and sfo_title != title_id:
        raise Invalid('PKG content ID and param.sfo identify different titles')
    title_id = sfo_title or title_id
    category = values.get('CATEGORY', '').lower()
    if flags & 0x41000000 == 0x41000000:
        role = 'delta_update'
    elif content_type not in (0x1A, 0x1E) or category not in ('', 'gd', 'gp'):
        role = 'other'
    elif content_type == 0x1E or category == 'gp' or flags & 0x40300000:
        # PkgTool v0.2 writes type DP for complete patch PFS packages too.
        # Only the explicit delta flags establish an unsupported binary delta.
        role = 'update'
    else:
        role = 'base'
    return {'title_id': title_id, 'app_version': values.get('APP_VER'), 'title': values.get('TITLE'),
            'role': role, 'category': category or None, 'content_id': content_id,
            'content_type': content_type, 'system_entries': system_entries,
            'size': size, 'note': 'Identified from PKG metadata; extracted files still need validation.'}


def input_expectation(definition, input_id):
    for op in definition.get('operations', []):
        if op.get('op') != 'tool' or op.get('tool') != 'ps4_pkg_extract':
            continue
        if input_id in op['packages']:
            index = op['packages'].index(input_id)
            return {'title_id': op['title_id'], 'app_version': op['app_version'],
                    'role': 'base' if index == 0 else 'update', 'last_update': index == len(op['packages']) - 1}
    return None


def mismatch(metadata, expected):
    if metadata['title_id'] != expected['title_id']:
        return f"title is {metadata['title_id'] or 'unknown'}, expected {expected['title_id']}"
    if metadata['role'] == 'delta_update':
        return 'this is a delta update; a full, extractable update PKG is needed'
    if metadata['role'] != expected['role']:
        return f"this is a {metadata['role']} package, not the {expected['role']} PKG"
    version = metadata.get('app_version')
    if version and re.fullmatch(r'\d{2}\.\d{2}', version):
        if expected['role'] == 'update' and expected['last_update'] and version != expected['app_version']:
            return f"update is version {version}, expected {expected['app_version']}"
        if version > expected['app_version']:
            return f"version is {version}, newer than the required {expected['app_version']}"
    return None
