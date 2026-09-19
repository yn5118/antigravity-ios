"""ウォレット監視レイヤ。

外部 API から対象アドレスの新規スワップを取得し、正規化した :class:`SwapEvent`
を asyncio.Queue に流し込む。

    Solana : Helius Enhanced Transactions API（推奨） / Solana JSON-RPC（フォールバック）
    EVM    : Etherscan V2 マルチチェーン API（tokentx）
    価格   : DEXScreener API（TTL キャッシュ付き）

いずれのモニタも「ポーリング + 既読署名の重複排除」で動く。WebSocket を使いたい
場合は :class:`BaseWalletMonitor.run` を差し替えれば他のレイヤはそのまま使える。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import aiohttp

try:  # `copytrade.monitor` / 単体の `monitor` どちらでも動くように
    from . import config
except ImportError:  # pragma: no cover
    import config

log = logging.getLogger("copytrade.monitor")

BUY = "BUY"
SELL = "SELL"
#: これ未満の残高変化はガス/端数として無視する
DUST = 1e-9
#: ネイティブ通貨の残高変化はガス代を含むため閾値を大きめに取る
NATIVE_DUST = 1e-4


def now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------
# 正規化イベント
# --------------------------------------------------------------------------
@dataclass(slots=True)
class SwapEvent:
    """監視ウォレットが行った 1 件のスワップ。"""

    chain: str
    wallet: str
    wallet_label: str
    tx_hash: str
    side: str  # BUY / SELL
    token_address: str
    token_symbol: str
    token_amount: float
    usd_amount: float
    price_usd: float
    block_time_ms: int
    detected_at_ms: int
    source: str = ""
    quote_symbol: str = ""
    weight: float = 1.0

    @property
    def detect_lag_ms(self) -> int:
        """ブロック確定から検知までのタイムラグ（ミリ秒）。"""
        if not self.block_time_ms:
            return 0
        return max(0, self.detected_at_ms - self.block_time_ms)

    @property
    def uid(self) -> str:
        return f"{self.chain}:{self.tx_hash}:{self.token_address}:{self.side}"

    def describe(self) -> str:
        return (
            f"[{self.chain}/{self.wallet_label}] {self.side} {self.token_symbol} "
            f"qty={self.token_amount:,.4f} usd={self.usd_amount:,.2f} "
            f"px={self.price_usd:.8g} lag={self.detect_lag_ms}ms tx={self.tx_hash[:12]}.."
        )


@dataclass(slots=True)
class SwapLeg:
    """スワップから抽出した「銘柄側 / 決済通貨側」の組。"""

    token_address: str
    token_symbol: str
    token_amount: float
    side: str
    quote_address: str = ""
    quote_symbol: str = ""
    quote_amount: float = 0.0


def classify_swap(chain: str, deltas: dict[str, tuple[float, str]]) -> SwapLeg | None:
    """ウォレットの残高差分から売買方向と銘柄を判定する。

    Args:
        chain: チェーン名。
        deltas: ``{token_address: (残高差分, シンボル)}``。差分は受取が正、支払が負。

    Returns:
        判定できた場合は :class:`SwapLeg`、スワップと見なせない場合は ``None``。
    """
    token_side: list[tuple[str, float, str]] = []
    quote_side: list[tuple[str, float, str]] = []

    for address, (delta, symbol) in deltas.items():
        threshold = NATIVE_DUST if address.startswith("native:") else DUST
        if abs(delta) < threshold:
            continue
        bucket = quote_side if config.is_quote_token(chain, address) else token_side
        bucket.append((address, delta, symbol))

    if not token_side:
        # USDC <-> SOL のような決済通貨同士の両替はシグナルとして扱わない
        return None

    address, delta, symbol = max(token_side, key=lambda item: abs(item[1]))
    side = BUY if delta > 0 else SELL

    quote_address = quote_symbol = ""
    quote_amount = 0.0
    opposite = [item for item in quote_side if item[1] * delta < 0]
    if opposite:
        quote_address, quote_delta, q_symbol = max(opposite, key=lambda item: abs(item[1]))
        quote_amount = abs(quote_delta)
        quote_symbol = q_symbol or config.quote_symbol(chain, quote_address)

    return SwapLeg(
        token_address=address,
        token_symbol=symbol,
        token_amount=abs(delta),
        side=side,
        quote_address=quote_address,
        quote_symbol=quote_symbol,
        quote_amount=quote_amount,
    )


# --------------------------------------------------------------------------
# 価格取得（DEXScreener）
# --------------------------------------------------------------------------
@dataclass(slots=True)
class PriceQuote:
    price_usd: float
    symbol: str
    liquidity_usd: float
    fetched_at_ms: int
    pair_url: str = ""


class PriceFeed:
    """DEXScreener のトークン価格を TTL キャッシュ付きで取得する。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        ttl_sec: float | None = None,
        max_concurrency: int = 4,
        enabled: bool = True,
    ) -> None:
        self._session = session
        self._ttl_ms = int((config.PRICE_CACHE_TTL_SEC if ttl_sec is None else ttl_sec) * 1000)
        self._cache: dict[str, PriceQuote] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._sem = asyncio.Semaphore(max_concurrency)
        self._enabled = enabled
        self.requests = 0
        self.failures = 0

    async def get(self, chain: str, token_address: str) -> PriceQuote | None:
        """価格を返す。取得できなければ ``None``。"""
        if not self._enabled:
            return None
        if token_address.startswith("native:"):
            token_address = config.NATIVE_PRICE_REFERENCE.get(chain, "")
            if not token_address:
                return None

        key = f"{chain}:{token_address.lower()}"
        cached = self._cache.get(key)
        if cached and now_ms() - cached.fetched_at_ms < self._ttl_ms:
            return cached

        async with self._locks[key]:
            cached = self._cache.get(key)
            if cached and now_ms() - cached.fetched_at_ms < self._ttl_ms:
                return cached
            quote = await self._fetch(chain, token_address)
            if quote:
                self._cache[key] = quote
            return quote or cached

    async def usd_value(self, chain: str, token_address: str, symbol: str, amount: float) -> float:
        """トークン数量を USD に換算する（ステーブルは 1.0 固定）。"""
        if amount <= 0:
            return 0.0
        if config.is_stable(symbol):
            return amount
        quote = await self.get(chain, token_address)
        return amount * quote.price_usd if quote else 0.0

    def _note_failure(self, reason: str, token_address: str) -> None:
        """価格取得の失敗を記録する。継続的な失敗に気付けるよう間引いて警告する。"""
        self.failures += 1
        message = "DEXScreener 価格取得に失敗 (%s) token=%s / 累計 %d 件"
        if self.failures == 1 or self.failures % 50 == 0:
            log.warning(message, reason, token_address, self.failures)
        else:
            log.debug(message, reason, token_address, self.failures)

    async def _fetch(self, chain: str, token_address: str) -> PriceQuote | None:
        slug = config.DEXSCREENER_CHAINS.get(chain, chain)
        url = f"{config.DEXSCREENER_TOKENS_URL}/{token_address}"
        try:
            async with self._sem:
                self.requests += 1
                async with self._session.get(url) as resp:
                    if resp.status != 200:
                        self._note_failure(f"HTTP {resp.status}", token_address)
                        return None
                    payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self._note_failure(f"{type(exc).__name__}: {exc}", token_address)
            return None

        pairs = payload.get("pairs") or []
        candidates = [p for p in pairs if str(p.get("chainId", "")).lower() == slug]
        exact = [
            p
            for p in candidates
            if str((p.get("baseToken") or {}).get("address", "")).lower() == token_address.lower()
        ]
        pool = exact or candidates
        if not pool:
            return None

        best = max(pool, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0))
        try:
            price = float(best.get("priceUsd") or 0.0)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None

        return PriceQuote(
            price_usd=price,
            symbol=str((best.get("baseToken") or {}).get("symbol") or ""),
            liquidity_usd=float((best.get("liquidity") or {}).get("usd") or 0.0),
            fetched_at_ms=now_ms(),
            pair_url=str(best.get("url") or ""),
        )


