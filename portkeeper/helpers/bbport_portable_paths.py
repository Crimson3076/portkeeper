"""Keep bbport launcher paths within its movable Linux package."""
import json,os
from pathlib import Path
root=Path(__file__).resolve().parent
config=root/'data/config/bbport-launcher/settings.json'
config.parent.mkdir(parents=True,exist_ok=True)
settings={}
if config.is_file():
 settings=json.loads(config.read_text())
settings.update(game_dir=str(root/'game'),user_dir=str(root/'data/user'),mods_dir=str(root/'data/mods'),patches_dir=str(root/'data/patches'))
settings.setdefault('ui_language','en')
settings.setdefault('fullscreen',False)
settings.setdefault('fps_mode','30')
settings.setdefault('skip_network_choice',True)
config.write_text(json.dumps(settings,indent=2)+'\n');config.chmod(0o600)
