"""Match registered native-port imports after RomM discovers their files."""
import json
import urllib.parse
import urllib.request
from pathlib import Path

from .romm import _OriginRedirect, _origin
from .safety import Invalid


class ImportIdentities:
    def __init__(self, romm, data_dir):
        self.romm = romm
        self.path = Path(data_dir) / 'romm-import-identities.json'

    def sync(self):
        if not self.path.is_file():
            return {'matched': 0}
        if self.path.is_symlink():
            raise Invalid('Import identity configuration must not be a symlink')
        identities = json.loads(self.path.read_text())
        if not isinstance(identities, list) or len(identities) > 100:
            raise Invalid('Import identities must be a list of at most 100 games')
        config = self.romm._config()
        matched = 0
        for identity in identities:
            prefix, game, igdb_id = identity['filename_prefix'], identity['game'], identity['igdb_id']
            if not isinstance(prefix, str) or not prefix or '/' in prefix or not isinstance(game, str) or not game or type(igdb_id) is not int or igdb_id < 1:
                raise Invalid('Invalid registered game identity')
            offset = 0
            while True:
                params = urllib.parse.urlencode({'search_term': prefix, 'limit': 100, 'offset': offset, 'with_files': 'false', 'with_char_index': 'false', 'with_filter_values': 'false', 'with_rom_id_index': 'false'})
                result = self.romm._json('roms?' + params)
                items = result.get('items', [])
                for record in items:
                    # Only explicit archive prefixes on PC platforms; console inputs remain untouched.
                    filename = record.get('fs_name', '')
                    if record.get('platform_slug') not in ('linux', 'win') or not filename.startswith(prefix + '-') or not filename.lower().endswith('.zip'):
                        continue
                    cover = identity.get('url_cover', '')
                    current_name = record.get('name', filename)
                    name = current_name if current_name.startswith(game + ' — ') else game + ' — ' + current_name
                    if record.get('igdb_id') == igdb_id and current_name == name and (not cover or record.get('url_cover') == cover):
                        continue
                    data = {'igdb_id': str(igdb_id), 'name': name, 'summary': record.get('summary') or ''}
                    if cover:
                        data['url_cover'] = cover
                    request = urllib.request.Request(config['url'] + '/api/roms/' + str(record['id']), data=urllib.parse.urlencode(data).encode(), headers={'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/x-www-form-urlencoded'}, method='PUT')
                    opener = urllib.request.build_opener(_OriginRedirect(_origin(config['url'])))
                    with opener.open(request, timeout=60) as response:
                        updated = json.load(response)
                    if updated.get('igdb_id') != igdb_id:
                        raise Invalid('RomM did not retain the registered game identity')
                    matched += 1
                offset += len(items)
                if not items or offset >= result.get('total', offset):
                    break
        return {'matched': matched}
