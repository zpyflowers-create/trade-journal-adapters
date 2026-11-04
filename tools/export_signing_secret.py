"""Prepare a local GitHub secret file without printing private-key material."""
import argparse
import base64
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-key', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output.is_relative_to(root):
        raise ValueError('Secret output must be outside the repository')
    key = args.private_key.read_bytes()
    if b'PRIVATE KEY' not in key or len(key) > 20000:
        raise ValueError('Select the private PEM from the R3 backup, not the public JSON')
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(base64.b64encode(key))
    print('Secret file prepared locally. Copy its contents only into the GitHub environment secret; do not commit it.')


if __name__ == '__main__':
    main()
