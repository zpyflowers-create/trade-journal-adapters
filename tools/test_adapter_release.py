"""Offline tests. All generated signing keys live only in temporary directories."""
import argparse
import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

import adapter_release as release

ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.key = Path(cls.keys.name) / 'test.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                        '-out', str(cls.key)], check=True, capture_output=True)
        modulus = subprocess.run(['openssl', 'rsa', '-in', str(cls.key), '-modulus', '-noout'],
                                 check=True, capture_output=True, text=True).stdout.strip().split('=', 1)[1].lower()
        cls.signer = dict(id='offline-test', name='Offline test key', algorithm='rsa-pkcs1v15-sha256', n_hex=modulus, e=65537)
        cls.signer['fingerprint'] = release.digest((modulus + ':65537').encode())

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        self.public = self.root / 'trusted-signers' / 'offline-test.json'
        self.public.write_text(json.dumps(self.signer))
        self.args = argparse.Namespace(root=self.root, source=self.root / 'sources/bybit', version='1.1.6',
                                       private_key=self.key, signer=self.public, notes='Offline fixture only')

    def change_source(self):
        p = self.args.source / 'adapter.py'
        p.write_text(p.read_text() + '\n# Offline release-tool fixture\n')

    def test_existing_signed_packages(self):
        with contextlib.redirect_stdout(io.StringIO()):
            release.verify_registry(self.root)

    def test_reject_version_only_release_without_touching_registry(self):
        before = (self.root / 'registry.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'unchanged'):
            release.build(self.args)
        self.assertEqual((self.root / 'registry.json').read_bytes(), before)
        self.assertFalse((self.root / 'bybit/bybit-1.1.6.zip').exists())

    def test_reject_older_version(self):
        self.change_source()
        self.args.version = '1.1.4'
        with self.assertRaisesRegex(ValueError, 'exceed'):
            release.build(self.args)

    def test_reject_mismatched_signing_key_before_writes(self):
        self.change_source()
        self.args.signer = self.root / 'trusted-signers/trade-journal-adapter-publisher-20261023-r3.json'
        with self.assertRaisesRegex(ValueError, 'signature'):
            release.build(self.args)
        self.assertFalse((self.root / 'bybit/bybit-1.1.6.zip').exists())

    def test_build_verifiable_release_and_preserve_other_platform(self):
        self.change_source()
        original_gate = next(i for i in json.loads((self.root / 'registry.json').read_text())['adapters'] if i['type'] == 'gate')
        with contextlib.redirect_stdout(io.StringIO()):
            release.build(self.args)
            release.verify_registry(self.root)
        new_registry = json.loads((self.root / 'registry.json').read_text())
        self.assertEqual(next(i for i in new_registry['adapters'] if i['type'] == 'gate'), original_gate)
        data = (self.root / 'bybit/bybit-1.1.6.zip').read_bytes()
        result = release.check_package(data, self.signer, 'bybit', '1.1.6')
        self.assertEqual(result['signer_id'], 'offline-test')
        changed = dict(self.signer, id='unknown')
        with self.assertRaisesRegex(ValueError, 'Unknown signer'):
            release.check_package(data, changed)

    def test_tampered_archive_rejected(self):
        self.change_source()
        with contextlib.redirect_stdout(io.StringIO()):
            release.build(self.args)
        data = (self.root / 'bybit/bybit-1.1.6.zip').read_bytes()
        target = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(target, 'w') as out:
            for name in src.namelist():
                content = src.read(name)
                if name == 'adapter.py':
                    content += b'\n# tampered\n'
                out.writestr(name, content)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            release.check_package(target.getvalue(), self.signer)

    def test_private_material_not_accepted_as_source(self):
        self.change_source()
        (self.args.source / 'publisher.pem').write_text('not an actual key')
        with self.assertRaisesRegex(ValueError, 'unsupported files'):
            release.build(self.args)


if __name__ == '__main__':
    unittest.main()
