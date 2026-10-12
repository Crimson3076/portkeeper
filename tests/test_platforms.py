import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from portkeeper import platforms
from portkeeper.engine import Engine
from portkeeper.safety import Invalid


class PlatformDetectionTests(unittest.TestCase):
    def test_release_operating_systems_are_detected_without_guessing_architecture(self):
        result = platforms.detect([{"name": name} for name in ["game-linux-x64.zip", "game-win64.zip", "game.apk", "game-macos.dmg", "game.ipa", "source.zip", "game-arm64.zip", "game-ps4.pkg"]])
        self.assertEqual([record["id"] for record in result["platforms"]], ["linux", "windows", "android", "macos", "ios"])
        self.assertEqual(result["suggested_targets"], ["linux", "windows"])
        self.assertTrue(all(not record["packaging_supported"] for record in result["platforms"][2:]))
        self.assertEqual(platforms.detect([{"name": "game-arm64.zip"}])["suggested_targets"], [])
        self.assertEqual(platforms.detect([{"name": "release.zip"}])["suggested_targets"], [])

    def test_documented_builds_are_distinct_from_release_downloads(self):
        docs = [{"path": "README.md", "text": "# Supported platforms\n- Windows\n- Linux\n## Android build instructions\nInstall the Android SDK and compile the app."}]
        result = platforms.detect([{"name": "game-windows.zip"}], docs)
        by_id = {record["id"]: record for record in result["platforms"]}
        self.assertEqual(result["suggested_targets"], ["linux", "windows"])
        self.assertTrue(any(item["kind"] == "release" for item in by_id["windows"]["evidence"]))
        self.assertTrue(all(item["kind"] == "documentation" for item in by_id["linux"]["evidence"]))
        self.assertFalse(by_id["android"]["packaging_supported"])

    def test_unsupported_platforms_credits_and_roadmaps_are_not_support(self):
        docs = [{"path": "README.md", "text": "A Windows port.\nLinux is not supported.\n## Supported platforms\n| Android | No |\n## Unsupported platforms\n- macOS\n## Roadmap\nBuild an Android port someday.\n## Credits\nSDL supports Linux and Android."}]
        self.assertEqual(platforms.detect([], docs)["suggested_targets"], ["windows"])
        self.assertEqual([record["id"] for record in platforms.detect([], docs)["platforms"]], ["windows"])

    def test_a_platform_heading_alone_does_not_imply_support(self):
        docs = [{"path": "README.md", "text": "## Linux\nNot supported yet.\n## Windows\nBuild using MSYS2."}]
        self.assertEqual(platforms.detect([], docs)["suggested_targets"], ["windows"])

    def test_pinned_readme_and_linked_guides_are_bounded_and_first_party(self):
        requests = []
        commit = "a" * 40
        def open_document(url, accept):
            requests.append(url)
            if url.endswith("README.md"):
                return io.BytesIO(b"[Build on Linux](docs/linux-build.md)\n[External](https://evil.example/windows.md)\n[Escape](../windows.md)\n[Encoded escape](%2e%2e/windows.md)")
            if url.endswith("docs/linux-build.md"):
                return io.BytesIO(b"# Linux build\nBuild with CMake")
            raise Invalid("missing")
        docs, error = platforms.read_documents(Mock(open=open_document), "owner/game", commit)
        self.assertIsNone(error)
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(url.startswith(f"https://raw.githubusercontent.com/owner/game/{commit}/") for url in requests))
        self.assertEqual(platforms.detect([], docs)["suggested_targets"], ["linux"])

    def test_missing_or_oversized_readme_does_not_invent_platforms(self):
        github = Mock(open=lambda *args: io.BytesIO(b"x" * 128_001))
        docs, error = platforms.read_documents(github, "owner/game", "a" * 40)
        self.assertEqual(docs, [])
        self.assertIn("could not be read", error)
        github = Mock()
        docs, error = platforms.read_documents(github, "owner/game", "not-a-commit")
        github.open.assert_not_called()
        self.assertEqual(docs, [])

    def test_lookup_combines_release_and_documentation_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            github = Mock()
            github.release.return_value = {"version": "v1", "commit": "a" * 40, "url": "https://github.com/example/game/releases/tag/v1", "assets": [{"name": "game-windows.zip"}]}
            github.open.side_effect = lambda *args: io.BytesIO(b"# Linux build instructions\nBuild on Linux with CMake.")
            engine = Engine(Path(temp) / "data", github=github)
            self.addCleanup(engine.close)
            result = engine.github_lookup("example/game")
            self.assertEqual(result["platforms"], ["linux", "windows"])
            self.assertEqual(result["platform_detection"]["platforms"][0]["evidence"][0]["kind"], "documentation")
