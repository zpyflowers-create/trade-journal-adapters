"""Sign a manually approved main revision; never execute Adapter code here."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import adapter_release

ROOT = Path(__file__).resolve().parents[1]


def main():
    typ = os.environ['ADAPTER_TYPE']
    version = os.environ['ADAPTER_VERSION']
    expected = os.environ['SOURCE_SHA']
    if os.environ.get('CONFIRM_RELEASE') != 'PUBLISH':
        raise ValueError('Manual release confirmation is required')
    if not re.fullmatch(r'[a-z][a-z0-9_-]*', typ) or not re.fullmatch(r'[0-9a-f]{40}', expected):
        raise ValueError('Invalid release inputs')
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if actual != expected:
        raise ValueError('Checked-out main is not the manually reviewed revision')
    providers = json.loads((ROOT / 'automation/providers.json').read_text())['providers']
    provider = providers.get(typ) or {}
    if provider.get('source') != f'sources/{typ}/adapter.py':
        raise ValueError('Provider must be onboarded with maintained source before CI release')
    test = ROOT / 'tools' / f'test_contract_{typ}.py'
    if not test.is_file():
        raise ValueError('Provider-specific contract tests are required before release')
    signer = ROOT / 'trusted-signers/trade-journal-adapter-publisher-20261023-r3.json'
    encoded_key = os.environ.get('ADAPTER_SIGNING_KEY_B64', '')
    if not encoded_key:
        raise ValueError('Configure ADAPTER_SIGNING_KEY_B64 in the adapter-release environment')
    key = base64.b64decode(encoded_key, validate=True)
    if len(key) > 20000:
        raise ValueError('Signing key exceeds size limit')
    with tempfile.TemporaryDirectory(prefix='adapter-signer-') as temp:
        key_path = Path(temp) / 'signer.pem'
        key_path.write_bytes(key)
        key_path.chmod(0o600)
        args = argparse.Namespace(root=ROOT, source=ROOT / 'sources' / typ,
                                  version=version, private_key=key_path, signer=signer,
                                  notes=os.environ['RELEASE_NOTES'], initial=False)
        adapter_release.build(args)
    adapter_release.verify_registry(ROOT)
    Path('release-ready.json').write_text(json.dumps({'base_sha': expected, 'type': typ, 'version': version}) + '\n')


if __name__ == '__main__':
    main()
