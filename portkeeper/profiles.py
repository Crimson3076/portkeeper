"""Curated profiles are version-specific; unknown releases are preserved, not built."""
import copy

from .safety import Invalid

SHIPWRIGHT_COMMIT = "ecd889c2019b87e78b0a3d942a6a3fe98b7efaed"
_SUPPORTED = [{'name': 'PAL 1.0', 'sha1': '328a1f1beba30ce5e178f031662019eb32c5f3b5'}, {'name': 'PAL 1.1', 'sha1': 'cfbb98d392e4a9d39da8285d10cbef3974c2f012'}, {'name': 'PAL GC', 'sha1': '0227d7c0074f2d0ac935631990da8ec5914597b4'}, {'name': 'PAL MQ', 'sha1': 'f46239439f59a2a594ef83cf68ef65043b1bffe2'}, {'name': 'PAL GC (Debug)', 'sha1': 'cee6bc3c2a634b41728f2af8da54d9bf8cc14099'}, {'name': 'PAL MQ (Debug)', 'sha1': '079b855b943d6ad8bd1eb026c0ed169ecbdac7da'}, {'name': 'PAL MQ (Debug)', 'sha1': '50bebedad9e0f10746a52b07239e47fa6c284d03'}, {'name': 'PAL MQ (Debug)', 'sha1': 'cfecfdc58d650e71a200c81f033de4e6d617a9f6'}, {'name': 'NTSC 1.0 (US)', 'sha1': 'ad69c91157f6705e8ab06c79fe08aad47bb57ba7'}, {'name': 'NTSC 1.1 (US)', 'sha1': 'd3ecb253776cd847a5aa63d859d8c89a2f37b364'}, {'name': 'NTSC 1.2 (US)', 'sha1': '41b3bdc48d98c48529219919015a1af22f5057c2'}, {'name': 'NTSC 1.0 (JP)', 'sha1': 'c892bbda3993e66bd0d56a10ecd30b1ee612210f'}, {'name': 'NTSC 1.1 (JP)', 'sha1': 'dbfc81f655187dc6fefd93fa6798face770d579d'}, {'name': 'NTSC 1.2 (JP)', 'sha1': 'fa5f5942b27480d60243c2d52c0e93e26b9e6b86'}, {'name': 'NTSC GC (US)', 'sha1': 'b82710ba2bd3b4c6ee8aa1a7e9acf787dfc72e9b'}, {'name': 'NTSC MQ (US)', 'sha1': '8b5d13aac69bfbf989861cfdc50b1d840945fc1d'}, {'name': 'NTSC GC (JP)', 'sha1': '0769c84615422d60f16925cd859593cdfa597f84'}, {'name': "NTSC GC (JP) (Collector's Edition)", 'sha1': '2ce2d1a9f0534c9cd9fa04ea5317b80da21e5e73'}, {'name': 'NTSC MQ (JP)', 'sha1': 'dd14e143c4275861fe93ea79d0c02e36ae8c6c2f'}]

_PROFILE = {
    "id": "shipwright",
    "name": "Ship of Harkinian",
    "description": "Extract your verified Ocarina of Time ROM and package matching official runtimes.",
    "repository": "HarbourMasters/Shipwright",
    "tested_release": "9.3.0",
    "preservation_scope": "Shipwright 9.3.0 headless extraction and prebuilt runtime assembly",
    "source_commit": SHIPWRIGHT_COMMIT,
    "release_url": "https://github.com/HarbourMasters/Shipwright/releases/tag/9.3.0",
    "rom_search": "Ocarina of Time",
    "rom_platform": "n64",
    "targets": ["linux", "windows"],
    "devices": [{"id": "linux", "label": "Linux x86-64 / Steam Deck", "architecture": "x86_64"},
                {"id": "windows", "label": "Windows x64", "architecture": "x86_64"}],
    "unsupported_devices": ["macOS", "ARM handhelds", "Nintendo Switch"],
    "asset_format": "o2r",
    "validation": "packaged",
    "validation_note": "Extraction and packaging are verified. Launch checks and user-confirmed gameplay are separate.",
    "release_policy": "Preserve newer releases automatically; require a tested profile before building them.",
    "runner": {"kind": "local-extractor", "requires_opt_in": True,
               "requirements": ["Linux x86-64 build host", "CMake", "Ninja", "C/C++ compiler", "patch", "lsb_release", "Python 3.11+"],
               "patch": "tools/patches/soh-torch-cli-read-rom.patch",
               "patch_reason": "Load and normalize ROM bytes before calling the release extractor's vector API."},
    "runtimes": {
        "linux": {"name": "SoH-Dewey-Alpha.appimage", "sha256": "8507bb70413e130e452ebaf5f156bfd78013ee0144364b7968680e8cf064a0fa",
                  "url": "https://github.com/HarbourMasters/Shipwright/releases/download/9.3.0/SoH-Dewey-Alpha.appimage"},
        "windows": {"name": "SoH-Dewey-Alpha-Win64.zip", "sha256": "bf46cfc41be17f3b63901db46dac46ac2a63ed3851aa13e3ba6c9ec5d0ac2632",
                    "url": "https://github.com/HarbourMasters/Shipwright/releases/download/9.3.0/SoH-Dewey-Alpha-Win64.zip"},
    },
    "default_recipe": {"version": 1, "mode": "profile", "profile": "shipwright",
        "payload": {"kind": "release", "asset_patterns": {"linux": "^SoH-.*[.]appimage$", "windows": "^SoH-.*-Win64[.]zip$"}},
        "inputs": [], "expected_files": ["oot.o2r"]},
    "sources": ["https://github.com/HarbourMasters/Shipwright/blob/9.3.0/docs/supportedHashes.json",
                "https://github.com/HarbourMasters/Shipwright/blob/9.3.0/docs/BUILDING.md",
                "https://github.com/HarbourMasters/Shipwright/releases/tag/9.3.0"],
}
_PROFILE["supported_roms"] = _SUPPORTED


def catalog():
    """Return detached records so callers cannot mutate trusted metadata."""
    return [copy.deepcopy(_PROFILE)]


def profile(profile_id):
    if profile_id != "shipwright":
        raise Invalid("Unknown verified profile")
    return copy.deepcopy(_PROFILE)


def supported_hashes(profile_id="shipwright"):
    return {item["sha1"]: item["name"] for item in profile(profile_id)["supported_roms"]}


def ensure_release(profile_id, version, commit=None):
    record = profile(profile_id)
    if str(version).removeprefix("v") != record["tested_release"]:
        raise Invalid("This release has been preserved, but its profile has not been tested. Build the verified 9.3.0 release or update the profile first.")
    if commit is not None and commit != record["source_commit"]:
        raise Invalid("Release source commit differs from the verified profile; build is blocked")
    return record
