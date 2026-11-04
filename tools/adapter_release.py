"""Build signed Adapter releases or verify a registry, without contacting a NAS."""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import hmac
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import zipfile
from datetime import date

FIELDS = ('type', 'name', 'version', 'description', 'entrypoint', 'fields', 'files')
ALLOWED = {'.py', '.json', '.txt', '.md'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(manifest):
    return json.dumps({k: manifest.get(k) for k in FIELDS}, ensure_ascii=False,
                      sort_keys=True, separators=(',', ':')).encode('utf-8')


def version(value):
    if not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError('Version must have three numeric components')
    return tuple(map(int, value.split('.')))


def check_package(data, signer, expected_type=None, expected_version=None):
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('Package exceeds 8 MiB')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        names = [i.filename for i in infos]
        if len(names) != len(set(names)) or len(names) > 64:
            raise ValueError('Duplicate names or too many package files')
        if sum(i.file_size for i in infos) > 24 * 1024 * 1024:
            raise ValueError('Unpacked package exceeds 24 MiB')
        for i in infos:
            p = PurePosixPath(i.filename)
            if (p.is_absolute() or '..' in p.parts or '\\' in i.filename
                    or i.file_size > 4 * 1024 * 1024
                    or stat.S_ISLNK(i.external_attr >> 16)
                    or (not i.is_dir() and p.suffix.lower() not in ALLOWED)):
                raise ValueError('Unsafe package entry')
        m = json.loads(z.read('manifest.json'))
        if m.get('type') != expected_type and expected_type is not None:
            raise ValueError('Adapter type mismatch')
        if m.get('version') != expected_version and expected_version is not None:
            raise ValueError('Adapter version mismatch')
        version(m['version'])
        if m.get('signer_id') != signer['id']:
            raise ValueError('Unknown signer')
        if signer.get('algorithm') != 'rsa-pkcs1v15-sha256':
            raise ValueError('Unsupported signer algorithm')
        n = int(signer['n_hex'], 16)
        e = int(signer['e'])
        if not 2048 <= n.bit_length() <= 4096 or e < 3 or e % 2 == 0:
            raise ValueError('Invalid RSA public key')
        fingerprint = digest((signer['n_hex'].lower() + ':' + str(e)).encode('ascii'))
        if signer.get('fingerprint') != fingerprint:
            raise ValueError('Public-key fingerprint mismatch')
        sig = base64.b64decode(m['signature'], validate=True)
        size = (n.bit_length() + 7) // 8
        if len(sig) != size or int.from_bytes(sig, 'big') >= n:
            raise ValueError('Invalid RSA signature size')
        actual = pow(int.from_bytes(sig, 'big'), e, n).to_bytes(size, 'big')
        tail = bytes.fromhex('3031300d060960864801650304020105000420') + hashlib.sha256(canonical(m)).digest()
        expected = b'\x00\x01' + b'\xff' * (size - len(tail) - 3) + b'\x00' + tail
        if not hmac.compare_digest(actual, expected):
            raise ValueError('Invalid manifest signature')
        hashes = m.get('files')
        actual_files = {i.filename for i in infos if not i.is_dir() and i.filename != 'manifest.json'}
        if not isinstance(hashes, dict) or set(hashes) != actual_files:
            raise ValueError('Signature must cover every package file')
        if m.get('entrypoint') not in hashes or not m['entrypoint'].endswith('.py'):
            raise ValueError('Missing Python entrypoint')
        for name, sha in hashes.items():
            content = z.read(name)
            if digest(content) != sha:
                raise ValueError('Package file checksum mismatch')
            if name.endswith('.py'):
                ast.parse(content, filename=name)
        return m


def verify_registry(root):
    registry = json.loads((root / 'registry.json').read_text())
    if registry.get('schema_version') != 1 or not isinstance(registry.get('adapters'), list):
        raise ValueError('Unsupported registry schema')
    signers = {}
    for p in (root / 'trusted-signers').glob('*.json'):
        s = json.loads(p.read_text())
        if s['id'] in signers and signers[s['id']] != s:
            raise ValueError('Conflicting public signer records')
        signers[s['id']] = s
    seen = set()
    for item in registry['adapters']:
        typ, ver = item['type'], item['version']
        if not re.fullmatch(r'[a-z][a-z0-9_-]*', typ) or typ in seen:
            raise ValueError('Invalid or duplicate registry Adapter type')
        seen.add(typ)
        version(ver)
        filename = f'{typ}-{ver}.zip'
        suffix = f'/{typ}/{filename}'
        if not item['package_url'].startswith('https://') or not item['package_url'].endswith(suffix):
            raise ValueError('Registry package URL mismatch')
        data = (root / typ / filename).read_bytes()
        if digest(data) != item['sha256']:
            raise ValueError('Registry SHA-256 mismatch')
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            sid = json.loads(z.read('manifest.json')).get('signer_id')
        if sid not in signers:
            raise ValueError('Missing trusted public signer record')
        check_package(data, signers[sid], typ, ver)
        checksum = root / typ / f'{typ}-{ver}.sha256'
        if checksum.read_text().split() != [item['sha256'], filename]:
            raise ValueError('Companion checksum mismatch')
        print(f'Verified {typ} {ver}: hash, signature, complete files and Python syntax')


def build(args):
    root, source = args.root.resolve(), args.source.resolve()
    registry_path = root / 'registry.json'
    registry = json.loads(registry_path.read_text())
    m = json.loads((source / 'manifest.json').read_text())
    typ = m['type']
    if not re.fullmatch(r'[a-z][a-z0-9_-]*', typ):
        raise ValueError('Invalid Adapter type')
    old = next((i for i in registry['adapters'] if i['type'] == typ), None)
    if old is None:
        raise ValueError('New Adapter onboarding requires a reviewed registry entry first')
    if version(args.version) <= version(old['version']):
        raise ValueError('New version must exceed the published version')
    files = {}
    for p in sorted(source.rglob('*')):
        if p.is_symlink():
            raise ValueError('Symlinks are not allowed in release sources')
        if p.is_dir():
            continue
        name = p.relative_to(source).as_posix()
        if name == 'manifest.json':
            continue
        if p.suffix.lower() not in ALLOWED:
            raise ValueError('Source contains unsupported files; keep keys outside the source tree')
        files[name] = p.read_bytes()
    hashes = {name: digest(data) for name, data in files.items()}
    with zipfile.ZipFile(root / typ / f"{typ}-{old['version']}.zip") as z:
        previous = json.loads(z.read('manifest.json'))['files']
    if {k: v for k, v in hashes.items() if k.endswith('.py')} == {k: v for k, v in previous.items() if k.endswith('.py')}:
        raise ValueError('Python source is unchanged; refusing a version-only release')
    m = {k: m.get(k) for k in FIELDS}
    m.update(version=args.version, files=hashes)
    signer = json.loads(args.signer.read_text())
    signed = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(args.private_key.resolve())],
                            input=canonical(m), capture_output=True)
    if signed.returncode:
        raise ValueError('OpenSSL signing failed; check the private key locally')
    m.update(signer_id=signer['id'], signature=base64.b64encode(signed.stdout).decode('ascii'))
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in [*files.items(), ('manifest.json', (json.dumps(m, ensure_ascii=False, indent=2) + '\n').encode())]:
            entry = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            z.writestr(entry, data)
    data = out.getvalue()
    check_package(data, signer, typ, args.version)
    filename = f'{typ}-{args.version}.zip'
    package = root / typ / filename
    if package.exists():
        raise ValueError('Release already exists; immutable packages cannot be overwritten')
    old.update(version=args.version, sha256=digest(data), published_at=date.today().isoformat(),
               release_notes=args.notes,
               package_url=old['package_url'].rsplit('/', 1)[0] + '/' + filename)
    public_path = root / 'trusted-signers' / f"{signer['id']}.json"
    if not public_path.exists():
        raise ValueError('Public signer must already be staged in trusted-signers')
    if json.loads(public_path.read_text()) != signer:
        raise ValueError('Staged public signer differs from the selected signer')
    package.write_bytes(data)
    package.with_suffix('.sha256').write_text(f'{digest(data)}  {filename}\n')
    # Publish the registry last; local generation does not upload or install anything.
    with tempfile.NamedTemporaryFile('w', dir=root, delete=False, encoding='utf-8') as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
        f.write('\n')
        tmp = Path(f.name)
    tmp.replace(registry_path)
    print(f'Built and signed {filename}. Review, test, then commit the package and registry together.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    verify = sub.add_parser('verify')
    verify.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    release = sub.add_parser('build')
    release.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    release.add_argument('--source', type=Path, required=True)
    release.add_argument('--version', required=True)
    release.add_argument('--private-key', type=Path, required=True)
    release.add_argument('--signer', type=Path, required=True)
    release.add_argument('--notes', required=True)
    args = p.parse_args()
    try:
        verify_registry(args.root) if args.command == 'verify' else build(args)
    except (ValueError, KeyError, OSError, SyntaxError, zipfile.BadZipFile) as e:
        p.exit(1, f'Release rejected: {e}\n')


if __name__ == '__main__':
    main()
