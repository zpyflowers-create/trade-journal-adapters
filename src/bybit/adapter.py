from __future__ import annotations
import asyncio, hashlib, hmac, json, random, time, re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from websockets.asyncio.client import connect
from adapters.base import AdapterBase

class Adapter(AdapterBase):
    TYPE = "bybit"

    def __init__(self, config, emit):
        super().__init__(config, emit)
        self.api_key = str(config.get("api_key") or "").strip()
        self.api_secret = str(config.get("api_secret") or "").strip()
        # New unified environment selector; keep old boolean flags as a migration fallback.
        env = str(config.get("environment") or "").lower().strip()
        if not env:
            env = "demo" if bool(config.get("demo", False)) else ("testnet" if bool(config.get("testnet", False)) else "live")
        if env not in {"live","testnet","demo"}: env="live"
        self.environment=env; self.testnet=env=="testnet"; self.demo=env=="demo"
        legacy_categories=[str(x) for x in (config.get("categories") or ["linear","spot"])]
        self.enable_spot = bool(config.get("enable_spot", "spot" in legacy_categories))
        self.enable_perpetual = bool(config.get("enable_perpetual", "linear" in legacy_categories))
        self.categories = ([] if not self.enable_perpetual else ["linear"]) + ([] if not self.enable_spot else ["spot"])
        self.account_id = str(config.get("account_id") or "").strip()
        if self.demo:
            self.ws_url = "wss://stream-demo.bybit.com/v5/private"
            self.rest_base = "https://api-demo.bybit.com"
        elif self.testnet:
            self.ws_url = "wss://stream-testnet.bybit.com/v5/private"
            self.rest_base = "https://api-testnet.bybit.com"
        else:
            self.ws_url = "wss://stream.bybit.com/v5/private"
            self.rest_base = "https://api.bybit.com"
        self.recv_window = str(int(config.get("recv_window") or 5000))
        self.time_offset_ms = 0
        self._connected_once = False
        # Live derivative executions do not include leverage in Bybit V5 execution payloads.
        # Capture a post-execution position snapshot so the journal can store leverage / IM / position value.
        # Historical execution sync intentionally does not use a current position snapshot, because that
        # could rewrite old trades with today's leverage setting.
        self._position_snapshot_cache = {}
        self._risk_seen = {}
        self._risk_baselined = False
        self._risk_warn_at = 0.0
        self._risk_failures = 0
        self._risk_unavailable = False
        self._recovery_from_ms = None
        self._pending_ws_messages = []

    def _check_credentials(self):
        if not self.api_key or not self.api_secret or self.api_key.startswith("PUT_") or self.api_secret.startswith("PUT_"):
            raise RuntimeError("Bybit API Key / Secret 尚未配置。请在网页“交易接口管理”中填写并保存")
        if not self.categories:
            raise RuntimeError("Bybit 至少需要启用“监听现货”或“监听 USDT 永续”中的一个")

    def _now_ms(self):
        return int(time.time() * 1000) + int(self.time_offset_ms)

    def _public_get(self, path, params=None):
        query = urlencode(params or {})
        url = self.rest_base + path + (("?" + query) if query else "")
        req = Request(url, headers={"User-Agent":"TradingReviewBridge/1.4-nas"})
        try:
            with urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 403:
                raise RuntimeError("Bybit API 返回 403：当前网络/IP 可能被限制，或区域 API 域名不正确") from e
            raise RuntimeError(f"Bybit HTTP {e.code}: {e.reason}") from e
        except URLError as e:
            raise RuntimeError(f"Bybit 网络连接失败: {e.reason}") from e

    def _refresh_time_offset(self):
        # Estimate local time at the server-response midpoint. This is materially more
        # stable than subtracting the local clock after a VPS round trip has completed.
        t0 = int(time.time() * 1000)
        data = self._public_get("/v5/market/time")
        t1 = int(time.time() * 1000)
        server_ms = int((data or {}).get("time") or 0)
        if not server_ms:
            result = (data or {}).get("result") or {}
            sec = result.get("timeSecond")
            if sec:
                server_ms = int(float(sec) * 1000)
        if server_ms:
            local_mid = (t0 + t1) // 2
            self.time_offset_ms = server_ms - local_mid
        return self.time_offset_ms

    @staticmethod
    def _is_timestamp_error(data):
        if not isinstance(data, dict):
            return False
        if int(data.get("retCode") or 0) == 10002:
            return True
        msg = str(data.get("retMsg") or "").lower()
        return "timestamp" in msg and ("recv_window" in msg or "server_timestamp" in msg)

    def _apply_server_time_from_error(self, data):
        server_ms = int((data or {}).get("time") or 0) if isinstance(data, dict) else 0
        if not server_ms:
            msg = str((data or {}).get("retMsg") or "") if isinstance(data, dict) else ""
            m = re.search(r"server_timestamp\[(\d+)\]", msg)
            if m:
                server_ms = int(m.group(1))
        if server_ms:
            self.time_offset_ms = server_ms - int(time.time() * 1000)
            return True
        return False

    async def _heartbeat(self, ws):
        while True:
            await asyncio.sleep(20)
            await ws.send(json.dumps({"op": "ping"}))

    @staticmethod
    def _fnum(v, default=0.0):
        try: return float(v)
        except Exception: return default

    def _position_snapshot(self, category, symbol, position_idx=None, side=""):
        """Read the current Bybit derivative position immediately after a live execution.

        This is deliberately used only by the live websocket path. Bybit execution history
        does not carry the leverage that was active at the historical fill, so using a
        current position row while backfilling would create false historical precision.
        """
        category=str(category or "").lower().strip(); symbol=str(symbol or "").upper().strip()
        if category not in {"linear","inverse"} or not symbol:
            return {}
        data={}; rows=[]
        for attempt in range(3):
            data=self._rest_get("/v5/position/list", {"category":category,"symbol":symbol})
            if data.get("retCode") != 0:
                raise RuntimeError(f"Bybit position/list: {data.get('retMsg') or data}")
            rows=((data.get("result") or {}).get("list") or [])
            if rows:
                # A just-filled position may need a few hundred ms before leverage/IM is visible.
                if any(self._fnum(r.get("leverage"),0.0)>0 or self._fnum(r.get("positionIM"),0.0)>0 or self._fnum(r.get("positionValue"),0.0)!=0 for r in rows):
                    break
            if attempt<2:time.sleep((0.20,0.55)[attempt])
        if not rows:
            return {}
        wanted_idx=None
        try:
            if position_idx not in (None,""): wanted_idx=int(position_idx)
        except Exception:
            wanted_idx=None
        candidates=rows
        if wanted_idx is not None:
            matched=[r for r in rows if int(self._fnum(r.get("positionIdx"),-999))==wanted_idx]
            if matched: candidates=matched
        side_norm=str(side or "").lower()
        if len(candidates)>1 and side_norm in {"buy","sell"}:
            matched=[r for r in candidates if str(r.get("side") or "").lower()==side_norm]
            if matched: candidates=matched
        # Prefer a non-zero position row; when the fill closes the position, Bybit still
        # returns the symbol row and commonly preserves the configured leverage.
        row=next((r for r in candidates if abs(self._fnum(r.get("size"),0.0))>0), candidates[0])
        lev=self._fnum(row.get("leverage"),0.0)
        pim=self._fnum(row.get("positionIM"),0.0)
        pmm=self._fnum(row.get("positionMM"),0.0)
        pval=self._fnum(row.get("positionValue"),0.0)
        flat=str(row.get("size") or "") in {"0","0.0","0.00"}
        return {
            "leverage": lev if lev>0 else None,
            # Keep unified-margin compatibility: position IM remains the capital-usage snapshot.
            "position_margin_snapshot": abs(pim) if pim>0 else (0.0 if flat else None),
            "initial_margin_snapshot": abs(pim) if pim>0 else (0.0 if flat else None),
            "maintenance_margin_snapshot": abs(pmm) if pmm>0 else (0.0 if flat else None),
            "position_value_snapshot": abs(pval) if pval else (0.0 if flat else None),
            "position_idx": row.get("positionIdx"),
            "margin_mode": "portfolio" if not str(row.get("leverage") or "").strip() and not str(row.get("positionIM") or "").strip() else "",
            "margin_snapshot_source": "bybit_position_list",
        }

    def _normalize(self, x, snapshot=None):
        fnum=self._fnum
        exec_time = x.get("execTime") or x.get("transTime") or int(time.time() * 1000)
        try: exec_time = int(exec_time)
        except Exception: exec_time = int(time.time() * 1000)
        category=str(x.get("category") or "").lower(); price=fnum(x.get("execPrice")); qty=abs(fnum(x.get("execQty"))); notional=fnum(x.get("execValue"),0.0) or (abs(price*qty) if price>0 and qty>0 else 0.0)
        snap=dict(snapshot or {}); lev=fnum(x.get("leverage"),0.0) or fnum(snap.get("leverage"),0.0); margin=(abs(notional)/lev) if notional and lev>0 else None
        event={
            "type": "execution", "schema_version": 2,
            "provider": self.id, "provider_type": self.TYPE, "provider_name": self.name,
            "account_id": self.account_id, "execution_id": str(x.get("execId") or ""), "order_id": str(x.get("orderId") or ""),
            "symbol": str(x.get("symbol") or "").upper(), "asset_type": "虚拟币", "side": str(x.get("side") or ""),
            "price": price, "qty": qty, "notional": abs(notional) if notional else None, "notional_source": "exchange_exec_value" if fnum(x.get("execValue"),0.0) else "price_x_qty",
            "leverage": lev or None, "margin_estimate": margin, "margin_source": "notional_over_leverage" if margin is not None else "",
            "value_model": "spot" if category=="spot" else "crypto_derivative", "quantity_unit": "base_asset",
            "fee": fnum(x.get("execFee")), "fee_currency": str(x.get("feeCurrency") or x.get("feeCoin") or ""),
            "exec_time": exec_time, "category": category, "is_maker": x.get("isMaker"), "raw": x,
        }
        if snap.get("position_margin_snapshot") is not None:
            event["position_margin_snapshot"]=snap.get("position_margin_snapshot")
        if snap.get("initial_margin_snapshot") is not None:
            event["initial_margin_snapshot"]=snap.get("initial_margin_snapshot")
        if snap.get("maintenance_margin_snapshot") is not None:
            event["maintenance_margin_snapshot"]=snap.get("maintenance_margin_snapshot")
        if snap.get("position_value_snapshot") is not None:
            event["position_value_snapshot"]=snap.get("position_value_snapshot")
        if snap.get("margin_mode"):
            event["margin_mode"]=snap.get("margin_mode")
        if snap.get("margin_snapshot_source"):
            event["margin_snapshot_source"]=snap.get("margin_snapshot_source")
        return event

    async def _auth_and_subscribe(self, ws):
        self._pending_ws_messages = []
        expires = self._now_ms() + 10_000
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            f"GET/realtime{expires}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        await ws.send(json.dumps({"op": "auth", "args": [self.api_key, expires, signature]}))
        deadline = time.monotonic() + 12
        authed = False
        subscribed = False
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.5, deadline-time.monotonic()))
            msg = json.loads(raw)
            if msg.get("topic") == "execution":
                # Some fills can precede the subscribe acknowledgement.
                self._pending_ws_messages.append(raw)
                continue
            if msg.get("op") == "auth":
                if not msg.get("success") and msg.get("retCode") not in (0, None):
                    raise RuntimeError(f"Bybit WebSocket 认证失败: {msg.get('ret_msg') or msg.get('retMsg') or msg}")
                if msg.get("success") is False:
                    raise RuntimeError(f"Bybit WebSocket 认证失败: {msg.get('ret_msg') or msg}")
                authed = True
                await ws.send(json.dumps({"op": "subscribe", "args": ["execution"]}))
                continue
            if msg.get("op") == "subscribe":
                if msg.get("success") is False:
                    raise RuntimeError(f"Bybit execution 订阅失败: {msg.get('ret_msg') or msg}")
                subscribed = True
                break
        if not authed:
            raise RuntimeError("Bybit WebSocket 认证超时")
        if not subscribed:
            raise RuntimeError("Bybit execution 订阅超时")

    async def _execution_messages(self, ws):
        pending, self._pending_ws_messages = self._pending_ws_messages, []
        for raw in pending:
            yield raw
        async for raw in ws:
            yield raw

    async def _recover_executions(self):
        end = self._now_ms()
        if self._recovery_from_ms is None:
            # No durable bridge checkpoint is available in this repository.
            # Startup covers seven days; a reconnect retains the in-memory point.
            self._recovery_from_ms = end - 7 * 86400 * 1000
        start = max(0, self._recovery_from_ms - 5000)
        total = 0
        while start < end:
            window_end = min(end, start + 7 * 86400 * 1000)
            for category in self.categories:
                total += await self._history_category(category, start, window_end)
            # Advance only after every category in this window was accepted.
            self._recovery_from_ms = window_end
            start = window_end
        return total

    async def _risk_watch_loop(self):
        """Capture newly observed protective stops without backfilling current moved stops."""
        while True:
            try:
                active={}
                if self.enable_perpetual:
                    data=await asyncio.to_thread(self._rest_get,"/v5/position/list",{"category":"linear","settleCoin":"USDT","limit":200})
                    if data.get("retCode")!=0:
                        raise RuntimeError(data.get("retMsg") or "position/list failed")
                    now=int(time.time()*1000)
                    for row in ((data.get("result") or {}).get("list") or []):
                        size=abs(self._fnum(row.get("size"),0.0)); stop=self._fnum(row.get("stopLoss"),0.0)
                        sym=str(row.get("symbol") or "").upper(); side=str(row.get("side") or "").lower()
                        if size<=0 or not sym or side not in {"buy","sell"}: continue
                        key=(sym,side)
                        if stop>0: active[key]=stop
                        if not self._risk_baselined or stop<=0 or self._risk_seen.get(key)==stop: continue
                        await self.emit({"type":"risk_update","schema_version":1,"risk_type":"protective_stop","provider":self.id,"provider_type":self.TYPE,"provider_name":self.name,"account_id":self.account_id,"symbol":sym,"position_side":"long" if side=="buy" else "short","protective_stop_price":stop,"detected_at":now,"source":"bybit_position_stop_loss","raw":{"positionIdx":row.get("positionIdx"),"stopLoss":row.get("stopLoss")}})
                self._risk_seen=active
                self._risk_baselined=True
                self._risk_failures=0
                if self._risk_unavailable:
                    self._risk_unavailable=False
                    await self.status("connected","Bybit 初始保护性止损监听已恢复")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._risk_failures += 1
                now=time.time()
                if self._risk_failures>=3 and now-self._risk_warn_at>=60:
                    self._risk_warn_at=now
                    self._risk_unavailable=True
                    await self.status("warning",f"Bybit 初始保护性止损监听暂时不可用（连续 {self._risk_failures} 次）：{e}")
            await asyncio.sleep(4.0)

    async def run(self):
        if not self.enabled:
            return
        backoff = 2.0
        while True:
            try:
                self._check_credentials()
                try:
                    await asyncio.to_thread(self._refresh_time_offset)
                except Exception as e:
                    await self.status("warning", f"服务器时间校准失败，将使用本机时间：{e}")
                await self.status("connecting", f"连接 {self.ws_url}")
                async with connect(self.ws_url, ping_interval=None, close_timeout=5, open_timeout=15) as ws:
                    await self._auth_and_subscribe(ws)
                    self.running = True
                    self._connected_once = True
                    backoff = 2.0
                    await self.status("connected", f"Bybit 只读 execution 已连接 · {'Demo 模拟' if self.demo else ('Testnet' if self.testnet else '实盘')}")
                    heartbeat = asyncio.create_task(self._heartbeat(ws))
                    risk_watch = asyncio.create_task(self._risk_watch_loop()) if self.enable_perpetual else None
                    try:
                        recovered = await self._recover_executions()
                        await self.status("connected", f"Bybit 成交补拉完成 · {recovered} 条（桥接层按成交 ID 去重）")
                        async for raw in self._execution_messages(ws):
                            msg = json.loads(raw)
                            if msg.get("topic") != "execution":
                                continue
                            rows=[]
                            for x in msg.get("data") or []:
                                exec_type = str(x.get("execType") or "")
                                if exec_type and exec_type != "Trade":
                                    continue
                                cat=str(x.get("category") or "").lower()
                                if cat and cat not in self.categories:
                                    continue
                                rows.append(x)
                            # One post-message position read per derivative symbol. Attach leverage
                            # to all fills in the message, but attach the position IM/value snapshot
                            # only to the last fill for that symbol so a multi-fill order does not
                            # apply the final position margin too early.
                            grouped={}
                            for idx,x in enumerate(rows):
                                cat=str(x.get("category") or "").lower(); sym=str(x.get("symbol") or "").upper()
                                if cat in {"linear","inverse"} and sym:
                                    grouped.setdefault((cat,sym),[]).append(idx)
                            snapshots={}
                            for key,indexes in grouped.items():
                                last=rows[indexes[-1]]
                                try:
                                    snapshots[key]=await asyncio.to_thread(self._position_snapshot,key[0],key[1],last.get("positionIdx"),last.get("side"))
                                except Exception as e:
                                    snapshots[key]={}
                                    await self.status("warning", f"Bybit 成交已收到，但 {key[1]} 杠杆/保证金快照读取失败：{e}")
                            for idx,x in enumerate(rows):
                                key=(str(x.get("category") or "").lower(),str(x.get("symbol") or "").upper())
                                snap=dict(snapshots.get(key) or {})
                                if key in grouped and idx != grouped[key][-1]:
                                    snap.pop("position_margin_snapshot",None); snap.pop("position_value_snapshot",None)
                                event = self._normalize(x,snap)
                                if event["execution_id"] and event["price"] > 0 and event["qty"] > 0:
                                    await self.emit(event)
                            if rows:
                                # Use exchange execution time, never receipt time:
                                # a delayed message must not move the gap past lost fills.
                                stamp = min(int(x.get("execTime") or 0) for x in rows)
                                self._recovery_from_ms = max(self._recovery_from_ms or 0, stamp)
                    finally:
                        heartbeat.cancel()
                        if risk_watch: risk_watch.cancel()
                        self.running = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.running = False
                await self.status("error", str(e))
                wait = min(backoff, 30.0) + random.uniform(0, 1.0)
                await asyncio.sleep(wait)
                backoff = min(backoff * 1.8, 30.0)

    def _rest_get(self, path, params):
        self._check_credentials()
        query = urlencode(params)
        last_data = None
        for attempt in range(2):
            # Keep local clock aligned with Bybit before each authenticated request.
            try:
                self._refresh_time_offset()
            except Exception:
                pass
            ts = str(self._now_ms())
            plain = ts + self.api_key + self.recv_window + query
            sign = hmac.new(self.api_secret.encode(), plain.encode(), hashlib.sha256).hexdigest()
            req = Request(self.rest_base + path + ("?" + query if query else ""), headers={
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": self.recv_window,
                "X-BAPI-SIGN": sign,
                "Content-Type": "application/json",
                "User-Agent": "TradingReviewBridge/1.4-nas",
            })
            try:
                with urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode("utf-8"))
                last_data = data
                if attempt == 0 and self._is_timestamp_error(data):
                    # Bybit returns retCode 10002 for timestamp / recv_window drift.
                    # Apply the server timestamp from that very response (when present),
                    # then retry once with a newly signed request.
                    if not self._apply_server_time_from_error(data):
                        try: self._refresh_time_offset()
                        except Exception: pass
                    continue
                return data
            except HTTPError as e:
                body = ""
                try: body = e.read().decode("utf-8", "ignore")
                except Exception: pass
                if e.code == 403:
                    raise RuntimeError("Bybit API 返回 403：当前网络/IP 可能被限制，或区域 API 域名不正确") from e
                raise RuntimeError(f"Bybit HTTP {e.code}: {body or e.reason}") from e
            except URLError as e:
                raise RuntimeError(f"Bybit 网络连接失败: {e.reason}") from e
        return last_data or {}

    async def _history_category(self, category, start_ms, end_ms):
        cursor = None
        cursors = set()
        all_rows = {}
        sent = 0
        while True:
            params = {
                "category": category,
                "startTime": int(start_ms),
                "endTime": int(end_ms),
                "execType": "Trade",
                "limit": 100,
            }
            if cursor:
                params["cursor"] = cursor
            data = await asyncio.to_thread(self._rest_get, "/v5/execution/list", params)
            if not isinstance(data, dict):
                raise RuntimeError("Bybit 成交接口返回格式异常，未确认补拉完成")
            if data.get("retCode") != 0:
                raise RuntimeError(f"{category}: {data.get('retMsg')}")
            result = data.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("list"), list):
                raise RuntimeError("Bybit 成交结果缺少 list，未确认补拉完成")
            rows = result["list"]
            for x in rows:
                execution_id = str(x.get("execId") or "")
                if not execution_id:
                    raise RuntimeError("Bybit 历史成交缺少 execId，未确认补拉完成")
                key = (str(x.get("symbol") or ""), execution_id)
                if key in all_rows and all_rows[key] != x:
                    raise RuntimeError("Bybit 重复成交 ID 内容冲突，未确认补拉完成")
                # REST payloads may omit category; the request establishes it.
                all_rows[key] = dict(x)
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
            if cursor in cursors:
                raise RuntimeError("Bybit 成交分页游标重复，未确认补拉完成")
            cursors.add(cursor)
            await asyncio.sleep(0.05)
        # Sort across ALL pages. Sorting only each descending page emits later
        # closes before the earlier opening fills on subsequent pages.
        for x in sorted(all_rows.values(), key=lambda a: (
                int(a.get("execTime") or 0), str(a.get("execId") or ""),
                str(a.get("orderId") or ""), str(a.get("leavesQty") or ""))):
            x.setdefault("category", category)
            if int(x.get("execTime") or 0) <= 0:
                raise RuntimeError("Bybit 历史成交时间无效，未确认补拉完成")
            event = self._normalize(x)
            if not (event["execution_id"] and event["price"] > 0 and event["qty"] > 0):
                raise RuntimeError("Bybit 历史成交价格/数量无效，未确认补拉完成")
            await self.emit(event)
            sent += 1
        return sent

    async def handle_command(self, command):
        action = command.get("action")
        if action == "network_diagnostic":
            self._check_credentials()
            details=await self.network_probe([("REST DNS/TLS", self.rest_base),("WebSocket DNS/TLS", self.ws_url)])
            try:
                offset=await asyncio.to_thread(self._refresh_time_offset)
                details.append({"stage":"HTTPS 公共 API","ok":True,"message":f"服务器时间可达 · 偏差 {offset}ms"})
            except Exception as e:
                details.append({"stage":"HTTPS 公共 API","ok":False,"message":str(e)})
            auth_ok=False
            auth_msg=""
            try:
                for cat in self.categories:
                    data=await asyncio.to_thread(self._rest_get,"/v5/execution/list",{"category":cat,"limit":1})
                    if data.get("retCode")==0:
                        auth_ok=True; auth_msg=f"{cat} 只读认证成功"; break
                    auth_msg=str(data.get("retMsg") or data)
                details.append({"stage":"API 认证","ok":auth_ok,"message":auth_msg or "认证失败"})
            except Exception as e:
                details.append({"stage":"API 认证","ok":False,"message":str(e)})
            try:
                async with connect(self.ws_url,ping_interval=None,close_timeout=3,open_timeout=8) as ws:
                    await self._auth_and_subscribe(ws)
                details.append({"stage":"WebSocket 认证/订阅","ok":True,"message":"execution 订阅成功"})
            except Exception as e:
                details.append({"stage":"WebSocket 认证/订阅","ok":False,"message":str(e)})
            ok=all(x.get("ok") for x in details)
            msg=" · ".join(f"{x['stage']} {'✓' if x['ok'] else '✗'}" for x in details)
            return {"ok":ok,"kind":"network_diagnostic","message":"Bybit 网络诊断："+msg,"details":details}
        if action == "connection_test":
            self._check_credentials()
            offset = await asyncio.to_thread(self._refresh_time_offset)
            # Use execution-list because it verifies authenticated read access without placing orders.
            last_error = None
            for cat in (self.categories or ["linear"]):
                try:
                    data = await asyncio.to_thread(self._rest_get, "/v5/execution/list", {"category": cat, "limit": 1})
                    if data.get("retCode") == 0:
                        return {"ok": True, "message": f"Bybit 只读 API 认证成功 · {'Demo 模拟' if self.demo else ('Testnet' if self.testnet else '实盘')} · {cat} · 时钟偏差 {offset}ms"}
                    last_error = data.get("retMsg")
                except Exception as e:
                    last_error = str(e)
            return {"ok": False, "message": f"Bybit 连接测试失败: {last_error or 'unknown error'}"}
        if action == "history_sync":
            days = max(1, min(int(command.get("days") or 1), 7))
            end_ms = self._now_ms()
            start_ms = end_ms - days * 86400 * 1000
            total = 0
            ok_cats = []
            errors = []
            for category in (self.categories or ["linear", "spot"]):
                try:
                    count = await self._history_category(category, start_ms, end_ms)
                    total += count
                    ok_cats.append(f"{category}:{count}")
                except Exception as e:
                    errors.append(f"{category}:{e}")
            if not ok_cats:
                return {"ok": False, "message": "Bybit 历史成交同步失败 · " + "；".join(errors)}
            msg = f"Bybit 最近 {days} 天历史成交已推送 {total} 条（重复项由桥接层去重） · " + ", ".join(ok_cats)
            if errors:
                msg += " · 部分分类跳过: " + "；".join(errors)
            return {"ok": True, "message": msg, "count": total}
        return await super().handle_command(command)
