"""Expose matching host CUDA libraries to bbport 0.51's portable driver cache.
Workaround for https://github.com/deadinside28/bloodborne_pc/issues/135.
Regenerated on every launch so moving the package or updating drivers is safe.
"""
import hashlib
import json
import os
from pathlib import Path

MANIFEST_DIRS = ['/etc/vulkan/icd.d', '/usr/share/vulkan/icd.d', '/usr/local/share/vulkan/icd.d', '/run/opengl-driver/share/vulkan/icd.d']
LIBRARY_DIRS = ['/usr/lib/x86_64-linux-gnu', '/lib/x86_64-linux-gnu', '/usr/lib64', '/lib64', '/usr/lib', '/lib', '/run/opengl-driver/lib']


def elf64(path):
    try:
        with path.open('rb') as stream:
            header = stream.read(20)
        return header[:6] == b'\x7fELF\x02\x01' and header[18:20] == b'\x3e\x00'
    except OSError:
        return False


def configure(data_dir):
    if os.environ.get('VK_DRIVER_FILES') or os.environ.get('VK_ICD_FILENAMES'):
        return
    directories = [Path(p) for p in LIBRARY_DIRS]
    if os.environ.get('BB_NVIDIA_LIB_DIR'):
        directories.insert(0, Path(os.environ['BB_NVIDIA_LIB_DIR']))
    for directory in map(Path, MANIFEST_DIRS):
        for manifest in sorted(directory.glob('*nvidia*.json')):
            try:
                library = Path(json.loads(manifest.read_text())['ICD']['library_path'])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if 'nvidia' not in library.name.lower():
                continue
            candidates = ([library, *[d / library.name for d in directories]] if library.is_absolute() else [manifest.parent / library] if len(library.parts) > 1 else [d / library for d in directories])
            for candidate in candidates:
                if not elf64(candidate):
                    continue
                driver = candidate.resolve()
                key = hashlib.sha256(f'{driver}:{driver.stat().st_mtime_ns}'.encode()).hexdigest()[:16]
                target = data_dir / 'vulkan/nvidia' / key / 'lib'
                target.mkdir(parents=True, exist_ok=True)
                count = 0
                for source in sorted(driver.parent.glob('libcuda.so*')):
                    if not elf64(source):
                        continue
                    link = target / source.name
                    if link.is_symlink():
                        if link.resolve() == source.resolve():
                            count += 1
                            continue
                        link.unlink()
                    elif link.exists():
                        continue
                    link.symlink_to(source.resolve())
                    count += 1
                print(f'Portable NVIDIA fix: {count} matching host CUDA library links ready', flush=True)
                return


if __name__ == '__main__':
    configure(Path(__file__).resolve().parent / 'data')
