"""Offline exchange-response replay; never connects to exchange accounts.

AdapterBase belongs to the separately deployed bridge. Its minimal test double
records emit/status calls; durable dedupe/position aggregation is NOT tested here.
"""
import asyncio
import importlib.util
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

    async def test_bybit_retry_failed_recovery_window_without_advancing(self):
        adapter, _ = self.make(bybit)
        adapter.categories = ["spot","linear"]
        adapter._recovery_from_ms = 1000000
        adapter._now_ms = lambda: 2000000
        calls = []
        async def history(cat,start,end):
            calls.append((cat,start,end))
            if cat=="linear":
                raise OSError("rate limit")
            return 1
        adapter._history_category = history
        with self.assertRaises(OSError):
            await adapter._recover_executions()
        self.assertEqual(adapter._recovery_from_ms,1000000)
        self.assertEqual(calls[0][1],995000)

    async def test_bybit_long_gap_split_into_at_most_seven_days(self):
        adapter, _ = self.make(bybit)
        adapter._recovery_from_ms = 1000000
        adapter._now_ms = lambda: 1000000 + 10*86400000
        calls = []
        async def history(cat,start,end):
            calls.append((start,end))
            return 0
        adapter._history_category = history
        await adapter._recover_executions()
        self.assertEqual(len(calls),2)
        self.assertTrue(all(end-start<=7*86400000 for start,end in calls))
        self.assertEqual(calls[0][1],calls[1][0])

    async def test_bybit_fill_before_subscribe_ack_is_preserved(self):
        adapter, _ = self.make(bybit)
        fill = json.dumps({"topic":"execution","data":[bybit_fill(1)]})
        messages = iter([json.dumps({"op":"auth","success":True}),fill,
                         json.dumps({"op":"subscribe","success":True})])
        class WS:
            async def send(self,raw):
                pass
            async def recv(self):
                return next(messages)
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise StopAsyncIteration
        ws=WS()
        await adapter._auth_and_subscribe(ws)
        self.assertEqual([raw async for raw in adapter._execution_messages(ws)],[fill])

    async def test_bybit_malformed_success_does_not_advance_recovery(self):
        adapter, _ = self.make(bybit)
        adapter._recovery_from_ms=1000000
        adapter._now_ms=lambda:2000000
        adapter._rest_get=lambda *args:{"retCode":0}
        with self.assertRaisesRegex(RuntimeError,"缺少 list"):
            await adapter._recover_executions()
        self.assertEqual(adapter._recovery_from_ms,1000000)

    async def test_bybit_run_backfills_disconnect_before_resuming_live(self):
        adapter, events = self.make(bybit)
        adapter._check_credentials=lambda:None
        adapter.categories=["spot"]
        adapter._now_ms=lambda:1000100
        adapter._refresh_time_offset=lambda:None
        sessions=[]
        rest_calls=[]
        def get(path,params):
            rest_calls.append(dict(params))
            # Trade 3 happened while disconnected and never appears on WS.
            n=1 if len(sessions)==1 else 3
            return {"retCode":0,"result":{"list":[bybit_fill(n)],"nextPageCursor":""}}
        adapter._rest_get=get
        class WS:
            def __init__(self,number):
                self.number=number
                self.acks=iter([json.dumps({"op":"auth","success":True}),
                                json.dumps({"op":"subscribe","success":True})])
                self.done=False
            async def __aenter__(self):
                return self
            async def __aexit__(self,*args):
                return False
            async def send(self,raw):
                pass
            async def recv(self):
                return next(self.acks)
            def __aiter__(self):
                return self
            async def __anext__(self):
                if self.done:
                    if self.number==1:
                        raise OSError("connection dropped")
                    raise asyncio.CancelledError
                self.done=True
                x=bybit_fill(2 if self.number==1 else 4)
                x["category"]="spot"
                return json.dumps({"topic":"execution","data":[x]})
        def connect(*args,**kwargs):
            sessions.append(WS(len(sessions)+1))
            return sessions[-1]
        async def heartbeat(ws):
            await asyncio.Future()
        adapter._heartbeat=heartbeat
        async def no_delay(*args):
            pass
        with patch.object(bybit,"connect",side_effect=connect), patch.object(bybit.asyncio,"sleep",side_effect=no_delay):
            with self.assertRaises(asyncio.CancelledError):
                await adapter.run()
        self.assertEqual([e["execution_id"] for e in events],["1","2","3","4"])
        self.assertEqual(len(rest_calls),2)
        self.assertLessEqual(rest_calls[1]["startTime"],1000002)


if __name__ == "__main__":
    unittest.main()