# --------------------------------------------------------------------------
# モニタ基底クラス
# --------------------------------------------------------------------------
class BaseWalletMonitor:
    """1 ウォレットを担当するポーリングモニタ。"""

    source = "base"

    def __init__(
        self,
        wallet: config.WatchedWallet,
        price_feed: PriceFeed,
        session: aiohttp.ClientSession,
    ) -> None:
        self.wallet = wallet
        self.price_feed = price_feed
        self.session = session
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.polls = 0
        self.errors = 0
        self.emitted = 0

    # -- 重複排除 ----------------------------------------------------------
    def _is_new(self, identifier: str) -> bool:
        if identifier in self._seen:
            return False
        self._seen[identifier] = None
        while len(self._seen) > config.SEEN_CACHE_SIZE:
            self._seen.popitem(last=False)
        return True

    # -- サブクラスが実装 --------------------------------------------------
    async def poll(self) -> list[SwapEvent]:
        raise NotImplementedError

    async def prime(self) -> None:
        """起動時の過去履歴を既読にする（過去分の誤約定を防ぐ）。"""
        try:
            events = await self.poll()
            if events:
                log.info("[%s] 起動時に %d 件の過去取引を既読化", self.wallet.name, len(events))
        except Exception as exc:  # noqa: BLE001 - 起動時の失敗で全体を止めない
            log.warning("[%s] 初期化ポーリング失敗: %s", self.wallet.name, exc)

    # -- メインループ ------------------------------------------------------
    async def run(self, queue: "asyncio.Queue[SwapEvent]", stop: asyncio.Event) -> None:
        await self._sleep(random.uniform(0, config.STAGGER_SEC), stop)
        if stop.is_set():
            return
        if not config.EMIT_BACKLOG:
            await self.prime()

        backoff = config.POLL_INTERVAL_SEC
        while not stop.is_set():
            started = time.perf_counter()
            try:
                events = await self.poll()
                self.polls += 1
                backoff = config.POLL_INTERVAL_SEC
            except Exception as exc:  # noqa: BLE001 - 監視は落とさず再試行する
                self.errors += 1
                backoff = min(backoff * 2, config.MAX_BACKOFF_SEC)
                log.warning(
                    "[%s] ポーリング失敗 (%s): %s / %.1fs 後に再試行",
                    self.wallet.name,
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                await self._sleep(backoff, stop)
                continue

            for event in events:
                age_ms = now_ms() - event.block_time_ms if event.block_time_ms else 0
                if config.MAX_EVENT_AGE_SEC and age_ms > config.MAX_EVENT_AGE_SEC * 1000:
                    log.debug("[%s] 古いイベントを無視 (%.0fs前) %s", self.wallet.name, age_ms / 1000, event.tx_hash[:10])
                    continue
                self.emitted += 1
                log.info(
                    "検知 %s | poll=%.0fms",
                    event.describe(),
                    (time.perf_counter() - started) * 1000,
                )
                await queue.put(event)

            await self._sleep(config.POLL_INTERVAL_SEC, stop)

    @staticmethod
    async def _sleep(seconds: float, stop: asyncio.Event) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # -- 共通ヘルパ --------------------------------------------------------
    async def _build_event(
        self,
        leg: SwapLeg,
        tx_hash: str,
        block_time_ms: int,
        detected_at_ms: int,
    ) -> SwapEvent | None:
        """SwapLeg を USD 換算して SwapEvent にする。"""
        chain = self.wallet.chain
        usd = 0.0
        if leg.quote_amount > 0:
            usd = await self.price_feed.usd_value(
                chain, leg.quote_address, leg.quote_symbol, leg.quote_amount
            )
        if usd <= 0:
            # 決済通貨側が取れないケース（ネイティブ直スワップ等）は銘柄価格から逆算
            usd = await self.price_feed.usd_value(
                chain, leg.token_address, leg.token_symbol, leg.token_amount
            )
        if usd <= 0:
            log.debug("USD 換算できずスキップ: %s %s", leg.token_symbol, tx_hash[:10])
            return None

        symbol = leg.token_symbol
        if not symbol:
            quote = await self.price_feed.get(chain, leg.token_address)
            symbol = (quote.symbol if quote else "") or f"{leg.token_address[:4]}..{leg.token_address[-4:]}"

        return SwapEvent(
            chain=chain,
            wallet=self.wallet.address,
            wallet_label=self.wallet.name,
            tx_hash=tx_hash,
            side=leg.side,
            token_address=leg.token_address,
            token_symbol=symbol,
            token_amount=leg.token_amount,
            usd_amount=usd,
            price_usd=usd / leg.token_amount if leg.token_amount else 0.0,
            block_time_ms=block_time_ms,
            detected_at_ms=detected_at_ms,
            source=self.source,
            quote_symbol=leg.quote_symbol,
            weight=self.wallet.weight,
        )

    async def _get_json(self, url: str, params: dict[str, Any] | None = None, method: str = "GET",
                        json_body: Any | None = None) -> Any:
        async with self.session.request(method, url, params=params, json=json_body) as resp:
            if resp.status == 429:
                raise RuntimeError("レート制限 (HTTP 429)")
            if resp.status >= 400:
                text = (await resp.text())[:200]
                raise RuntimeError(f"HTTP {resp.status}: {text}")
            return await resp.json(content_type=None)


# --------------------------------------------------------------------------
# Solana: Helius Enhanced Transactions
# --------------------------------------------------------------------------
class HeliusSolanaMonitor(BaseWalletMonitor):
    source = "helius"

    async def poll(self) -> list[SwapEvent]:
        url = f"{config.HELIUS_BASE_URL}/v0/addresses/{self.wallet.address}/transactions"
        params = {"api-key": config.HELIUS_API_KEY, "limit": config.FETCH_LIMIT}
        payload = await self._get_json(url, params=params)
        detected_at = now_ms()
        if not isinstance(payload, list):
            raise RuntimeError(f"想定外のレスポンス: {str(payload)[:200]}")

        events: list[SwapEvent] = []
        for tx in reversed(payload):  # 古い順に処理
            signature = str(tx.get("signature") or "")
            if not signature or tx.get("transactionError"):
                continue
            if not self._is_new(signature):
                continue

            deltas = self._deltas_from_tx(tx)
            leg = classify_swap(config.SOLANA, deltas)
            if leg is None:
                continue
            block_time_ms = int(tx.get("timestamp") or 0) * 1000
            event = await self._build_event(leg, signature, block_time_ms, detected_at)
            if event:
                events.append(event)
        return events

    def _deltas_from_tx(self, tx: dict[str, Any]) -> dict[str, tuple[float, str]]:
        wallet = self.wallet.address
        deltas: dict[str, float] = defaultdict(float)
        symbols: dict[str, str] = {}

        for transfer in tx.get("tokenTransfers") or []:
            mint = str(transfer.get("mint") or "")
            if not mint:
                continue
            try:
                amount = float(transfer.get("tokenAmount") or 0.0)
            except (TypeError, ValueError):
                continue
            if transfer.get("toUserAccount") == wallet:
                deltas[mint] += amount
            elif transfer.get("fromUserAccount") == wallet:
                deltas[mint] -= amount
            symbols.setdefault(mint, config.quote_symbol(config.SOLANA, mint))

        for transfer in tx.get("nativeTransfers") or []:
            try:
                lamports = float(transfer.get("amount") or 0.0)
            except (TypeError, ValueError):
                continue
            sol = lamports / 1e9
            if transfer.get("toUserAccount") == wallet:
                deltas[config.NATIVE_SOL] += sol
            elif transfer.get("fromUserAccount") == wallet:
                deltas[config.NATIVE_SOL] -= sol
            symbols[config.NATIVE_SOL] = "SOL"

        return {mint: (delta, symbols.get(mint, "")) for mint, delta in deltas.items()}


# --------------------------------------------------------------------------
# Solana: JSON-RPC フォールバック
# --------------------------------------------------------------------------
class SolanaRpcMonitor(BaseWalletMonitor):
    source = "solana-rpc"
    #: 1 ポーリングで getTransaction を呼ぶ上限（公開 RPC 保護）
    MAX_TX_PER_POLL = 6

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        payload = await self._get_json(config.SOLANA_RPC_URL, method="POST", json_body=body)
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(f"RPC エラー {method}: {payload['error']}")
        return (payload or {}).get("result")

    async def poll(self) -> list[SwapEvent]:
        signatures = await self._rpc(
            "getSignaturesForAddress",
            [self.wallet.address, {"limit": min(config.FETCH_LIMIT, 25)}],
        ) or []

        fresh = [
            item
            for item in reversed(signatures)
            if not item.get("err") and self._is_new(str(item.get("signature")))
        ]
        events: list[SwapEvent] = []
        for item in fresh[-self.MAX_TX_PER_POLL :]:
            signature = str(item.get("signature"))
            tx = await self._rpc(
                "getTransaction",
                [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            )
            if not tx:
                continue
            detected_at = now_ms()
            deltas = self._deltas_from_tx(tx)
            leg = classify_swap(config.SOLANA, deltas)
            if leg is None:
                continue
            block_time_ms = int(tx.get("blockTime") or item.get("blockTime") or 0) * 1000
            event = await self._build_event(leg, signature, block_time_ms, detected_at)
            if event:
                events.append(event)
        return events

    def _deltas_from_tx(self, tx: dict[str, Any]) -> dict[str, tuple[float, str]]:
        wallet = self.wallet.address
        meta = tx.get("meta") or {}
        deltas: dict[str, float] = defaultdict(float)

        def scan(entries: Iterable[dict[str, Any]], sign: float) -> None:
            for entry in entries or []:
                if entry.get("owner") != wallet:
                    continue
                mint = str(entry.get("mint") or "")
                amount = (entry.get("uiTokenAmount") or {}).get("uiAmount")
                if not mint or amount is None:
                    continue
                deltas[mint] += sign * float(amount)

        scan(meta.get("postTokenBalances"), 1.0)
        scan(meta.get("preTokenBalances"), -1.0)

        # ネイティブ SOL（ガス代を含むため NATIVE_DUST で丸められる）
        keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
        addresses = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
        if wallet in addresses:
            index = addresses.index(wallet)
            pre = meta.get("preBalances") or []
            post = meta.get("postBalances") or []
            if index < len(pre) and index < len(post):
                deltas[config.NATIVE_SOL] += (float(post[index]) - float(pre[index])) / 1e9

        symbols = {config.NATIVE_SOL: "SOL"}
        return {
            mint: (delta, symbols.get(mint) or config.quote_symbol(config.SOLANA, mint))
            for mint, delta in deltas.items()
        }


# --------------------------------------------------------------------------
# EVM: Etherscan V2 (tokentx)
# --------------------------------------------------------------------------
class EvmScanMonitor(BaseWalletMonitor):
    source = "etherscan-v2"

    async def poll(self) -> list[SwapEvent]:
        chain_id = config.EVM_CHAIN_IDS[self.wallet.chain]
        params = {
            "chainid": chain_id,
            "module": "account",
            "action": "tokentx",
            "address": self.wallet.address,
            "page": 1,
            "offset": config.FETCH_LIMIT,
            "sort": "desc",
            "apikey": config.ETHERSCAN_API_KEY,
        }
        payload = await self._get_json(config.ETHERSCAN_V2_URL, params=params)
        detected_at = now_ms()

        status = str(payload.get("status", "0"))
        result = payload.get("result")
        if status != "1":
            message = str(payload.get("message") or "")
            if "No transactions found" in message or result == []:
                return []
            raise RuntimeError(f"Etherscan: {message} / {str(result)[:160]}")
        if not isinstance(result, list):
            raise RuntimeError(f"想定外のレスポンス: {str(result)[:160]}")

        grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
        for row in reversed(result):  # 古い順
            grouped.setdefault(str(row.get("hash")), []).append(row)

        events: list[SwapEvent] = []
        for tx_hash, rows in grouped.items():
            if not self._is_new(tx_hash):
                continue
            deltas = self._deltas_from_rows(rows)
            leg = classify_swap(self.wallet.chain, deltas)
            if leg is None:
                continue
            block_time_ms = int(rows[0].get("timeStamp") or 0) * 1000
            event = await self._build_event(leg, tx_hash, block_time_ms, detected_at)
            if event:
                events.append(event)
        return events

    def _deltas_from_rows(self, rows: Sequence[dict[str, Any]]) -> dict[str, tuple[float, str]]:
        wallet = self.wallet.address.lower()
        deltas: dict[str, float] = defaultdict(float)
        symbols: dict[str, str] = {}

        for row in rows:
            token = str(row.get("contractAddress") or "").lower()
            if not token:
                continue
            try:
                decimals = int(row.get("tokenDecimal") or 18)
                amount = float(int(row.get("value") or 0)) / (10**decimals)
            except (TypeError, ValueError):
                continue
            if str(row.get("to") or "").lower() == wallet:
                deltas[token] += amount
            elif str(row.get("from") or "").lower() == wallet:
                deltas[token] -= amount
            symbols[token] = str(row.get("tokenSymbol") or "")

        return {token: (delta, symbols.get(token, "")) for token, delta in deltas.items()}


# --------------------------------------------------------------------------
# オフライン検証用のシミュレータ
# --------------------------------------------------------------------------
class SimulatedMonitor(BaseWalletMonitor):
    """API キー無しで全体の流れを確認するための擬似モニタ。"""

    source = "simulated"

    TOKENS = [("BONK", 0.0000215), ("WIF", 2.31), ("PEPE", 0.0000094), ("JUP", 0.78)]

    def __init__(self, wallet, price_feed, session, interval_sec: float = 2.0) -> None:
        super().__init__(wallet, price_feed, session)
        self._counter = 0
        self._interval = interval_sec
        self._open: dict[str, tuple[float, float]] = {}

    async def prime(self) -> None:  # 過去履歴は無い
        return None

    async def run(self, queue: "asyncio.Queue[SwapEvent]", stop: asyncio.Event) -> None:
        await self._sleep(random.uniform(0, self._interval), stop)
        while not stop.is_set():
            for event in await self.poll():
                self.emitted += 1
                log.info("検知 %s", event.describe())
                await queue.put(event)
            await self._sleep(self._interval, stop)

    async def poll(self) -> list[SwapEvent]:
        self.polls += 1
        self._counter += 1
        symbol, base_price = random.choice(self.TOKENS)
        price = base_price * random.uniform(0.85, 1.15)
        address = f"sim{symbol}{'x' * 8}"

        if address in self._open and random.random() < 0.5:
            amount, _ = self._open.pop(address)
            side, token_amount = SELL, amount
        else:
            usd = random.uniform(config.MIN_SIGNAL_USD, config.MIN_SIGNAL_USD * 50)
            token_amount = usd / price
            self._open[address] = (token_amount, price)
            side = BUY

        block_time = now_ms() - random.randint(400, 2500)
        return [
            SwapEvent(
                chain=self.wallet.chain,
                wallet=self.wallet.address,
                wallet_label=self.wallet.name,
                tx_hash=f"sim-{self.wallet.name}-{self._counter:05d}",
                side=side,
                token_address=address,
                token_symbol=symbol,
                token_amount=token_amount,
                usd_amount=token_amount * price,
                price_usd=price,
                block_time_ms=block_time,
                detected_at_ms=now_ms(),
                source=self.source,
                quote_symbol="USDC",
                weight=self.wallet.weight,
            )
        ]


# --------------------------------------------------------------------------
# ファクトリ
# --------------------------------------------------------------------------
def build_monitor(
    wallet: config.WatchedWallet,
    price_feed: PriceFeed,
    session: aiohttp.ClientSession,
    simulate: bool = False,
) -> BaseWalletMonitor | None:
    """ウォレット設定に応じたモニタを作る。監視できない場合は ``None``。"""
    if simulate:
        return SimulatedMonitor(wallet, price_feed, session)

    if wallet.chain == config.SOLANA:
        if config.HELIUS_API_KEY:
            return HeliusSolanaMonitor(wallet, price_feed, session)
        log.warning("[%s] HELIUS_API_KEY が無いため公開 RPC で監視します", wallet.name)
        return SolanaRpcMonitor(wallet, price_feed, session)

    if wallet.chain in config.EVM_CHAIN_IDS:
        if not config.ETHERSCAN_API_KEY:
            log.error("[%s] ETHERSCAN_API_KEY が無いため監視をスキップします", wallet.name)
            return None
        return EvmScanMonitor(wallet, price_feed, session)

    log.error("[%s] 未対応チェーン: %s", wallet.name, wallet.chain)
    return None


def build_monitors(
    wallets: Sequence[config.WatchedWallet],
    price_feed: PriceFeed,
    session: aiohttp.ClientSession,
    simulate: bool = False,
) -> list[BaseWalletMonitor]:
    monitors = []
    for wallet in wallets:
        monitor = build_monitor(wallet, price_feed, session, simulate=simulate)
        if monitor:
            monitors.append(monitor)
    return monitors


def create_session() -> aiohttp.ClientSession:
    timeout = aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT_SEC)
    headers = {"User-Agent": "copytrade-demo/1.0", "Accept": "application/json"}
    return aiohttp.ClientSession(timeout=timeout, headers=headers)
