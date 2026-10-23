"""Build a signed package locally using an EXISTING trusted signing key.

This script never generates keys, uploads files, or edits registry.json.
Requires cryptography and the deployment's sync/security_utils.py.
"""
import argparse
import base64
import getpass
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import zipfile

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def load_security(bridge_sync):
    spec=importlib.util.spec_from_file_location("release_security",Path(bridge_sync)/"security_utils.py")
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(source_dir, version, description, signer_record, private_key_path,
          bridge_sync, password=None):
    """Return package bytes and manifest. No writes and no remote operations."""
    source_dir=Path(source_dir)
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-rc\d+)?",version):
        raise ValueError("版本必须为 x.y.z 或 x.y.z-rcN")
    if not description.strip():
        raise ValueError("必须提供本次发布说明")
    security=load_security(bridge_sync)
    signer=security.validate_signer_record(signer_record)
    private=serialization.load_pem_private_key(Path(private_key_path).read_bytes(),password=password)
    if not isinstance(private,rsa.RSAPrivateKey):
        raise ValueError("必须使用与可信公钥匹配的 RSA 私钥")
    public=private.public_key().public_numbers()
    if public.n!=int(signer["n_hex"],16) or public.e!=signer["e"]:
        raise ValueError("签名私钥不匹配所选可信公钥，未生成安装包")
    manifest=json.loads((source_dir/"manifest.template.json").read_text())
    if manifest.get("type") not in {"bybit","gate"} or manifest.get("entrypoint")!="adapter.py":
        raise ValueError("不支持的 Adapter 模板")
    source=source_dir/"adapter.py"
    if source.is_symlink() or not source.is_file():
        raise ValueError("入口必须为普通源码文件")
    content=source.read_bytes()
    # Compile in memory only; never run the adapter during packaging.
    compile(content,str(source),"exec")
    changelog=(f"# {manifest['name']} {version}\n\n{description.strip()}\n").encode()
    files={"adapter.py":content,"CHANGELOG.md":changelog}
    manifest.update(version=version,description=description.strip(),signer_id=signer["id"],
                    files={name:hashlib.sha256(data).hexdigest() for name,data in files.items()})
    manifest.pop("signature",None)
    signature=private.sign(security.canonical_manifest_payload(manifest),padding.PKCS1v15(),hashes.SHA256())
    manifest["signature"]=base64.b64encode(signature).decode("ascii")
    if not security.verify_manifest_signature(manifest,{signer["id"]:signer})[0]:
        raise ValueError("生成后验签失败")
    files["manifest.json"]=(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n").encode()
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,"w",compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(files.items()):
            info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED
            info.create_system=3
            info.external_attr=0o100600<<16
            archive.writestr(info,data)
    return stream.getvalue(),manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir",type=Path)
    parser.add_argument("--bridge-sync",type=Path,required=True)
    parser.add_argument("--version",required=True)
    parser.add_argument("--description",required=True)
    parser.add_argument("--signer",type=Path,required=True)
    parser.add_argument("--private-key",type=Path,required=True)
    parser.add_argument("--password-prompt",action="store_true",help="在本机隐藏输入私钥口令")
    parser.add_argument("--output-dir",type=Path,required=True)
    args=parser.parse_args()
    password=getpass.getpass("私钥口令（仅在本机使用）：").encode() if args.password_prompt else None
    signer=json.loads(args.signer.read_text())
    data,manifest=build(args.source_dir,args.version,args.description,signer,args.private_key,
                        args.bridge_sync,password=password)
    # A fresh output directory makes an interrupted build obvious and prevents
    # accidental replacement of an already reviewed release.
    args.output_dir.mkdir(parents=True,exist_ok=False)
    filename=f"{manifest['type']}-{manifest['version']}.zip"
    package=args.output_dir/filename
    package.write_bytes(data)
    checksum=hashlib.sha256(data).hexdigest()
    (args.output_dir/(filename[:-4]+".sha256")).write_text(f"{checksum}  {filename}\n")
    entry={"type":manifest["type"],"name":manifest["name"],"version":manifest["version"],
           "sha256":checksum,"min_bridge_version":"2.10.0","release_notes":manifest["description"],
           "package_url":"REVIEW_REQUIRED_SET_FINAL_HTTPS_URL","published_at":"REVIEW_REQUIRED"}
    (args.output_dir/"registry-entry.draft.json").write_text(json.dumps(entry,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"package":str(package.resolve()),"sha256":checksum,"signer_id":manifest["signer_id"],
                      "signature_verified":True,"registry_updated":False},ensure_ascii=False))


if __name__=="__main__":
    main()
