"""オンチェーンの売買を MT5 のメジャー銘柄（BTC/ETH/SOL）の方向性に集約する。

個々のスワップは「投票」として扱い、ローリングウィンドウ内のネット金額が
しきい値を超えたら LONG / SHORT シグナルを出す。1 件の大口取引だけで発火しない
よう、異なるウォレット数の下限とクールダウン、決済用のヒステリシスを持つ。

投票の作り方は 2 通り:

    直接 (direct) : WBTC / WETH / SOL など、メジャー資産そのものの売買。
                    BUY なら + 、SELL なら - 、金額はそのまま。
    代理 (proxy)  : それ以外のトークンの売買。そのチェーンのネイティブ資産に対する
                    リスクオン / リスクオフの代理指標として `SIGNAL_PROXY_WEIGHT`
                    倍して加算する（ミームコインを買う = そのチェーンに強気、という解釈）。

代理投票は解釈であって資金フローそのものではない点に注意（SOL でミームコインを
買う行為は、厳密には SOL を手放している）。`SIGNAL_PROXY_WEIGHT=0` にすれば
直接マッピングだけを使う。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Iterable

try:  # パッケージ / 単体の両対応
    from . import config
    from .monitor import BUY, SwapEvent, now_ms
except ImportError:  # pragma: no cover
    import config
    from monitor import BUY, SwapEvent, now_ms

log = logging.getLogger("copytrade.signals")

LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"


@dataclass(slots=True)
class Vote:
    """1 スワップ = 1 投票。"""

    ts_ms: int
    asset: str
    wallet: str
    usd: float  # 強気が正、弱気が負
    kind: str  # direct / proxy
    token_symbol: str


@dataclass(slots=True)
class MajorSignal:
    """メジャー銘柄に対する売買シグナル。"""

    asset: str
    direction: str  # LONG / SHORT / FLAT
    previous: str
    score_usd: float
    wallets: int
    votes: int
    created_at_ms: int
    reference_price: float = 0.0
    contributors: tuple[str, ...] = ()

    @property
    def is_entry(self) -> bool:
        return self.direction in (LONG, SHORT)

    def describe(self) -> str:
        return (
            f"シグナル {self.asset} {self.previous}->{self.direction} "
            f"net={self.score_usd:+,.0f} USD / {self.wallets}ウォレット {self.votes}件"
            + (f" / 参考価格 {self.reference_price:,.2f}" if self.reference_price else "")
            + (f" / {', '.join(self.contributors)}" if self.contributors else "")
        )


class SignalEngine:
    """SwapEvent を受け取り、資産ごとの方向性シグナルを生成する。"""

    def __init__(
        self,
        window_sec: float = config.SIGNAL_WINDOW_SEC,
        threshold_usd: float = config.SIGNAL_NET_USD_THRESHOLD,
        min_wallets: int = config.SIGNAL_MIN_WALLETS,
        exit_ratio: float = config.SIGNAL_EXIT_RATIO,
        proxy_weight: float = config.SIGNAL_PROXY_WEIGHT,
        cooldown_sec: float = config.SIGNAL_COOLDOWN_SEC,
    ) -> None:
        self.window_ms = int(window_sec * 1000)
        self.threshold_usd = threshold_usd
        self.min_wallets = min_wallets
        self.exit_ratio = exit_ratio
        self.proxy_weight = proxy_weight
        self.cooldown_ms = int(cooldown_sec * 1000)

        self._votes: Deque[Vote] = deque()
        self._state: dict[str, str] = {asset: FLAT for asset in config.MAJOR_ASSETS}
        self._last_entry_ms: dict[str, int] = {}
        self._last_price: dict[str, float] = {}
        self.emitted = 0
        self.ignored = 0

    # ------------------------------------------------------------------
    # 投票の生成
    # ------------------------------------------------------------------
    @staticmethod
    def asset_for_token(chain: str, token_address: str, token_symbol: str) -> tuple[str | None, str]:
        """(資産, 種別) を返す。対応する資産が無ければ (None, "")。"""
        table = {k.lower(): v for k, v in config.MAJOR_TOKEN_MAP.items()}
        asset = table.get(token_address.lower()) or table.get((token_symbol or "").lower())
        if asset:
            return asset, "direct"
        proxy = config.CHAIN_PROXY_ASSET.get(chain)
        return (proxy, "proxy") if proxy else (None, "")

    def _vote_for(self, event: SwapEvent) -> Vote | None:
        asset, kind = self.asset_for_token(event.chain, event.token_address, event.token_symbol)
        if asset is None or asset not in self._state:
            return None

        usd = event.usd_amount * (event.weight or 1.0)
        if kind == "proxy":
            usd *= self.proxy_weight
        if usd <= 0:
            return None
        if event.side != BUY:
            usd = -usd

        if kind == "direct" and event.price_usd > 0:
            self._last_price[asset] = event.price_usd

        return Vote(
            ts_ms=event.detected_at_ms or now_ms(),
            asset=asset,
            wallet=event.wallet_label or event.wallet,
            usd=usd,
            kind=kind,
            token_symbol=event.token_symbol,
        )

    # ------------------------------------------------------------------
    # 集計
    # ------------------------------------------------------------------
    def _prune(self, now: int) -> None:
        cutoff = now - self.window_ms
        while self._votes and self._votes[0].ts_ms < cutoff:
            self._votes.popleft()

    def _aggregate(self, asset: str) -> tuple[float, list[str], int]:
        net = 0.0
        wallets: dict[str, float] = {}
        count = 0
        for vote in self._votes:
            if vote.asset != asset:
                continue
            net += vote.usd
            wallets[vote.wallet] = wallets.get(vote.wallet, 0.0) + vote.usd
            count += 1
        # ネット方向に寄与したウォレットだけを数える（買いと売りの相殺を防ぐ）
        aligned = [w for w, usd in wallets.items() if usd * net > 0]
        return net, aligned, count

    # ------------------------------------------------------------------
    # 本体
    # ------------------------------------------------------------------
    def add(self, event: SwapEvent) -> MajorSignal | None:
        """イベントを取り込み、状態が変わったらシグナルを返す。"""
        vote = self._vote_for(event)
        if vote is None:
            self.ignored += 1
            return None

        self._votes.append(vote)
        now = vote.ts_ms
        self._prune(now)
        return self._evaluate(vote.asset, now)

    def _evaluate(self, asset: str, now: int) -> MajorSignal | None:
        net, aligned, count = self._aggregate(asset)
        current = self._state.get(asset, FLAT)
        wallets = len(aligned)
        target = current

        entry_ok = wallets >= self.min_wallets
        if net >= self.threshold_usd and entry_ok:
            target = LONG
        elif net <= -self.threshold_usd and entry_ok:
            target = SHORT
        elif current != FLAT and abs(net) < self.threshold_usd * self.exit_ratio:
            target = FLAT
        elif current == LONG and net < 0:
            target = FLAT
        elif current == SHORT and net > 0:
            target = FLAT

        if target == current:
            return None

        if target in (LONG, SHORT):
            last = self._last_entry_ms.get(asset, 0)
            if last and now - last < self.cooldown_ms:
                log.debug(
                    "%s のシグナルはクールダウン中 (%.0fs 残り)",
                    asset,
                    (self.cooldown_ms - (now - last)) / 1000,
                )
                return None
            self._last_entry_ms[asset] = now

        self._state[asset] = target
        self.emitted += 1
        signal = MajorSignal(
            asset=asset,
            direction=target,
            previous=current,
            score_usd=net,
            wallets=wallets,
            votes=count,
            created_at_ms=now,
            reference_price=self._last_price.get(asset, 0.0),
            contributors=tuple(sorted(aligned)[:5]),
        )
        log.info("%s", signal.describe())
        return signal

    # ------------------------------------------------------------------
    # 参照用
    # ------------------------------------------------------------------
    def state(self, asset: str) -> str:
        return self._state.get(asset, FLAT)

    def snapshot(self, now: int | None = None) -> dict[str, dict[str, float | str | int]]:
        """資産ごとの現在のネット金額と状態。ログ/デバッグ用。"""
        now = now or now_ms()
        self._prune(now)
        result: dict[str, dict[str, float | str | int]] = {}
        for asset in config.MAJOR_ASSETS:
            net, aligned, count = self._aggregate(asset)
            result[asset] = {
                "state": self._state.get(asset, FLAT),
                "net_usd": round(net, 2),
                "wallets": len(aligned),
                "votes": count,
            }
        return result

    def describe_state(self) -> str:
        parts = []
        for asset, row in self.snapshot().items():
            parts.append(f"{asset}:{row['state']}({float(row['net_usd']):+,.0f}/{row['wallets']}w)")
        return " ".join(parts)

    def reset(self) -> None:
        self._votes.clear()
        self._state = {asset: FLAT for asset in config.MAJOR_ASSETS}
        self._last_entry_ms.clear()
