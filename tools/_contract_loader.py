"""Minimal host fixture for parsing tests, not a replacement for live bridge QA."""
import importlib.util
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]


async def no_network(*args, **kwargs):
    raise AssertionError('Contract fixtures must not connect to a network')


def load_adapter(provider):
    if 'adapters' not in sys.modules:
        package = types.ModuleType('adapters')
        package.__path__ = []
        sys.modules['adapters'] = package
    base = types.ModuleType('adapters.base')
    class FixtureBase:
        def __init__(self, config, emit):
            self.config = config
            self.id = config.get('id', provider + '-fixture')
            self.name = config.get('name', provider)
            self.enabled = config.get('enabled', True)
            self.emit = emit

        async def status(self, *args, **kwargs):
            return None
    base.AdapterBase = FixtureBase
    sys.modules['adapters.base'] = base
    for name in ('websockets', 'websockets.asyncio', 'websockets.asyncio.client'):
        sys.modules[name] = types.ModuleType(name)
    sys.modules['websockets.asyncio.client'].connect = no_network
    name = 'adapters.' + provider
    spec = importlib.util.spec_from_file_location(name, ROOT / 'sources' / provider / 'adapter.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    def no_http(*args, **kwargs):
        raise AssertionError('Contract fixtures must not make HTTP requests')
    module.urlopen = no_http
    return module.Adapter
