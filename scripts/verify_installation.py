"""Isolated V2.10.0 installer replay using an ephemeral TEST key.

Usage: python scripts/verify_installation.py /path/to/v2.10.0/sync
The test key is neither written nor registered outside its temporary directory.
Exchange networking, provider startup and HTTPS transport are mocked; validation,
file installation, module loading, ACTIVE selection and rollback use real Bridge.
"""
import asyncio
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT=Path(__file__).resolve().parents[1]
BRIDGE_SYNC=Path(sys.argv.pop(1)).resolve()
sys.path.insert(0,str(BRIDGE_SYNC))
import bridge as bridge_module
from bridge import Bridge
from security_utils import validate_signer_record
from build_signed_adapter import build


class InstallationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        public=cls.test_key.public_key().public_numbers()
        cls.signer=validate_signer_record({"id":"ephemeral-test-only","n_hex":format(public.n,"x"),"e":public.e})

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.key_path=self.root/"test-only.pem"
        self.key_path.write_bytes(self.test_key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        self.key_path.chmod(0o600)
        self.b=Bridge.__new__(Bridge)
        self.b.config={"providers":[{"id":"test-provider","type":"bybit","enabled":True}],"security":{}}
        self.b.provider_status={}
        self.b.trusted_signers={self.signer["id"]:self.signer}
        self.b._plugin_type_root=lambda typ:self.root/"plugins"/typ
        self.b._manager_payload=lambda:{}
        self.history=[]
        self.b._append_adapter_update_history=lambda entry:self.history.append(entry)
        self.restarts=[]
        async def restart(typ):self.restarts.append(typ)
        self.b._restart_type=restart
        self.data,self.manifest=build(ROOT/"src/bybit","1.1.5-rc1","Offline installer test only",self.signer,self.key_path,BRIDGE_SYNC)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def configure_remote(self,minimum="2.10.0",checksum=None):
        self.b.remote_registry={"adapters":[{"type":"bybit","version":"1.1.5-rc1","name":"Bybit",
               "package_url":"https://example.invalid/offline-test.zip","min_bridge_version":minimum,
               "sha256":checksum or hashlib.sha256(self.data).hexdigest()}]}
        self.b._catalog_entry=lambda typ:{"type":typ,"version":"1.1.4","name":"Bybit"}
        # The real compatibility function only needs the catalog/version data.
        self.b._catalog=lambda:[{"type":"bybit","version":"1.1.4","name":"Bybit"}]
        self.b._fetch_https_bytes=lambda *args:self.data

    async def test_reproducible_package_with_existing_key(self):
        again,_=build(ROOT/"src/bybit","1.1.5-rc1","Offline installer test only",self.signer,self.key_path,BRIDGE_SYNC)
        self.assertEqual(self.data,again)
        self.assertEqual(self.b._validate_adapter_zip(self.data,require_signature=True)[1:3],("bybit","1.1.5-rc1"))

    async def test_gate_candidate_package_uses_same_validator(self):
        data,_=build(ROOT/"src/gate","1.2.3-rc1","Offline installer test only",self.signer,self.key_path,BRIDGE_SYNC)
        self.assertEqual(self.b._validate_adapter_zip(data,require_signature=True)[1:3],("gate","1.2.3-rc1"))

    async def test_wrong_key_is_rejected_before_packaging(self):
        other=dict(self.signer,n_hex=format(self.test_key.public_key().public_numbers().n+2,"x"))
        with self.assertRaisesRegex(ValueError,"不匹配"):
            build(ROOT/"src/bybit","1.1.5-rc1","test",other,self.key_path,BRIDGE_SYNC)

    async def test_corrupted_hash_never_changes_active_version(self):
        with self.assertRaisesRegex(ValueError,"SHA256"):
            await self.b._install_adapter_bytes(self.data,"0"*64,require_signature=True)
        self.assertFalse(self.b._plugin_active_file("bybit").exists())
        self.assertEqual(self.restarts,[])

    async def test_unknown_signer_is_rejected(self):
        self.b.trusted_signers={}
        with self.assertRaisesRegex(ValueError,"可信数字签名"):
            self.b._validate_adapter_zip(self.data,require_signature=True)

    async def test_tampered_source_is_rejected(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.data)) as source,zipfile.ZipFile(stream,"w") as out:
            for name in source.namelist():out.writestr(name,source.read(name)+(b"\n# tampered\n" if name=="adapter.py" else b""))
        with self.assertRaisesRegex(ValueError,"SHA256"):
            self.b._validate_adapter_zip(stream.getvalue(),require_signature=True)

    async def test_install_and_fresh_bridge_load_exact_candidate(self):
        await self.b._install_adapter_bytes(self.data,hashlib.sha256(self.data).hexdigest(),require_signature=True)
        self.assertEqual(self.b._active_plugin_info("bybit")["version"],"1.1.5-rc1")
        fresh=Bridge.__new__(Bridge)
        fresh._plugin_type_root=self.b._plugin_type_root
        with patch.object(bridge_module,"PLUGIN_ROOT",self.root/"plugins"):
            module=fresh._load_adapter_module("bybit")
        self.assertTrue(hasattr(module.Adapter,"_handle_order_stop"))
        self.assertEqual(Path(module.__file__).read_bytes(),(ROOT/"src/bybit/adapter.py").read_bytes())

    async def test_download_failure_leaves_active_untouched(self):
        self.configure_remote()
        def unavailable(*args):raise OSError("synthetic download interrupted")
        self.b._fetch_https_bytes=unavailable
        with self.assertRaises(OSError):
            await self.b._handle_bridge_command({"action":"adapter_install_remote","payload":{"type":"bybit"}})
        self.assertFalse(self.b._plugin_active_file("bybit").exists())
        self.assertEqual(self.restarts,[])

    async def test_minimum_bridge_version_rejects_before_download(self):
        self.configure_remote(minimum="99.0.0")
        downloads=[]
        self.b._fetch_https_bytes=lambda *args:downloads.append(args)
        result=await self.b._handle_bridge_command({"action":"adapter_install_remote","payload":{"type":"bybit"}})
        self.assertFalse(result["ok"])
        self.assertEqual(downloads,[])

    async def test_immediate_error_rolls_back_to_previous_plugin(self):
        old=self.b._plugin_type_root("bybit")/"1.1.2"
        old.mkdir(parents=True)
        (old/"adapter.py").write_text("# previous test plugin\n")
        (old/"manifest.json").write_text(json.dumps({"type":"bybit","version":"1.1.2"}))
        self.b._plugin_active_file("bybit").write_text("1.1.2")
        self.configure_remote()
        async def restart(typ):
            self.restarts.append(typ)
            self.b.provider_status["test-provider"]={"status":"error"}
        self.b._restart_type=restart
        async def no_delay(*args):pass
        with patch.object(bridge_module.asyncio,"sleep",side_effect=no_delay):
            result=await self.b._handle_bridge_command({"action":"adapter_install_remote","payload":{"type":"bybit"}})
        self.assertEqual(result["kind"],"adapter_update_rolled_back")
        self.assertEqual(self.b._active_plugin_info("bybit")["version"],"1.1.2")
        self.assertTrue((old/"adapter.py").is_file())

    async def test_manual_rollback_removes_only_active_marker(self):
        await self.b._install_adapter_bytes(self.data,require_signature=True)
        result=await self.b._handle_bridge_command({"action":"adapter_rollback","payload":{"type":"bybit"}})
        self.assertTrue(result["ok"])
        self.assertFalse(self.b._plugin_active_file("bybit").exists())
        self.assertTrue((self.b._plugin_type_root("bybit")/"1.1.5-rc1"/"adapter.py").exists())

    async def test_connecting_at_probe_is_not_confirmed_healthy(self):
        self.configure_remote()
        async def connecting(typ):
            self.b.provider_status["test-provider"]={"status":"connecting"}
        self.b._restart_type=connecting
        async def no_delay(*args):pass
        with patch.object(bridge_module.asyncio,"sleep",side_effect=no_delay):
            result=await self.b._handle_bridge_command({"action":"adapter_install_remote","payload":{"type":"bybit"}})
        # Regression evidence: V2.10.0's 1.5s probe only rejects status=error.
        # This PASS records the limitation; it does NOT certify live readiness.
        self.assertTrue(result["ok"])
        self.assertEqual(self.b.provider_status["test-provider"]["status"],"connecting")


if __name__=="__main__":
    unittest.main(verbosity=2)
