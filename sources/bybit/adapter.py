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
        # These statuses represent the *running* private WebSocket, not a REST probe.
        self._ws_channels = {"execution": "未连接", "position": "未连接", "order": "未连接"}
        self._ws_channel_counts = {"execution": 0, "position": 0, "order": 0}
        self._optional_ack_queue = []
        # WS position updates are authoritative snapshots, not execution payloads.
        # Keep only a short-lived, in-memory cache per account/session; never use a
        # snapshot from an earlier trade or historical-sync to fill old entries.
        self._ws_positions = {}
        self._ws_position_sequences = {}
        self._position_changed = asyncio.Event()
        self._rest_margin_blocked_until = 0.0
        self._rest_margin_warning_at = 0.0
        self._live_margin_wait_seconds = 2.5

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


    async def _heartbeat(self, ws):
        while True:
            await asyncio.sleep(20)
            for channel in tuple(self._optional_ack_queue):
                if self._ws_channels.get(channel)=="等待确认":
                    self._ws_channels[channel]="订阅未确认"
                    await self._diagnostic(f"Bybit WebSocket {channel} 订阅在 20 秒内未收到确认；请查看网络诊断", "warning")
            self._optional_ack_queue.clear()
            await ws.send(json.dumps({"op": "ping"}))

    @staticmethod
    def _fnum(v, default=0.0):
        try: return float(v)
        except Exception: return default

    @staticmethod
    def _maybe_number(v):
        if v is None or isinstance(v, bool) or str(v).strip() == "":
            return None
        try:
            n=float(v)
            return n if n == n and abs(n) != float("inf") else None
        except (ValueError, TypeError):
            return None

    def _cache_ws_position(self, msg):
        """Capture Bybit V5 private position snapshots, with temporal identity.

        A snapshot received before/after an execution may be reused only when its
        exchange update timestamp overlaps that actual fill, not merely because
        the symbol still happens to be open. Portfolio-margin blank fields stay
        unknown instead of turning into an artificial 0-dollar IM value.
        """
        now_ms=int(time.time()*1000)
        changed=False
        for row in msg.get("data") or []:
            if not isinstance(row,dict) or str(row.get("category") or "linear").lower()!="linear":
                continue
            sym=str(row.get("symbol") or "").upper().strip()
            idx=self._safe_idx(row.get("positionIdx"))
            side=str(row.get("side") or "").lower().strip()
            if not sym or idx is None or side not in {"buy","sell",""}:
                continue
            size=self._maybe_number(row.get("size"))
            lev=self._maybe_number(row.get("leverage"))
            im=self._maybe_number(row.get("positionIM"))
            mm=self._maybe_number(row.get("positionMM"))
            val=self._maybe_number(row.get("positionValue"))
            flat=size is not None and abs(size)<1e-12
            # Non-flat portfolio positions frequently send blank / 0 margin fields.
            # When the exchange does not supply a usable IM, do not claim 0.
            margin=abs(im) if im is not None and im>0 else (0.0 if flat else None)
            maintenance=abs(mm) if mm is not None and mm>0 else (0.0 if flat else None)
            position_value=abs(val) if val is not None and val>0 else (0.0 if flat else None)
            snapshot={
                "leverage":lev if lev is not None and lev>0 else None,
                "position_margin_snapshot":margin,
                "initial_margin_snapshot":margin,
                "maintenance_margin_snapshot":maintenance,
                "position_value_snapshot":position_value,
                "position_idx":idx,
                "margin_mode":"portfolio" if not (lev and lev>0) and margin is None else "",
                "margin_snapshot_source":"bybit_ws_position",
            }
            # Prefer actual position.updatedTime; private message creationTime
            # describes the delivery and need not mean the position just changed.
            updated=self._maybe_number(row.get("updatedTime"))
            creation=self._maybe_number(msg.get("creationTime"))
            stamp=int(updated or creation or now_ms)
            if stamp<1_000_000_000_000:stamp=int(stamp*1000)
            stamp=min(stamp,now_ms+5000)
            key=("linear",sym,idx)
            record={"snapshot":snapshot,"exchange_ms":stamp,"received_ms":now_ms,"side":side,"size":size}
            sequence=str(row.get("seq") if row.get("seq") is not None else "")
            if sequence and sequence != "-1":
                sequence_key=("linear",sym,idx,sequence)
                # Keep the first observed snapshot for a sequence: later leverage-only
                # updates can reuse the last execution seq without another fill.
                self._ws_position_sequences.setdefault(sequence_key,record)
                while len(self._ws_position_sequences)>512:
                    self._ws_position_sequences.pop(next(iter(self._ws_position_sequences)))
                changed=True
            prev=self._ws_positions.get(key)
            if prev and stamp < prev["exchange_ms"]:
                continue
            self._ws_positions[key]={"snapshot":snapshot,"exchange_ms":stamp,
                                     "received_ms":now_ms,"side":side,"size":size}
            changed=True
        if changed:
            self._position_changed.set()

    def _ws_snapshot_for_fill(self, category, symbol, position_idx, exec_time, sequence=None):
        """Only match the same symbol/position mode and the *same* recent change."""
        if str(category or "").lower()!="linear":return {}
        idx=self._safe_idx(position_idx)
        sym=str(symbol or "").upper()
        seq=str(sequence) if sequence is not None else ""
        if seq and seq != "-1":
            candidates=[value for key,value in self._ws_position_sequences.items()
                        if key[0]=="linear" and key[1]==sym and key[3]==seq and (idx is None or key[2]==idx)]
            if len(candidates)!=1:return {}  # ambiguous hedge legs must never be guessed
            entry=candidates[0]
        else:
            if idx is None:return {}
            entry=self._ws_positions.get(("linear",sym,idx))
        if not entry:return {}
        now=int(time.time()*1000)
        stamp=entry["exchange_ms"]
        try: exec_ms=int(exec_time)
        except (ValueError,TypeError):return {}
        if exec_ms<1_000_000_000_000:exec_ms*=1000
        # ±2 sec accounts for server message ordering, and the strict upper bound
        # prevents attaching a much later current position to an earlier fill.
        if not (exec_ms-2000 <= stamp <= exec_ms+8000):return {}
        if now-entry["received_ms"]>15_000:return {}
        snap=entry["snapshot"]
        if any(snap.get(k) is not None for k in ("leverage","position_margin_snapshot","position_value_snapshot")):
            result=dict(snap)
            result["margin_snapshot_time"]=stamp
            return result
        return {}

    async def _live_fill_worker(self, queue):
        """Preserve execution order while giving position WS up to 2.5s to arrive."""
        while True:
            rows=await queue.get()
            try:
                await self._deliver_live_batch(rows)
            finally:
                queue.task_done()

    async def _deliver_live_batch(self, rows):
        groups={}
        for index,x in enumerate(rows):
            cat=str(x.get("category") or "").lower()
            sym=str(x.get("symbol") or "").upper()
            idx=self._safe_idx(x.get("positionIdx"))
            if cat in {"linear","inverse"} and sym:
                groups.setdefault((cat,sym,idx,str(x.get("seq") if x.get("seq") is not None else "")),[]).append(index)
        # Collect snapshots based on the *last* execution in each exchange message.
        # Order matters: a WS position notification can precede or follow execution.
        snapshots={}
        deadline=time.monotonic()+self._live_margin_wait_seconds
        while groups:
            for key,indexes in groups.items():
                last=rows[indexes[-1]]
                snap=self._ws_snapshot_for_fill(key[0],key[1],key[2],last.get("execTime"),last.get("seq"))
                if snap:snapshots[key]=snap
            if len(snapshots)==len(groups) or time.monotonic()>=deadline:
                break
            self._position_changed.clear()
            # Cache state is rechecked on each notification and again at timeout.
            try:await asyncio.wait_for(self._position_changed.wait(),timeout=max(0.01,deadline-time.monotonic()))
            except asyncio.TimeoutError:break
        for key,indexes in groups.items():
            if key in snapshots:continue
            # Optional REST fallback: only when WS supplied no usable snapshot.
            # A 403 suppresses all repeated probes for an hour but never loses the fill.
            if time.monotonic()<self._rest_margin_blocked_until:continue
            last=rows[indexes[-1]]
            try:
                snap=await asyncio.to_thread(self._position_snapshot,key[0],key[1],key[2],last.get("side"))
                # REST response is current, so it is trustworthy only near the live fill.
                if snap:snapshots[key]=snap
            except Exception as e:
                text=str(e)
                self._rest_margin_blocked_until=time.monotonic()+(3600 if "403" in text else 60)
                if time.monotonic()-self._rest_margin_warning_at>300:
                    self._rest_margin_warning_at=time.monotonic()
                    await self.status("warning",f"Bybit {key[1]} 保证金快照不可用；成交仍会同步，WS 缺失且 REST 查询失败：{text}")
        for key,indexes in groups.items():
            ws_snap=self._ws_snapshot_for_fill(key[0],key[1],key[2],rows[indexes[-1]].get("execTime"),rows[indexes[-1]].get("seq"))
            if ws_snap:snapshots[key]=ws_snap  # WS takes precedence over late REST.
        for index,x in enumerate(rows):
            key=(str(x.get("category") or "").lower(),str(x.get("symbol") or "").upper(),self._safe_idx(x.get("positionIdx")),str(x.get("seq") if x.get("seq") is not None else ""))
            snap=dict(snapshots.get(key) or {})
            if key in groups and index != groups[key][-1]:
                # Each execution remains unique, but a *position-level* snapshot
                # must never be counted multiple times in a multi-fill order.
                for field in ("position_margin_snapshot","initial_margin_snapshot",
                              "maintenance_margin_snapshot","position_value_snapshot",
                              "margin_snapshot_source","margin_snapshot_time"):
                    snap.pop(field,None)
            event=self._normalize(x,snap)
            if event["execution_id"] and event["price"]>0 and event["qty"]>0:
                await self.emit(event)

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
        candidates=[r for r in rows if str(r.get("symbol") or symbol).upper()==symbol]
        if wanted_idx is not None:
            candidates=[r for r in candidates if self._safe_idx(r.get("positionIdx"))==wanted_idx]
        else:
            # Without a linked seq/idx, only an explicit one-way row is unambiguous.
            candidates=[r for r in candidates if self._safe_idx(r.get("positionIdx"))==0]
        if len(candidates)!=1:return {}
        row=candidates[0]
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
            "exec_time": exec_time, "category": category, "position_idx": x.get("positionIdx") if x.get("positionIdx") is not None else snap.get("position_idx"), "is_maker": x.get("isMaker"), "raw": x,
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
        if snap.get("margin_snapshot_time") is not None:
            event["margin_snapshot_time"]=snap["margin_snapshot_time"]
        return event

    async def _auth_and_subscribe(self, ws):
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

    async def _diagnostic(self, text, level="info"):
        # Do not echo raw Bybit orders / credentials in bridge logs.
        await self.emit({"type": "adapter_diagnostic", "provider": self.id,
                         "provider_type": self.TYPE, "level": level, "message": text})

    async def _send_risk_event(self, symbol, side, stop, position_idx, source, raw,
                               event_time=None, candidate=False, order_id=""):
        """Read-only stop observation; unprotected conditional orders need human review."""
        symbol=str(symbol or "").upper(); side=str(side or "").lower()
        if not symbol or side not in {"buy","sell"} or stop<=0:
            return
        try: position_idx=int(position_idx)
        except (ValueError,TypeError): position_idx=None
        key=(symbol,side,position_idx,source,str(order_id or ""))
        if self._risk_seen.get(key)==stop:
            return
        detected=int(time.time()*1000)
        payload={"type":"risk_update","schema_version":1,
                         "risk_type":"stop_candidate" if candidate else "protective_stop",
                         "provider":self.id,"provider_type":self.TYPE,"provider_name":self.name,
                         "account_id":self.account_id,"symbol":symbol,
                         "position_idx":position_idx,"position_side":"long" if side=="buy" else "short",
                         "protective_stop_price":stop,"detected_at":detected,
                         "source":source,
                         # Candidate payload deliberately excludes full order/account details.
                         "raw": ({"orderId":str(order_id or ""),"stopOrderType":str(raw.get("stopOrderType") or ""),
                                  "triggerDirection":raw.get("triggerDirection"),"reduceOnly":raw.get("reduceOnly"),
                                  "closeOnTrigger":raw.get("closeOnTrigger")} if candidate else raw)}
        if order_id:
            # Stable across WebSocket reconnections; different stop prices are distinct observations.
            payload["risk_id"]="RISK-"+hashlib.sha256((self.id+"|"+self.account_id+"|"+str(order_id)+"|"+str(stop)+"|"+payload["risk_type"]).encode()).hexdigest()[:24]
        await self.emit(payload)
        self._risk_seen[key]=stop  # Only cache after successful emission.
        if candidate:
            await self._diagnostic(f"Bybit {symbol} 条件单疑似止损，已进入人工确认（未自动填写初始止损价）")

    async def _handle_position_stop(self, msg):
        for row in msg.get("data") or []:
            if str(row.get("category") or "linear").lower()!="linear": continue
            symbol=str(row.get("symbol") or "").upper()
            side=str(row.get("side") or "").lower()
            idx=row.get("positionIdx")
            key=(symbol,side,self._safe_idx(idx))
            if self._fnum(row.get("size"))<=0:
                # A flat position cannot supply an initial stop for a future position.
                self._risk_seen.pop(key,None)
                continue
            stop=self._fnum(row.get("stopLoss"))
            if stop>0:
                await self._send_risk_event(symbol,side,stop,idx,"bybit_ws_position_stop_loss",row)
            else:
                self._risk_seen.pop(key,None)

    @staticmethod
    def _safe_idx(idx):
        try: return int(idx)
        except (ValueError,TypeError): return None

    @staticmethod
    def _true_flag(value):
        return value is True or str(value).lower() in {"true", "1"}

    async def _handle_order_stop(self,msg):
        for row in msg.get("data") or []:
            if str(row.get("category") or "linear").lower()!="linear": continue
            status=str(row.get("orderStatus") or "").lower()
            if status not in {"new","untriggered","partiallyfilled"}: continue
            kind=str(row.get("stopOrderType") or "").lower()
            if "trailing" in kind or "takeprofit" in kind: continue
            idx=row.get("positionIdx")
            order_side=str(row.get("side") or "").lower()
            if order_side not in {"buy","sell"}:continue
            # Explicit attached stopLoss belongs to the order's *own* direction.
            embedded=self._fnum(row.get("stopLoss"))
            if embedded>0:
                # This is the stop attached to a *pending order*. It is not evidence
                # of protection for the current open position, so require review.
                await self._send_risk_event(row.get("symbol"),order_side,embedded,idx,
                                            "bybit_ws_order_attached_stop_candidate",row,
                                            candidate=True,order_id=str(row.get("orderId") or ""))
            price=self._fnum(row.get("triggerPrice"))
            if price<=0 or kind not in {"stoploss","partialstoploss","stop",""}: continue
            protected_side="buy" if order_side=="sell" else "sell"
            direction=self._safe_idx(row.get("triggerDirection"))
            # A protective long stop should trigger on falling price; vice versa for shorts.
            if direction in (1,2) and direction != (2 if protected_side=="buy" else 1):continue
            safe_close=self._true_flag(row.get("reduceOnly")) or self._true_flag(row.get("closeOnTrigger"))
            # Without close-only protection, even an apparent stop can open an opposite
            # position. Never silently treat that as a confirmed initial stop.
            await self._send_risk_event(row.get("symbol"),protected_side,price,idx,
                                        "bybit_ws_conditional_stop_loss" if safe_close else "bybit_ws_conditional_stop_candidate",
                                        row,candidate=not safe_close,
                                        order_id=str(row.get("orderId") or ""))

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
                    if "403" in str(e):
                        # Both public and signed REST share the blocked region/API host.
                        # Retain WS processing; do not stall the first live execution
                        # on a known-denied position/list request.
                        self._rest_margin_blocked_until=time.monotonic()+3600
                    await self.status("warning", f"服务器时间校准失败，将使用本机时间：{e}")
                await self.status("connecting", f"连接 {self.ws_url}")
                async with connect(self.ws_url, ping_interval=None, close_timeout=5, open_timeout=15) as ws:
                    await self._auth_and_subscribe(ws)
                    self.running = True
                    self._connected_once = True
                    backoff = 2.0
                    await self.status("connected", f"Bybit 只读 execution 已连接 · {'Demo 模拟' if self.demo else ('Testnet' if self.testnet else '实盘')}")
                    self._risk_seen.clear()  # WebSocket never backfills old stop values.
                    self._ws_channels={"execution":"已订阅", "position":"未启用", "order":"未启用"}
                    self._ws_channel_counts={"execution":0,"position":0,"order":0}
                    self._optional_ack_queue=[]
                    self._ws_positions.clear()
                    self._position_changed.clear()
                    live_queue=asyncio.Queue()
                    fill_worker=asyncio.create_task(self._live_fill_worker(live_queue),name="bybit-live-margin-worker")
                    heartbeat = asyncio.create_task(self._heartbeat(ws))
                    if self.enable_perpetual:
                        # Optional channels are separate: failure must not interrupt executions.
                        for channel in ("position","order"):
                            self._ws_channels[channel]="等待确认"
                            self._optional_ack_queue.append(channel)
                            await ws.send(json.dumps({"op":"subscribe","args":[channel],
                                                      "req_id":"stop-watch-"+channel}))
                        await self._diagnostic("Bybit WebSocket 止损监听：position/order 订阅请求已发出，等待各频道确认")
                    try:
                        async for raw in ws:
                            msg = json.loads(raw)
                            await self.health_event("stream")
                            if msg.get("op")=="subscribe":
                                req=str(msg.get("req_id") or msg.get("reqId") or "")
                                channel=req.removeprefix("stop-watch-") if req.startswith("stop-watch-") else ""
                                if channel not in {"position","order"}:
                                    args=msg.get("args") or []
                                    if isinstance(args,list) and len(args)==1 and args[0] in {"position","order"}:
                                        channel=args[0]
                                if not channel and self._optional_ack_queue:
                                    channel=self._optional_ack_queue[0]
                                if channel in {"position","order"}:
                                    if channel in self._optional_ack_queue:self._optional_ack_queue.remove(channel)
                                    ok=(msg.get("success") is True or msg.get("retCode")==0) and msg.get("success") is not False
                                    self._ws_channels[channel]="已订阅" if ok else "订阅失败"
                                    reason=str(msg.get("ret_msg") or msg.get("retMsg") or "")[:100]
                                    await self._diagnostic(f"Bybit WebSocket {channel} 频道："+("已确认订阅成功" if ok else "订阅失败 "+reason),
                                                           "info" if ok else "warning")
                                continue
                            topic=str(msg.get("topic") or "")
                            if topic.startswith("position") and self.enable_perpetual:
                                self._ws_channel_counts["position"]+=len(msg.get("data") or [])
                                self._cache_ws_position(msg)
                                await self._handle_position_stop(msg)
                                continue
                            if topic.startswith("order") and self.enable_perpetual:
                                self._ws_channel_counts["order"]+=len(msg.get("data") or [])
                                await self._handle_order_stop(msg)
                                continue
                            if topic!="execution":continue
                            self._ws_channel_counts["execution"]+=len(msg.get("data") or [])
                            rows=[]
                            for x in msg.get("data") or []:
                                exec_type = str(x.get("execType") or "")
                                if exec_type and exec_type != "Trade":
                                    continue
                                cat=str(x.get("category") or "").lower()
                                if cat and cat not in self.categories:
                                    continue
                                rows.append(x)
                            # WS receives continue while a separate ordered worker waits for a
                            # near-synchronous position update. No REST request blocks WS reads.
                            if rows:
                                await live_queue.put(rows)
                    finally:
                        heartbeat.cancel()
                        # Never discard a filled execution merely because the socket closed.
                        # Finish already-received batches, even if margin is unknown.
                        try:
                            await asyncio.wait_for(live_queue.join(),timeout=45)
                        except asyncio.TimeoutError:
                            await self._diagnostic("Bybit 断线前的成交尚未全部写入队列，请检查同步日志", "warning")
                        fill_worker.cancel()
                        try:await fill_worker
                        except asyncio.CancelledError:pass
                        self.running = False
                        for channel in self._ws_channels:self._ws_channels[channel]="已断开"
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
                if len(all_rows) > 200000:
                    raise RuntimeError("历史成交超过单批安全上限，请缩短补同步天数后重试")
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
            # This is the active streaming connection state, not an inferred REST test.
            if self.enable_perpetual:
                for channel in ("position","order"):
                    state=self._ws_channels.get(channel,"未知")
                    details.append({"stage":f"WebSocket {channel} 实时监听",
                                    "ok":state=="已订阅",
                                    "message":f"{state} · 本次连接收到 {self._ws_channel_counts.get(channel,0)} 条消息记录（不会补发历史订单）"})
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

