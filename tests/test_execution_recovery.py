"""Offline exchange-response replay; never connects to exchange accounts.

AdapterBase belongs to the separately deployed bridge. Its minimal test double
records emit/status calls; durable dedupe/position aggregation is NOT tested here.
"""
import asyncio
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


class BaseDouble:
    def __init__(self, config, emit):
        self.emit = emit
        self.id = config.get("id", "test")
        self.name = config.get("name", "test")
        self.enabled = True
        self.statuses = []

    async def status(self, state, message):
        self.statuses.append((state, message))


base = types.ModuleType("adapters.base")
base.AdapterBase = BaseDouble
sys.modules.setdefault("adapters", types.ModuleType("adapters"))
sys.modules["adapters.base"] = base
ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / name / "adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate, bybit = load("gate"), load("bybit")


def spot_fill(n):
    return {"id": str(n), "currency_pair": "BTC_USDT", "side": "buy",
            "create_time": str(1000 + n), "price": "100", "amount": "1"}


def bybit_fill(n):
    return {"execId": str(n), "orderId": str(n), "symbol": "BTCUSDT",
            "execTime": str(1000000 + n), "execPrice": "100", "execQty": "1", "side": "Buy"}


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def make(self, module):
        events = []
        async def emit(event):
            events.append(event)
        return module.Adapter({"enable_spot":True, "enable_perpetual":False}, emit), events

    async def test_bybit_timestamp_error_is_resigned_once(self):
        adapter, _ = self.make(bybit)
        adapter._check_credentials=lambda:None
        adapter._refresh_time_offset=lambda:None
        responses=[io.BytesIO(json.dumps({"retCode":10002,"retMsg":"req_timestamp error server_timestamp[12345000] recv_window"}).encode()),
                   io.BytesIO(json.dumps({"retCode":0,"result":{"list":[]}}).encode())]
        with patch.object(bybit,"urlopen",side_effect=responses) as request:
            result=adapter._rest_get("/v5/execution/list",{"category":"spot"})
        self.assertEqual(result["retCode"],0)
        self.assertEqual(request.call_count,2)
        first,last=[call.args[0] for call in request.call_args_list]
        self.assertNotEqual(first.get_header("X-bapi-sign"),last.get_header("X-bapi-sign"))

    async def test_gate_tls_error_retries_then_returns(self):
        adapter, _ = self.make(gate)
        adapter._check=lambda:None
        responses=[gate.URLError(gate.ssl.SSLError("synthetic TLS timeout")),io.BytesIO(b'[]')]
        with patch.object(gate,"urlopen",side_effect=responses) as request, patch.object(gate.time,"sleep"):
            self.assertEqual(adapter._get("/spot/my_trades"),[])
        self.assertEqual(request.call_count,2)

    async def test_gate_spot_more_than_1000_fills_globally_sorted(self):
        adapter, events = self.make(gate)
        rows = [spot_fill(n) for n in range(1505, 0, -1)]
        calls = []
        def get(path, params):
            calls.append(dict(params))
            off = (params["page"] - 1) * params["limit"]
            return rows[off:off+params["limit"]]
        adapter._get = get
        await adapter._poll_spot_once()
        self.assertEqual([x["execution_id"] for x in events], [str(n) for n in range(1,1506)])
        self.assertEqual([x["page"] for x in calls], [1,2])
        self.assertEqual(calls[0]["to"], calls[1]["to"])

    async def test_gate_futures_uses_offset_and_seconds_timestamp(self):
        adapter, _ = self.make(gate)
        calls = []
        def get(path, params):
            calls.append(dict(params))
            off = params["offset"]
            return [{"trade_id":str(n), "create_time":n+1, "contract":"BTC_USDT"}
                    for n in range(1204-off, max(0,1204-off-1000), -1)]
        adapter._get = get
        rows = await adapter._trade_rows("futures",1,2000)
        self.assertEqual(len(rows),1204)
        self.assertEqual([x["offset"] for x in calls],[0,1000])
        self.assertLess(rows[0]["create_time"],rows[-1]["create_time"])

    async def test_gate_second_page_failure_emits_nothing_and_retains_watermark(self):
        adapter, events = self.make(gate)
        adapter._spot_poll_from = 900
        def get(path, params):
            if params["page"] == 2:
                raise OSError("TLS timeout")
            return [spot_fill(n) for n in range(1,1001)]
        adapter._get = get
        with patch.object(gate.time,"time",return_value=1200):
            with self.assertRaises(OSError):
                await adapter._poll_spot_once()
        self.assertEqual(adapter._spot_poll_from,900)
        self.assertEqual(events,[])

    async def test_gate_initial_failure_window_is_anchored(self):
        adapter, _ = self.make(gate)
        def get(*args):
            raise OSError("timeout")
        adapter._get = get
        with patch.object(gate.time,"time",return_value=1200):
            with self.assertRaises(OSError):
                await adapter._poll_spot_once()
        with patch.object(gate.time,"time",return_value=1800):
            start, end = adapter._poll_window("spot")
        self.assertLessEqual(start,1140)
        self.assertEqual(end,1800)

    async def test_gate_emit_failure_does_not_advance_watermark(self):
        adapter, _ = self.make(gate)
        adapter._spot_poll_from = 900
        adapter._get = lambda *args: [spot_fill(1)]
        async def fail(event):
            raise OSError("queue unavailable")
        adapter.emit = fail
        with self.assertRaises(OSError):
            await adapter._poll_spot_once()
        self.assertEqual(adapter._spot_poll_from,900)

    async def test_gate_success_advances_watermark(self):
        adapter, _ = self.make(gate)
        adapter._spot_poll_from = 900
        adapter._get = lambda *args: []
        with patch.object(gate.time,"time",return_value=1200):
            await adapter._poll_spot_once()
        self.assertEqual(adapter._spot_poll_from,1200)

    async def test_gate_repeated_page_is_failure(self):
        adapter, _ = self.make(gate)
        adapter._get = lambda *args: [spot_fill(n) for n in range(1,1001)]
        with self.assertRaisesRegex(RuntimeError,"分页重复"):
            await adapter._trade_rows("spot",1,2000)

    async def test_gate_missing_id_or_time_is_failure(self):
        adapter, _ = self.make(gate)
        for row in [{"create_time":100}, {"id":"1"}]:
            adapter._get = lambda *args: [row]
            with self.assertRaises(RuntimeError):
                await adapter._trade_rows("spot",1,2000)

    async def test_gate_manual_history_uses_all_pages(self):
        adapter, events = self.make(gate)
        adapter.enable_futures = False
        adapter._get = lambda path,p: [spot_fill(n) for n in range(1,1001)] if p["page"]==1 else [spot_fill(1001)]
        result = await adapter.handle_command({"action":"history_sync","days":1})
        self.assertTrue(result["ok"])
        self.assertEqual(result["accepted"],1001)
        self.assertEqual(len(events),1001)

    async def test_bybit_all_pages_sorted_before_emit(self):
        adapter, events = self.make(bybit)
        calls = []
        def get(path,p):
            calls.append(dict(p))
            return {"retCode":0,"result":{"list":[bybit_fill(3),bybit_fill(4)] if not p.get("cursor") else [bybit_fill(1),bybit_fill(2)],
                                            "nextPageCursor":"next" if not p.get("cursor") else ""}}
        adapter._rest_get = get
        await adapter._history_category("spot",0,2000000)
        self.assertEqual([e["execution_id"] for e in events],["1","2","3","4"])
        self.assertTrue(all(e["category"]=="spot" for e in events))
        self.assertEqual(calls[1]["cursor"],"next")

    async def test_bybit_second_page_failure_emits_nothing(self):
        adapter, events = self.make(bybit)
        def get(path,p):
            if p.get("cursor"):
                raise OSError("timeout")
            return {"retCode":0,"result":{"list":[bybit_fill(2)],"nextPageCursor":"next"}}
        adapter._rest_get = get
        with self.assertRaises(OSError):
            await adapter._history_category("spot",0,2000000)
        self.assertEqual(events,[])

    async def test_bybit_cursor_cycle_is_failure(self):
        adapter, _ = self.make(bybit)
        adapter._rest_get = lambda *args: {"retCode":0,"result":{"list":[],"nextPageCursor":"same"}}
        with self.assertRaisesRegex(RuntimeError,"游标重复"):
            await adapter._history_category("spot",0,2000000)







if __name__ == "__main__":
    unittest.main()
