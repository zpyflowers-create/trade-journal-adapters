"""Run candidate checks against a separately supplied bridge source tree.

Usage: python scripts/verify_bridge_compatibility.py /path/to/project/sync
No exchange requests, credentials, existing databases, or deployed files used.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT=Path(__file__).resolve().parents[1]
BRIDGE_SYNC=Path(sys.argv.pop(1)).resolve()
sys.path.insert(0,str(BRIDGE_SYNC))
from execution_queue import ExecutionQueue
from reporting import review_revision
from security_utils import verify_manifest_signature
from bridge import Bridge


def load(name):
    spec=importlib.util.spec_from_file_location("candidate_"+name,ROOT/"src"/name/"adapter.py")
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_replay_cross_month_one_lifecycle_and_deduplication(self):
        mod=load("bybit")
        with tempfile.TemporaryDirectory() as tmp:
            q=ExecutionQueue(Path(tmp)/"queue.sqlite3")
            try:
                async def emit(e):
                    if e.get("type")!="execution":
                        return
                    inserted,p=q.enqueue(e)
                    result=q.ingest_execution(p["queue_id"])
                    self.assertTrue(result["ok"],result)
                adapter=mod.Adapter({"id":"fixture","account_id":"fixture-account"},emit)
                start=1790726400000
                rows=[]
                for n,(side,qty,closed) in enumerate([("Buy",1,0),("Buy",1,0),("Sell",0.5,0.5),("Sell",1.5,1.5),("Buy",1,0)],1):
                    rows.append({"execId":str(n),"orderId":str(n),"symbol":"BTCUSDT","execPrice":"100",
                                 "execQty":str(qty),"closedSize":str(closed),"side":side,
                                 "execTime":str(start+n*86400000)})
                def get(path,p):
                    return {"retCode":0,"result":{"list":rows[:3] if p.get("cursor") else rows[3:],
                                                   "nextPageCursor":"older" if not p.get("cursor") else ""}}
                adapter._rest_get=get
                await adapter._history_category("linear",start,start+7*86400000)
                all_trades=q.list_review_trades()
                self.assertEqual(len(all_trades),2)
                closed=next(x for x in all_trades if len(x["fills"])==4)
                self.assertEqual([x["action"] for x in closed["fills"]],["开仓","加仓","减仓","平仓"])
                self.assertEqual(closed["trade"]["remainingQty"],0)
                tid=closed["trade"]["id"]
                q.patch_review_fields(tid,{"stop":95,"note":"preserve review"},review_revision(closed["trade"]))
                await adapter._history_category("linear",start,start+7*86400000)
                self.assertEqual(len(q.list_review_trades()),2)
                current=next(x for x in q.list_review_trades() if x["trade"]["id"]==tid)
                self.assertEqual(len(current["fills"]),4)
                self.assertEqual(current["trade"]["stop"],95)
                self.assertEqual(current["trade"]["note"],"preserve review")
            finally:
                q.close()

    async def test_unprotected_conditional_remains_candidate(self):
        events=[]
        async def emit(e):events.append(e)
        adapter=load("bybit").Adapter({"id":"fixture"},emit)
        await adapter._handle_order_stop({"data":[{"category":"linear","symbol":"BTCUSDT","orderId":"fixture-order",
             "side":"Sell","triggerDirection":2,"triggerPrice":"95","stopOrderType":"Stop",
             "orderStatus":"Untriggered","positionIdx":0,"reduceOnly":False,"closeOnTrigger":False}]})
        risks=[e for e in events if e.get("type")=="risk_update"]
        self.assertEqual(len(risks),1)
        self.assertEqual(risks[0]["risk_type"],"stop_candidate")

    async def test_close_only_conditional_remains_protective_stop(self):
        events=[]
        async def emit(e):events.append(e)
        adapter=load("bybit").Adapter({"id":"fixture"},emit)
        await adapter._handle_order_stop({"data":[{"category":"linear","symbol":"BTCUSDT","orderId":"fixture-order",
             "side":"Sell","triggerDirection":2,"triggerPrice":"95","stopOrderType":"StopLoss",
             "orderStatus":"Untriggered","positionIdx":0,"reduceOnly":True,"closeOnTrigger":False}]})
        self.assertEqual(next(e for e in events if e.get("type")=="risk_update")["risk_type"],"protective_stop")

    async def test_existing_packages_validate_with_r2_and_reject_unknown_signer(self):
        signer=json.loads((ROOT/"trusted-signers/trade-journal-adapter-signer-20260920-r2.json").read_text())
        for name,version in [("bybit","1.1.2"),("gate","1.2.2")]:
            with zipfile.ZipFile(ROOT/name/f"{name}-{version}.zip") as z:
                manifest=json.loads(z.read("manifest.json"))
            self.assertTrue(verify_manifest_signature(manifest,{signer["id"]:signer})[0])
            self.assertFalse(verify_manifest_signature(manifest,{})[0])
            manifest["version"]="tampered"
            self.assertFalse(verify_manifest_signature(manifest,{signer["id"]:signer})[0])

    async def test_old_bybit_plugin_does_not_replace_newer_builtin(self):
        bridge=Bridge.__new__(Bridge)
        self.assertFalse(bridge._plugin_should_override_builtin("bybit",{"version":"1.1.2"}))
        self.assertTrue(bridge._plugin_should_override_builtin("bybit",{"version":"1.1.5"}))


if __name__=="__main__":
    unittest.main(verbosity=2)
