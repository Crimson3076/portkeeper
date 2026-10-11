import unittest
from portkeeper.profiles import catalog, profile, supported_hashes, ensure_release, SHIPWRIGHT_COMMIT
from portkeeper.safety import Invalid, validate_recipe


class ProfileTests(unittest.TestCase):
    def test_verified_profile_contains_exact_releases_and_supported_roms(self):
        record = profile('shipwright')
        self.assertEqual(record['tested_release'], '9.3.0')
        self.assertEqual(record['targets'], ['linux', 'windows'])
        self.assertEqual(len(supported_hashes()), 19)
        self.assertEqual(supported_hashes()['41b3bdc48d98c48529219919015a1af22f5057c2'], 'NTSC 1.2 (US)')
        for runtime in record['runtimes'].values():
            self.assertEqual(len(runtime['sha256']), 64)
        validate_recipe(record['default_recipe'])

    def test_unknown_release_or_changed_commit_is_blocked(self):
        self.assertEqual(ensure_release('shipwright', 'v9.3.0', SHIPWRIGHT_COMMIT)['source_commit'], SHIPWRIGHT_COMMIT)
        for version, commit in [('9.3.1', None), ('9.3.0', 'a' * 40)]:
            with self.assertRaises(Invalid):
                ensure_release('shipwright', version, commit)
        with self.assertRaises(Invalid):
            profile('unknown')

    def test_catalog_is_not_mutable_shared_trust(self):
        record = catalog()[0]
        record['supported_roms'].clear()
        record['runtimes']['linux']['sha256'] = 'changed'
        self.assertEqual(len(profile('shipwright')['supported_roms']), 19)
        self.assertNotEqual(profile('shipwright')['runtimes']['linux']['sha256'], 'changed')


if __name__ == '__main__':
    unittest.main()
