from __future__ import annotations
import asyncio, hashlib, hmac, json, random, time, socket, ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from websockets.asyncio.client import connect
from adapters.base import AdapterBase

class Adapter(AdapterBase):
    TYPE = "gate"
    def __init__(self, config, emit):
        super().__init__(config, emit)
        self.key = str(config.get("api_key") or "").strip(); self.secret = str(config.get("api_secret") or "").strip()
        self.environment = str(config.get("environment") or "live").lower().strip()
        if self.environment not in {"live","testnet"}: self.environment="live"
        self.enable_spot = bool(config.get("enable_spot", True)); self.enable_futures = bool(config.get("enable_futures", True))
        if self.environment=="testnet":
            self.rest = "https://api-testnet.gateapi.io/api/v4"
            self.spot_ws = "wss://ws-testnet.gate.com/v4/ws/spot"
            self.futures_ws = "wss://ws-testnet.gate.com/v4/ws/futures/usdt"
        else:
            self.rest = "https://api.gateio.ws/api/v4"
            self.spot_ws = "wss://api.gateio.ws/ws/v4/"
            self.futures_ws = "wss://fx-ws.gateio.ws/v4/ws/usdt"
        self.user_id = ""
        self.rest_fallback_enabled = bool(config.get("rest_fallback_enabled", True))
        self.rest_poll_interval = max(2.0, min(float(config.get("rest_poll_interval") or 3.0), 30.0))
        self.ws_retry_interval = max(5.0, min(float(config.get("ws_retry_interval") or 15.0), 120.0))
        self._spot_poll_from = 0
        self._fut_poll_from = 0
        self._contract_multiplier_cache = {}; self._contract_spec_cache = {}; self._position_snapshot_cache = {}; self._order_trade_cache = {}; self._risk_seen = {}; self._risk_baselined = False; self._risk_warn_at = 0.0
        self._risk_failures = 0; self._risk_unavailable = False

    def _check(self):
        if not self.key or not self.secret or self.key.startswith("PUT_") or self.secret.startswith("PUT_"):
            raise RuntimeError("Gate API Key / Secret 尚未配置")

    def _sign_headers(self, method, path, query="", body=""):
        ts = str(int(time.time()))
        body_hash = hashlib.sha512((body or "").encode()).hexdigest()
        prefix_path = "/api/v4" + path
        sign_str = f"{method.upper()}\n{prefix_path}\n{query}\n{body_hash}\n{ts}"
        sign = hmac.new(self.secret.encode(), sign_str.encode(), hashlib.sha512).hexdigest()
        return {"KEY":self.key,"Timestamp":ts,"SIGN":sign,"Accept":"application/json","Content-Type":"application/json","User-Agent":"TradingReviewBridge/2.2","X-Gate-Size-Decimal":"1"}

    def _get(self, path, params=None):
        self._check(); query = urlencode(params or {})
        url = self.rest + path + (("?"+query) if query else "")
        last_error = None
        # VPS / TLS routes can occasionally hit a one-off handshake timeout. Retry the
        # signed GET with a fresh timestamp before declaring the Adapter unavailable.
        for attempt in range(3):
            req = Request(url, headers=self._sign_headers("GET", path, query))
            try:
                with urlopen(req, timeout=20) as r:
                    return json.loads(r.read().decode())
            except HTTPError as e:
                body=""
                try: body=e.read().decode("utf-8","ignore")
                except Exception: pass
                raise RuntimeError(f"Gate HTTP {e.code}: {body or e.reason}") from e
            except (URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError) as e:
                last_error = e
                if attempt < 2:
                    time.sleep((0.35, 0.9)[attempt] + random.uniform(0.0, 0.15))
                    continue
                reason = getattr(e, "reason", None) or str(e)
                raise RuntimeError(f"Gate 网络连接失败: {reason}") from e
        raise RuntimeError(f"Gate 网络连接失败: {last_error}")


    def _ws_auth(self, channel, event, ts):
        s=f"channel={channel}&event={event}&time={ts}"
        return {"method":"api_key","KEY":self.key,"SIGN":hmac.new(self.secret.encode(),s.encode(),hashlib.sha512).hexdigest()}

    def _get_user_id(self):
        errors=[]
        # Futures read permission is enough for the normal USDT perpetual setup.
        # This avoids requiring Wallet permission just to discover the Gate user id.
        if self.enable_futures:
            try:
                data=self._get("/futures/usdt/accounts")
                uid=(data or {}).get("user") or (data or {}).get("user_id")
                if uid:
                    self.user_id=str(uid); return self.user_id
            except Exception as e:
                errors.append(f"futures: {e}")
        for path, field in (("/account/detail","user_id"),("/wallet/fee","user_id")):
            try:
                data=self._get(path); uid=(data or {}).get(field)
                if uid:
                    self.user_id=str(uid); return self.user_id
            except Exception as e:
                errors.append(f"{path}: {e}")
        raise RuntimeError("Gate API 已认证但无法取得 user_id。请确认 USDT 永续读取权限；详情: "+" | ".join(errors[-3:]))

    @staticmethod
    def _f(v, default=0.0):
        try:return float(v)
        except Exception:return default

    @staticmethod
    def _valid_execution(e):
        if not isinstance(e,dict): return False,"不是有效成交对象"
        if not str(e.get("execution_id") or "").strip(): return False,"缺少 execution_id"
        if not str(e.get("symbol") or "").strip(): return False,"缺少 symbol"
        if str(e.get("side") or "") not in {"Buy","Sell"}: return False,"side 无效"
        try: price=float(e.get("price") or 0); qty=float(e.get("qty") or 0)
        except Exception: return False,"价格或数量无法解析"
        if price<=0: return False,"price<=0"
        if qty<=0: return False,"qty<=0"
        return True,""

    async def _emit_if_valid(self,e,source="realtime"):
        ok,reason=self._valid_execution(e)
        if not ok:
            # Invalid exchange rows are diagnostics only. They must never enter the durable execution queue.
            await self.emit({"type":"adapter_diagnostic","provider":self.id,"provider_type":self.TYPE,"provider_name":self.name,"level":"warning","message":f"Gate {source} 跳过无效成交 · {e.get('symbol') or '-'} · {reason} · exec={e.get('execution_id') or '-'}"})
            return False
        await self.emit(e); return True

    def _contract_spec(self, contract):
        c=str(contract or "").upper()
        if not c:return {}
        if c in self._contract_spec_cache:return self._contract_spec_cache[c]
        out={}
        try:
            data=self._get(f"/futures/usdt/contracts/{c}") or {}
            mult=self._f(data.get("quanto_multiplier"),0.0); tick=self._f(data.get("order_price_round"),0.0)
            if mult>0:out["contract_multiplier"]=mult
            if tick>0:out["tick_size"]=tick
            if mult>0 and tick>0:out["tick_value"]=mult*tick
            out["contract_type"]=str(data.get("type") or "direct").lower(); out["settle_currency"]="USDT";out["contract_spec_source"]="gate_contract"
        except Exception:out={}
        self._contract_spec_cache[c]=out
        self._contract_multiplier_cache[c]=out.get("contract_multiplier")
        return out

    def _contract_multiplier(self, contract):
        return self._contract_spec(contract).get("contract_multiplier")

    @staticmethod
    def _pick_position_row(rows, target_sign=0):
        if isinstance(rows,dict):
            return rows
        if not isinstance(rows,list) or not rows:
            return {}
        nonzero=[]
        for r in rows:
            if not isinstance(r,dict):
                continue
            try:size=float(r.get("size") or 0)
            except Exception:size=0.0
            if abs(size)>0:nonzero.append((r,size))
        if target_sign:
            same=[r for r,size in nonzero if (size>0 and target_sign>0) or (size<0 and target_sign<0)]
            if same:return same[0]
        if len(nonzero)==1:return nonzero[0][0]
        return nonzero[0][0] if nonzero else (rows[0] if isinstance(rows[0],dict) else {})

    def _position_snapshot(self, contract, cache_tag="", target_sign=0):
        c=str(contract or "").upper()
        if not c:return {}
        tag=str(cache_tag or "")
        now=time.time(); cached=self._position_snapshot_cache.get(c)
        # Reuse a snapshot only for repeated fills of the same exchange order. A new
        # order must refresh the position state so max-margin tracking does not reuse
        # a stale snapshot from an earlier add/reduce operation.
        if cached and str(cached.get("_tag") or "")==tag and now-float(cached.get("_ts") or 0)<10:return dict(cached)
        last_error=""; out={"_ts":now,"_tag":tag}
        # Gate can expose positions differently in one-way vs hedge/split-position mode,
        # and the position REST state can lag the fill notification slightly. Retry and
        # accept either the normal or dual_comp response shape.
        for attempt in range(3):
            row={}; source=""
            try:
                data=self._get(f"/futures/usdt/positions/{c}")
                row=self._pick_position_row(data,target_sign)
                source="gate_position"
            except Exception as e:
                last_error=str(e)
            if not row:
                try:
                    data=self._get(f"/futures/usdt/dual_comp/positions/{c}")
                    row=self._pick_position_row(data,target_sign)
                    source="gate_dual_position"
                except Exception as e:
                    last_error=str(e)
            if row:
                raw_lev=self._f(row.get("leverage"),0.0)
                lever=self._f(row.get("lever"),0.0) or raw_lev or self._f(row.get("cross_leverage_limit"),0.0)
                if lever>0:out["leverage"]=lever
                margin=self._f(row.get("margin"),0.0); value=self._f(row.get("value"),0.0)
                initial_margin=self._f(row.get("initial_margin"),0.0); maintenance_margin=self._f(row.get("maintenance_margin"),0.0)
                if margin>0:out["position_margin"]=abs(margin)
                if initial_margin>0:out["initial_margin"]=abs(initial_margin)
                if maintenance_margin>0:out["maintenance_margin"]=abs(maintenance_margin)
                if value:out["position_value"]=abs(value)
                mode=str(row.get("pos_margin_mode") or "").strip().lower()
                if not mode:mode="cross" if raw_lev==0 and self._f(row.get("cross_leverage_limit"),0.0)>0 else ("isolated" if lever>0 else "")
                if mode:out["margin_mode"]=mode
                out["margin_snapshot_source"]=source
                # Gate's current schema also exposes a dedicated leverage query. Use it
                # only as a fallback when the position row has capital data but no lever.
                if not out.get("leverage") and mode:
                    try:
                        dual_side="dual_long" if target_sign>0 else ("dual_short" if target_sign<0 else "")
                        params={"pos_margin_mode":mode}
                        if dual_side:params["dual_side"]=dual_side
                        lev_data=self._get(f"/futures/usdt/get_leverage/{c}",params) or {}
                        lev=self._f(lev_data.get("Lever"),0.0) or self._f(lev_data.get("lever"),0.0) or self._f(lev_data.get("leverage"),0.0)
                        if lev>0:out["leverage"]=lev;out["leverage_source"]="gate_get_leverage"
                    except Exception as e:
                        last_error=str(e)
                if out.get("leverage") or out.get("position_margin") is not None or out.get("initial_margin") is not None:
                    break
            if attempt<2:time.sleep((0.20,0.55)[attempt])
        if last_error and not (out.get("leverage") or out.get("position_margin") is not None or out.get("initial_margin") is not None):
            out["snapshot_error"]=last_error[:300]
        self._position_snapshot_cache[c]=dict(out);return out

    def _enrich_fut_trade(self, x):
        row=dict(x or {})
        if self._f(row.get("trade_value"),0.0)>0:
            return row
        oid=str(row.get("order_id") or "").strip()
        if not oid:
            return row
        if oid not in self._order_trade_cache:
            try:
                rows=self._get("/futures/usdt/my_trades",{"order":oid,"limit":1000}) or []
            except Exception:
                rows=[]
            self._order_trade_cache[oid]=[dict(z or {}) for z in rows]
        target=str(row.get("id") or row.get("trade_id") or "")
        for z in self._order_trade_cache.get(oid,[]):
            zid=str(z.get("id") or z.get("trade_id") or "")
            if target and zid==target:
                merged=dict(row);merged.update({k:v for k,v in z.items() if v not in (None,"")});return merged
        return row

    @staticmethod
    def _effect_parts(x, qty):
        try: close_raw=abs(float((x or {}).get("close_size") or 0))
        except Exception: close_raw=0.0
        close_qty=min(abs(float(qty or 0)),close_raw) if close_raw>0 else 0.0
        open_qty=max(0.0,abs(float(qty or 0))-close_qty)
        if close_qty>0 and open_qty>0: effect="reversal"
        elif close_qty>0: effect="close"
        else: effect="open"
        return effect,close_qty,open_qty

    def _norm_spot(self,x):
        ms=x.get("create_time_ms") or (float(x.get("create_time") or time.time())*1000)
        try: ms=int(float(ms))
        except Exception: ms=int(time.time()*1000)
        pair=str(x.get("currency_pair") or "").upper()
        price=self._f(x.get("price")); qty=abs(self._f(x.get("amount"))); notional=abs(price*qty) if price>0 and qty>0 else None
        return {"type":"execution","schema_version":4,"provider":self.id,"provider_type":self.TYPE,"provider_name":self.name,
                "account_id":self.user_id or str(x.get("user_id") or ""),"execution_id":str(x.get("id") or x.get("id_market") or ""),"order_id":str(x.get("order_id") or ""),
                "symbol":pair,"asset_type":"虚拟币","side":"Buy" if str(x.get("side") or "").lower()=="buy" else "Sell",
                "price":price,"qty":qty,"notional":notional,"notional_source":"price_x_qty","margin_estimate":notional,"leverage":1.0,"margin_mode":"spot_cash","margin_model":"cash","value_model":"spot","quantity_unit":"base_asset",
                "fee":abs(self._f(x.get("fee"))),"fee_currency":str(x.get("fee_currency") or x.get("money") or ""),"exec_time":ms,"category":"spot","raw":x}

    def _norm_fut(self,x,use_position_snapshot=True):
        x=self._enrich_fut_trade(x)
        size=self._f(x.get("size")); ms=x.get("create_time_ms") or (float(x.get("create_time") or time.time())*1000)
        try: ms=int(float(ms))
        except Exception: ms=int(time.time()*1000)
        contract=str(x.get("contract") or "").upper(); spec=self._contract_spec(contract); spec_mult=spec.get("contract_multiplier")
        price=self._f(x.get("price")); qty=abs(size); trade_value=self._f(x.get("trade_value"),0.0)
        # For direct USDT contracts, exchange trade_value is authoritative and can
        # also recover the true per-contract multiplier. This avoids legacy guessed
        # multipliers and keeps notional/PnL on the exact same contract specification.
        derived_mult=(abs(trade_value)/(abs(price)*qty)) if trade_value and price>0 and qty>0 and str(spec.get("contract_type") or "direct").lower()=="direct" else None
        mult=derived_mult if derived_mult and derived_mult>0 else spec_mult
        mult_source="gate_trade_value_derived" if derived_mult and derived_mult>0 else ("gate_contract" if mult else "")
        notional=abs(trade_value) if trade_value else (abs(price*qty*mult) if price>0 and qty>0 and mult else None)
        effect,close_qty,open_qty=self._effect_parts(x,qty)
        # Opening quantity follows trade sign; a pure close targets the opposite side.
        target_sign=(1 if size>0 else -1) if open_qty>0 else (-1 if size>0 else 1)
        pos=self._position_snapshot(contract,str(x.get("order_id") or x.get("id") or x.get("trade_id") or ""),target_sign) if use_position_snapshot else {}
        leverage=self._f(pos.get("leverage"),0.0); margin_est=(notional/leverage) if notional is not None and leverage>0 else None
        return {"type":"execution","schema_version":5,"provider":self.id,"provider_type":self.TYPE,"provider_name":self.name,
                "account_id":self.user_id,"execution_id":str(x.get("id") or x.get("trade_id") or ""),"order_id":str(x.get("order_id") or ""),
                "symbol":contract,"asset_type":"虚拟币","side":"Buy" if size>0 else "Sell",
                "price":price,"qty":qty,"close_qty":close_qty,"open_qty":open_qty,"position_effect":effect,
                "contract_multiplier":mult,"multiplier_source":mult_source,"tick_size":spec.get("tick_size"),"tick_value":spec.get("tick_value"),"settle_currency":"USDT","contract_type":spec.get("contract_type") or "direct",
                "notional":notional,"trade_value":abs(trade_value) if trade_value else None,"notional_source":"exchange_trade_value" if trade_value else "price_x_qty_x_contract_size",
                "leverage":leverage or None,"margin_estimate":margin_est,"margin_source":"position_snapshot_notional_over_leverage" if margin_est is not None else "",
                "initial_margin_snapshot":pos.get("initial_margin"),"maintenance_margin_snapshot":pos.get("maintenance_margin"),
                "position_margin_snapshot":pos.get("position_margin"),"position_value_snapshot":pos.get("position_value"),"margin_mode":pos.get("margin_mode") or "",
                "margin_snapshot_source":pos.get("margin_snapshot_source") or "","margin_snapshot_error":pos.get("snapshot_error") or "",
                "value_model":"crypto_derivative","quantity_unit":"contracts",
                "fee":abs(self._f(x.get("fee"))),"fee_currency":"USDT","exec_time":ms,"category":"futures","raw":x}

    def _poll_window(self, kind: str):
        now=int(time.time())
        attr="_spot_poll_from" if kind=="spot" else "_fut_poll_from"
        prev=int(getattr(self,attr,0) or 0)
        # Always overlap a few seconds. The durable queue dedupes by execution id.
        start=max(0, (prev-5) if prev else (now-60))
        # Anchor the first attempt too: a long failed request must not slide the
        # initial 60-second window forward on its next attempt.
        if not prev:
            setattr(self,attr,start)
        return start, now


    async def _poll_spot_once(self):
        start,now=self._poll_window("spot")
        rows=await self._trade_rows("spot",start,now)
        count=0
        for x in rows:
            e=self._norm_spot(x)
            if not await self._emit_if_valid(e,"现货 REST 轮询"):
                raise RuntimeError("Gate 现货轮询存在无效成交；未推进水位")
            count+=1
        self._spot_poll_from=now
        return count


    async def _poll_fut_once(self):
        start,now=self._poll_window("futures")
        rows=await self._trade_rows("futures",start,now)
        count=0
        for x in rows:
            e=await asyncio.to_thread(self._norm_fut,x)
            if not await self._emit_if_valid(e,"永续 REST 轮询"):
                raise RuntimeError("Gate 永续轮询存在无效成交；未推进水位")
            count+=1
        self._fut_poll_from=now
        return count


    async def _fallback_window(self, kind: str, reason: Exception):
        if not self.rest_fallback_enabled:
            await asyncio.sleep(self.ws_retry_interval)
            return
        label="现货" if kind=="spot" else "USDT 永续"
        await self.status("fallback",f"Gate {label} WebSocket 不可用，已自动切换 REST 轮询 · {reason}")
        end=asyncio.get_running_loop().time()+self.ws_retry_interval
        while True:
            try:
                if kind=="spot": await self._poll_spot_once()
                else: await self._poll_fut_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self.status("error",f"Gate {label} REST 回退也失败: {e}")
            remain=end-asyncio.get_running_loop().time()
            if remain<=0: break
            await asyncio.sleep(min(self.rest_poll_interval,remain))

    async def _spot_loop(self):
        backoff=2.0
        while True:
            try:
                await self.status("connecting","Gate 现货成交流连接中")
                async with connect(self.spot_ws, ping_interval=20, ping_timeout=20, open_timeout=15) as ws:
                    ts=int(time.time()); req={"time":ts,"channel":"spot.usertrades","event":"subscribe","payload":["!all"]}; req["auth"]=self._ws_auth(req["channel"],req["event"],ts); await ws.send(json.dumps(req))
                    await self.status("connected","Gate 现货成交流已连接"); backoff=2
                    async for raw in ws:
                        m=json.loads(raw)
                        await self.health_event("stream")
                        if m.get("channel")=="spot.usertrades" and m.get("event")=="update":
                            for x in m.get("result") or []:
                                e=self._norm_spot(x)
                                await self._emit_if_valid(e,"现货 WebSocket")
                        if m.get("error"): raise RuntimeError(f"Gate spot WS: {m.get('error')}")
            except asyncio.CancelledError: raise
            except Exception as e:
                await self.status("error",f"Gate 现货 WebSocket: {e}")
                await self._fallback_window("spot",e)
                backoff=min(backoff*1.8,30)

    async def _fut_loop(self):
        backoff=2.0
        while True:
            try:
                if not self.user_id: await asyncio.to_thread(self._get_user_id)
                await self.status("connecting","Gate USDT 永续成交流连接中")
                async with connect(self.futures_ws, additional_headers={"X-Gate-Size-Decimal":"1"}, ping_interval=20, ping_timeout=20, open_timeout=15) as ws:
                    ts=int(time.time()); req={"time":ts,"channel":"futures.usertrades","event":"subscribe","payload":[self.user_id,"!all"]}; req["auth"]=self._ws_auth(req["channel"],req["event"],ts); await ws.send(json.dumps(req))
                    await self.status("connected","Gate 现货 / USDT 永续只读成交监听已连接" if self.enable_spot else "Gate USDT 永续成交流已连接"); backoff=2
                    async for raw in ws:
                        m=json.loads(raw)
                        await self.health_event("stream")
                        if m.get("channel")=="futures.usertrades" and m.get("event")=="update":
                            for x in m.get("result") or []:
                                e=await asyncio.to_thread(self._norm_fut,x)
                                if e.get("margin_snapshot_error"):
                                    await self.status("warning",f"Gate {e.get('symbol') or ''} 成交已收到，但杠杆/保证金快照暂未取得：{e.get('margin_snapshot_error')}")
                                await self._emit_if_valid(e,"永续 WebSocket")
                        if m.get("error"): raise RuntimeError(f"Gate futures WS: {m.get('error')}")
            except asyncio.CancelledError: raise
            except Exception as e:
                await self.status("error",f"Gate 永续 WebSocket: {e}")
                await self._fallback_window("futures",e)
                backoff=min(backoff*1.8,30)

    async def _risk_watch_loop(self):
        """Poll newly observed Gate price-trigger orders; the bridge validates stop direction."""
        while True:
            try:
                rows=await asyncio.to_thread(self._get,"/futures/usdt/price_orders",{"status":"open","limit":100})
                now=int(time.time()*1000); active={}
                for row in rows or []:
                    initial=row.get("initial") or {}; trigger=row.get("trigger") or {}
                    sym=str(initial.get("contract") or "").upper(); stop=self._f(trigger.get("price"),0.0)
                    typ=str(row.get("order_type") or "").lower()
                    if not sym or stop<=0 or not typ.startswith(("close-long","close-short","plan-close-long","plan-close-short")): continue
                    side="long" if "long" in typ else "short"; key=(sym,side)
                    # If several orders exist, keep the earliest observed price for event emission.
                    active.setdefault(key,stop)
                    if not self._risk_baselined or self._risk_seen.get(key)==stop: continue
                    await self.emit({"type":"risk_update","schema_version":1,"risk_type":"protective_stop","provider":self.id,"provider_type":self.TYPE,"provider_name":self.name,"account_id":self.user_id,"symbol":sym,"position_side":side,"protective_stop_price":stop,"detected_at":now,"source":"gate_price_order","raw":{"id":row.get("id_string") or row.get("id"),"order_type":typ,"trigger":trigger}})
                self._risk_seen=active
                self._risk_baselined=True
                self._risk_failures=0
                if self._risk_unavailable:
                    self._risk_unavailable=False
                    await self.status("connected","Gate 初始保护性止损监听已恢复")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._risk_failures += 1
                now=time.time()
                # Do not turn a single transient TLS/VPS hiccup into a persistent warning.
                if self._risk_failures>=3 and now-self._risk_warn_at>=60:
                    self._risk_warn_at=now
                    self._risk_unavailable=True
                    await self.status("warning",f"Gate 初始保护性止损监听暂时不可用（连续 {self._risk_failures} 次）：{e}")
            await asyncio.sleep(4.0)


    async def run(self):
        self._check()
        if not self.enable_spot and not self.enable_futures:
            raise RuntimeError("Gate 至少需要启用现货或 USDT 永续中的一个")
        # Preflight using only the permissions actually requested in the UI.
        if self.enable_spot:
            await asyncio.to_thread(self._get,"/spot/accounts")
        if self.enable_futures:
            await asyncio.to_thread(self._get_user_id)
        await self.status("authenticated",f"Gate API 只读认证成功 · {'TestNet 模拟环境' if self.environment=='testnet' else '实盘环境'}")
        tasks=[]
        if self.enable_spot: tasks.append(asyncio.create_task(self._spot_loop()))
        if self.enable_futures:
            tasks.append(asyncio.create_task(self._fut_loop()))
            tasks.append(asyncio.create_task(self._risk_watch_loop()))
        try: await asyncio.gather(*tasks)
        finally:
            for t in tasks: t.cancel()

    async def handle_command(self, command):
        a=command.get("action")
        if a=="network_diagnostic":
            self._check()
            targets=[("REST DNS/TLS",self.rest)]
            if self.enable_spot: targets.append(("现货 WebSocket DNS/TLS",self.spot_ws))
            if self.enable_futures: targets.append(("永续 WebSocket DNS/TLS",self.futures_ws))
            details=await self.network_probe(targets)
            api_ok=False
            try:
                checks=[]
                if self.enable_spot:
                    await asyncio.to_thread(self._get,"/spot/accounts"); checks.append("现货读取")
                if self.enable_futures:
                    await asyncio.to_thread(self._get_user_id); checks.append("USDT 永续读取")
                details.append({"stage":"API 认证","ok":True,"message":" + ".join(checks)+" ✓"}); api_ok=True
            except Exception as e:
                details.append({"stage":"API 认证","ok":False,"message":str(e)})
            rest_ok=bool(details and details[0].get("ok"))
            ws_failed=any(("WebSocket" in str(x.get("stage") or "")) and not x.get("ok") for x in details)
            if self.rest_fallback_enabled and rest_ok and api_ok:
                details.append({"stage":"REST 自动回退","ok":True,"message":f"已就绪 · WebSocket 不可用时每 {self.rest_poll_interval:g} 秒轮询成交，并自动重试 WebSocket"})
            # A broken WS path is degraded, not fatal, when authenticated REST fallback is available.
            ok=bool(rest_ok and api_ok and (not ws_failed or self.rest_fallback_enabled))
            if ok and ws_failed:
                msg="REST/API 正常；WebSocket 当前不可用，系统将自动使用 REST 轮询回退"
            else:
                msg=" · ".join(f"{x['stage']} {'✓' if x['ok'] else '✗'}" for x in details)
            return {"ok":ok,"kind":"network_diagnostic","message":"Gate 网络诊断："+msg,"details":details}
        if a=="connection_test":
            self._check(); checks=[]
            if self.enable_spot:
                await asyncio.to_thread(self._get,"/spot/accounts"); checks.append("现货读取 ✓")
            if self.enable_futures:
                uid=await asyncio.to_thread(self._get_user_id); checks.append(f"USDT 永续读取 ✓ · user_id {uid}")
            if not checks: return {"ok":False,"message":"Gate 至少需要启用现货或 USDT 永续中的一个"}
            return {"ok":True,"message":f"Gate API 只读认证成功 · {'TestNet 模拟环境' if self.environment=='testnet' else '实盘环境'} · "+" · ".join(checks)}
        if a=="history_sync":
            days=max(1,min(int(command.get("days") or 1),7)); now=int(time.time()); start=now-days*86400; total=0; skipped=0; reasons={}
            async def accept(e,label):
                nonlocal total,skipped
                ok,reason=self._valid_execution(e)
                if not ok:
                    skipped+=1; reasons[reason]=reasons.get(reason,0)+1
                    await self.emit({"type":"adapter_diagnostic","provider":self.id,"provider_type":self.TYPE,"provider_name":self.name,"level":"warning","message":f"Gate 历史补同步跳过无效{label}成交 · {e.get('symbol') or '-'} · {reason} · exec={e.get('execution_id') or '-'}"})
                    return
                await self.emit(e); total+=1
            if self.enable_spot:
                rows=await self._trade_rows("spot",start,now)
                for x in sorted(rows or [], key=lambda z:float(z.get("create_time_ms") or 0)):
                    await accept(self._norm_spot(x),"现货")
            if self.enable_futures:
                rows=await self._trade_rows("futures",start,now)
                for x in sorted(rows or [], key=lambda z:float(z.get("create_time_ms") or 0)):
                    await accept(self._norm_fut(x,use_position_snapshot=False),"永续")
            extra=("；跳过无效成交 "+str(skipped)+" 条（"+"、".join(f"{k} {v}" for k,v in reasons.items())+"）") if skipped else ""
            return {"ok":True,"message":f"Gate 历史成交补同步完成：有效 {total} 条"+extra,"accepted":total,"skipped":skipped,"skip_reasons":reasons}
        return await super().handle_command(command)

    @staticmethod
    def _trade_sort_key(row):
        from decimal import Decimal
        # Futures REST has create_time (seconds), not necessarily create_time_ms.
        stamp = (Decimal(str(row["create_time_ms"])) if row.get("create_time_ms")
                 else Decimal(str(row.get("create_time") or 0)) * 1000)
        if not stamp.is_finite() or stamp <= 0:
            raise RuntimeError("Gate 成交时间无效；未推进水位")
        return stamp, str(row.get("id") or row.get("trade_id") or ""), str(row.get("order_id") or "")


    async def _trade_rows(self, kind, start, end):
        """Read a fixed window completely before emitting it, oldest first.

        A repeated page or the documented spot offset ceiling is an error, not
        a successful partial sync. The caller retains its recovery watermark.
        """
        spot = kind == "spot"
        path = "/spot/my_trades" if spot else "/futures/usdt/my_trades_timerange"
        limit = 1000
        offset = 0
        all_rows = []
        seen_pages = set()
        while True:
            if offset > 100000:
                raise RuntimeError("Gate 成交分页超过安全上限，请缩小补同步时间范围；未推进水位")
            params = {"from":start, "to":end, "limit":limit}
            params.update({"page":offset // limit + 1} if spot else {"offset":offset})
            rows = await asyncio.to_thread(self._get, path, params)
            if not isinstance(rows, list):
                raise RuntimeError("Gate 成交接口返回格式异常；未推进水位")
            if not rows:
                break
            fingerprint = json.dumps(rows, sort_keys=True, separators=(",", ":"))
            if fingerprint in seen_pages:
                raise RuntimeError("Gate 成交分页重复，无法确认完整性；未推进水位")
            seen_pages.add(fingerprint)
            all_rows.extend(rows)
            if len(rows) < limit:
                break
            offset += limit
            await asyncio.sleep(0.05)
        # Keep an ID scoped to its market; do not collapse fills of one order.
        unique = {}
        for row in all_rows:
            key = (str(row.get("contract") or row.get("currency_pair") or ""),
                   str(row.get("id") or row.get("trade_id") or ""))
            if not key[1]:
                raise RuntimeError("Gate 成交缺少唯一 ID；未推进水位")
            if key in unique and unique[key] != row:
                raise RuntimeError("Gate 重复成交 ID 内容冲突；未推进水位")
            unique[key] = row
        return sorted(unique.values(), key=self._trade_sort_key)

