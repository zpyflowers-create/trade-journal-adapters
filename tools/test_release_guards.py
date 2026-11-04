import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import release_ci
import github_automation


class ReleaseGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'automation').mkdir()
        (self.root / 'tools').mkdir()
        (self.root / 'tools/test_contract_bybit.py').write_text('# fixture marker')
        (self.root / 'automation/providers.json').write_text(json.dumps({'providers': {'bybit': {'source': 'sources/bybit/adapter.py'}}}))
        self.env = dict(ADAPTER_TYPE='bybit', ADAPTER_VERSION='1.1.6', SOURCE_SHA='a'*40,
                        CONFIRM_RELEASE='PUBLISH', RELEASE_NOTES='fixture')

    def test_no_implicit_release_confirmation(self):
        with patch.dict(os.environ, dict(self.env, CONFIRM_RELEASE=''), clear=True):
            with self.assertRaisesRegex(ValueError, 'confirmation'):
                release_ci.main()

    def test_main_revision_change_rejected_before_reading_key(self):
        with patch.object(release_ci, 'ROOT', self.root), patch.dict(os.environ, self.env, clear=True), \
             patch.object(release_ci.subprocess, 'check_output', return_value='b'*40+'\n'):
            with self.assertRaisesRegex(ValueError, 'reviewed revision'):
                release_ci.main()

    def test_missing_signing_secret_cannot_bypass_signature(self):
        with patch.object(release_ci, 'ROOT', self.root), patch.dict(os.environ, self.env, clear=True), \
             patch.object(release_ci.subprocess, 'check_output', return_value='a'*40+'\n'):
            with self.assertRaisesRegex(ValueError, 'ADAPTER_SIGNING_KEY_B64'):
                release_ci.main()

    def test_publisher_refuses_stale_main_before_loading_packages(self):
        class API:
            def head(self, branch): return 'b'*40
        with patch.object(github_automation, 'GitHub', return_value=API()), \
             patch('sys.argv', ['github_automation.py', 'publish-release', '--expected-main', 'a'*40]):
            with self.assertRaisesRegex(ValueError, 'main changed'):
                github_automation.main()


if __name__ == '__main__':
    unittest.main()
