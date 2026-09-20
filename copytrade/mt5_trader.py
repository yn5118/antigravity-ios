"""MT5（MetaTrader5）へのシグナル執行レイヤ。

:class:`signals.MajorSignal` を受け取り、BTCUSD / ETHUSD / SOLUSD といった
ブローカーのメジャー銘柄に成行で発注する。ブローカー接続は :class:`Broker`
プロトコルで抽象化してあり、

    :class:`Mt5Broker`     公式 MetaTrader5 Python パッケージ（Windows 専用）
    :class:`DryRunBroker`  接続せずに約定を模擬する（Linux / CI でも動く）

安全側の既定値として、実口座では ``MT5_ALLOW_LIVE=1`` を明示しない限り発注しない。
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

try:  # パッケージ / 単体の両対応
    from . import config
    from .monitor import now_ms
    from .signals import FLAT, LONG, SHORT, MajorSignal
except ImportError:  # pragma: no cover
    import config
    from monitor import now_ms
    from signals import FLAT, LONG, SHORT, MajorSignal

log = logging.getLogger("copytrade.mt5")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mt5_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    asset           TEXT NOT NULL,
    symbol          TEXT NOT NULL DEFAULT '',
    action          TEXT NOT NULL,              -- OPEN / CLOSE / SKIP / ERROR
    direction       TEXT NOT NULL DEFAULT '',
    volume          REAL NOT NULL DEFAULT 0,
    price           REAL NOT NULL DEFAULT 0,
    sl              REAL NOT NULL DEFAULT 0,
    tp              REAL NOT NULL DEFAULT 0,
    ticket          INTEGER NOT NULL DEFAULT 0,
    retcode         INTEGER NOT NULL DEFAULT 0,
    profit          REAL NOT NULL DEFAULT 0,
    signal_score    REAL NOT NULL DEFAULT 0,
    signal_wallets  INTEGER NOT NULL DEFAULT 0,
    dry_run         INTEGER NOT NULL DEFAULT 1,
    note            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mt5_orders_ts ON mt5_orders (ts_ms);
"""


# --------------------------------------------------------------------------
# データ構造
# --------------------------------------------------------------------------
@dataclass(slots=True)
class AccountSnapshot:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    is_demo: bool
    name: str = ""

    def describe(self) -> str:
        kind = "デモ" if self.is_demo else "★実口座★"
        return (
            f"{kind} #{self.login} @{self.server} | 残高 {self.balance:,.2f} {self.currency}"
            f" / 有効証拠金 {self.equity:,.2f}"
        )


@dataclass(slots=True)
class SymbolSpec:
    asset: str
    name: str
    digits: int = 2
    point: float = 0.01
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    tick_value: float = 1.0
    tick_size: float = 0.01
    filling_modes: tuple[int, ...] = ()


@dataclass(slots=True)
class PositionInfo:
    ticket: int
    symbol: str
    direction: str
    volume: float
    price_open: float
    sl: float = 0.0
    tp: float = 0.0
    profit: float = 0.0
    magic: int = 0


@dataclass(slots=True)
class OrderResult:
    ok: bool
    retcode: int = 0
    comment: str = ""
    price: float = 0.0
    volume: float = 0.0
    ticket: int = 0
    profit: float = 0.0


class Broker(Protocol):
    """MT5 接続の最小インターフェース（同期。呼び出し側で to_thread する）。"""

    dry_run: bool

    def connect(self) -> AccountSnapshot: ...
    def resolve_symbol(self, asset: str, candidates: list[str]) -> SymbolSpec | None: ...
    def tick(self, spec: SymbolSpec) -> tuple[float, float]: ...
    def positions(self, spec: SymbolSpec) -> list[PositionInfo]: ...
    def open(
        self, spec: SymbolSpec, direction: str, volume: float, sl: float, tp: float, comment: str
    ) -> OrderResult: ...
    def close(self, spec: SymbolSpec, position: PositionInfo, comment: str) -> OrderResult: ...
    def shutdown(self) -> None: ...


# --------------------------------------------------------------------------
# 実 MT5 接続
# --------------------------------------------------------------------------
class Mt5Broker:
    """公式 MetaTrader5 パッケージ経由の接続（Windows 専用）。"""

    dry_run = False

    def __init__(self) -> None:
        try:
            import MetaTrader5 as mt5  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - Windows 以外では入らない
            raise RuntimeError(
                "MetaTrader5 パッケージが見つかりません。Windows で "
                "`pip install MetaTrader5` を実行してください（--mt5-dry-run なら不要）"
            ) from exc
        self._mt5 = mt5

    # -- 接続 ----------------------------------------------------------
    def connect(self) -> AccountSnapshot:
        mt5 = self._mt5
        kwargs: dict[str, object] = {}
        if config.MT5_TERMINAL_PATH:
            kwargs["path"] = config.MT5_TERMINAL_PATH
        if config.MT5_LOGIN:
            kwargs.update(login=config.MT5_LOGIN, password=config.MT5_PASSWORD, server=config.MT5_SERVER)

        if not mt5.initialize(**kwargs):
            raise RuntimeError(f"MT5 初期化に失敗: {mt5.last_error()}")

        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"口座情報を取得できません: {mt5.last_error()}")
        # trade_mode: 0=デモ, 1=コンテスト, 2=実口座
        return AccountSnapshot(
            login=int(info.login),
            server=str(info.server),
            currency=str(info.currency),
            balance=float(info.balance),
            equity=float(info.equity),
            is_demo=int(info.trade_mode) != 2,
            name=str(info.name),
        )

    def shutdown(self) -> None:
        try:
            self._mt5.shutdown()
        except Exception:  # noqa: BLE001 - 終了処理で落とさない
            log.debug("MT5 shutdown でエラー", exc_info=True)

    # -- 銘柄 ----------------------------------------------------------
    def resolve_symbol(self, asset: str, candidates: list[str]) -> SymbolSpec | None:
        mt5 = self._mt5
        for name in candidates:
            info = mt5.symbol_info(name)
            if info is None:
                continue
            if not info.visible and not mt5.symbol_select(name, True):
                log.warning("%s は気配値表示に追加できませんでした", name)
                continue
            info = mt5.symbol_info(name) or info
            return SymbolSpec(
                asset=asset,
                name=name,
                digits=int(info.digits),
                point=float(info.point),
                volume_min=float(info.volume_min),
                volume_max=float(info.volume_max),
                volume_step=float(info.volume_step),
                tick_value=float(getattr(info, "trade_tick_value", 0.0) or 0.0),
                tick_size=float(getattr(info, "trade_tick_size", 0.0) or info.point),
                filling_modes=self._filling_modes(int(getattr(info, "filling_mode", 0))),
            )
        return None

    def _filling_modes(self, mask: int) -> tuple[int, ...]:
        mt5 = self._mt5
        modes: list[int] = []
        if mask & getattr(mt5, "SYMBOL_FILLING_FOK", 1):
            modes.append(mt5.ORDER_FILLING_FOK)
        if mask & getattr(mt5, "SYMBOL_FILLING_IOC", 2):
            modes.append(mt5.ORDER_FILLING_IOC)
        modes.append(mt5.ORDER_FILLING_RETURN)
        return tuple(dict.fromkeys(modes))

    # -- 相場 / 建玉 ----------------------------------------------------
    def tick(self, spec: SymbolSpec) -> tuple[float, float]:
        tick = self._mt5.symbol_info_tick(spec.name)
        if tick is None:
            raise RuntimeError(f"{spec.name} の気配を取得できません: {self._mt5.last_error()}")
        return float(tick.bid), float(tick.ask)

    def positions(self, spec: SymbolSpec) -> list[PositionInfo]:
        mt5 = self._mt5
        rows = mt5.positions_get(symbol=spec.name) or []
        result = []
        for row in rows:
            if config.MT5_MAGIC and int(row.magic) != config.MT5_MAGIC:
                continue  # 手動やほかの EA の建玉には触らない
            result.append(
                PositionInfo(
                    ticket=int(row.ticket),
                    symbol=str(row.symbol),
                    direction=LONG if int(row.type) == mt5.POSITION_TYPE_BUY else SHORT,
                    volume=float(row.volume),
                    price_open=float(row.price_open),
                    sl=float(row.sl),
                    tp=float(row.tp),
                    profit=float(row.profit),
                    magic=int(row.magic),
                )
            )
        return result

    # -- 発注 ----------------------------------------------------------
    def open(
        self, spec: SymbolSpec, direction: str, volume: float, sl: float, tp: float, comment: str
    ) -> OrderResult:
        mt5 = self._mt5
        bid, ask = self.tick(spec)
        price = ask if direction == LONG else bid
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "volume": volume,
            "type": mt5.ORDER_TYPE_BUY if direction == LONG else mt5.ORDER_TYPE_SELL,
            "price": price,
            "deviation": config.MT5_DEVIATION,
            "magic": config.MT5_MAGIC,
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }
        if sl:
            request["sl"] = round(sl, spec.digits)
        if tp:
            request["tp"] = round(tp, spec.digits)
        return self._send(request, spec)

    def close(self, spec: SymbolSpec, position: PositionInfo, comment: str) -> OrderResult:
        mt5 = self._mt5
        bid, ask = self.tick(spec)
        price = bid if position.direction == LONG else ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "volume": position.volume,
            "type": mt5.ORDER_TYPE_SELL if position.direction == LONG else mt5.ORDER_TYPE_BUY,
            "position": position.ticket,
            "price": price,
            "deviation": config.MT5_DEVIATION,
            "magic": config.MT5_MAGIC,
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }
        result = self._send(request, spec)
        result.profit = position.profit
        return result

    def _send(self, request: dict[str, object], spec: SymbolSpec) -> OrderResult:
        """充填モードを順に試しながら発注する。"""
        mt5 = self._mt5
        modes = spec.filling_modes or (mt5.ORDER_FILLING_IOC,)
        last: OrderResult | None = None
        for mode in modes:
            request["type_filling"] = mode
            result = mt5.order_send(request)
            if result is None:
                last = OrderResult(ok=False, comment=f"order_send が None: {mt5.last_error()}")
                continue
            ok = int(result.retcode) == mt5.TRADE_RETCODE_DONE
            last = OrderResult(
                ok=ok,
                retcode=int(result.retcode),
                comment=str(result.comment),
                price=float(result.price),
                volume=float(result.volume),
                ticket=int(getattr(result, "order", 0) or 0),
            )
            if ok:
                return last
            if int(result.retcode) != getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030):
                return last  # 充填モード以外の理由なら再試行しない
        return last or OrderResult(ok=False, comment="発注できませんでした")


# --------------------------------------------------------------------------
# dry-run（MT5 に接続しない模擬ブローカー）
# --------------------------------------------------------------------------
class DryRunBroker:
    """MT5 に繋がずに執行を模擬する。ロジック確認と Windows 以外での検証用。"""

    dry_run = True

    def __init__(self, balance: float = 10_000.0, prices: dict[str, float] | None = None) -> None:
        self.balance = balance
        self.prices = dict(config.MT5_DRYRUN_PRICES)
        if prices:
            self.prices.update(prices)
        self._positions: dict[int, PositionInfo] = {}
        self._next_ticket = 1

    def set_price(self, asset: str, price: float) -> None:
        if price > 0:
            self.prices[asset] = price

    def connect(self) -> AccountSnapshot:
        return AccountSnapshot(
            login=0,
            server="dry-run",
            currency="USD",
            balance=self.balance,
            equity=self.balance,
            is_demo=True,
            name="dry-run",
        )

    def resolve_symbol(self, asset: str, candidates: list[str]) -> SymbolSpec | None:
        name = candidates[0] if candidates else f"{asset}USD"
        return SymbolSpec(
            asset=asset,
            name=name,
            digits=2,
            point=0.01,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            tick_value=1.0,
            tick_size=0.01,
        )

    def tick(self, spec: SymbolSpec) -> tuple[float, float]:
        price = self.prices.get(spec.asset, 100.0)
        spread = price * 0.0002
        return price - spread / 2, price + spread / 2

    def positions(self, spec: SymbolSpec) -> list[PositionInfo]:
        return [p for p in self._positions.values() if p.symbol == spec.name]

    def open(
        self, spec: SymbolSpec, direction: str, volume: float, sl: float, tp: float, comment: str
    ) -> OrderResult:
        bid, ask = self.tick(spec)
        price = ask if direction == LONG else bid
        ticket = self._next_ticket
        self._next_ticket += 1
        self._positions[ticket] = PositionInfo(
            ticket=ticket,
            symbol=spec.name,
            direction=direction,
            volume=volume,
            price_open=price,
            sl=sl,
            tp=tp,
            magic=config.MT5_MAGIC,
        )
        return OrderResult(ok=True, retcode=10009, comment="dry-run", price=price, volume=volume, ticket=ticket)

    def close(self, spec: SymbolSpec, position: PositionInfo, comment: str) -> OrderResult:
        bid, ask = self.tick(spec)
        price = bid if position.direction == LONG else ask
        sign = 1.0 if position.direction == LONG else -1.0
        profit = (price - position.price_open) * sign * position.volume
        self._positions.pop(position.ticket, None)
        self.balance += profit
        return OrderResult(
            ok=True, retcode=10009, comment="dry-run", price=price, volume=position.volume,
            ticket=position.ticket, profit=profit,
        )

    def shutdown(self) -> None:
        return None


# --------------------------------------------------------------------------
# 執行本体
# --------------------------------------------------------------------------
class Mt5Trader:
    """シグナルを MT5 のメジャー銘柄の建玉に変換する。"""

    def __init__(self, broker: Broker, db_path: Path | str = config.DB_PATH) -> None:
        self.broker = broker
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self.account: AccountSnapshot | None = None
        self.symbols: dict[str, SymbolSpec] = {}
        self.opened = 0
        self.closed = 0
        self.skipped = 0

    # ------------------------------------------------------------------
    async def start(self) -> None:
        await asyncio.to_thread(self._init_db)
        self.account = await asyncio.to_thread(self.broker.connect)
        log.info("MT5 接続: %s%s", self.account.describe(), " [dry-run]" if self.broker.dry_run else "")

        if not self.account.is_demo and not config.MT5_ALLOW_LIVE:
            raise RuntimeError(
                "実口座に接続されています。デモ口座で検証してください。"
                "どうしても実口座で動かす場合は MT5_ALLOW_LIVE=1 を明示してください"
            )
        if not self.account.is_demo:
            log.warning("★ 実口座での発注が有効になっています（MT5_ALLOW_LIVE=1）★")

        for asset in config.MAJOR_ASSETS:
            candidates = config.MT5_SYMBOL_CANDIDATES.get(asset, [])
            spec = await asyncio.to_thread(self.broker.resolve_symbol, asset, candidates)
            if spec is None:
                log.warning("%s に対応する銘柄が見つかりません（候補: %s）", asset, ", ".join(candidates))
                continue
            self.symbols[asset] = spec
            log.info("%s -> %s (最小 %.2f / ステップ %.2f ロット)", asset, spec.name, spec.volume_min, spec.volume_step)
        if not self.symbols:
            raise RuntimeError("MT5 側で使える銘柄が 1 つもありません。MT5_SYMBOL_BTC などで指定してください")

    async def close(self) -> None:
        await asyncio.to_thread(self.broker.shutdown)
        if self._conn is not None:
            await asyncio.to_thread(self._close_db)

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
        self._conn = conn

    def _close_db(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # ロット計算
    # ------------------------------------------------------------------
    def volume_for(self, spec: SymbolSpec, price: float) -> float:
        """設定に応じた発注ロットを返す（ステップに丸め、上下限でクランプ）。"""
        volume = config.MT5_FIXED_LOT
        if config.MT5_LOT_MODE == "risk" and config.MT5_SL_PCT > 0:
            balance = self.account.balance if self.account else 0.0
            risk = balance * config.MT5_RISK_PCT / 100.0
            sl_distance = price * config.MT5_SL_PCT / 100.0
            tick_size = spec.tick_size or spec.point or 0.01
            loss_per_lot = (sl_distance / tick_size) * (spec.tick_value or 0.0)
            if risk > 0 and loss_per_lot > 0:
                volume = risk / loss_per_lot
            else:
                log.debug("リスク計算に必要な値が無いため固定ロットを使用")

        step = spec.volume_step or 0.01
        volume = math.floor(volume / step) * step
        volume = min(volume, spec.volume_max, config.MT5_MAX_LOT)
        volume = max(volume, spec.volume_min)
        return round(volume, 8)

    def _sl_tp(self, direction: str, price: float) -> tuple[float, float]:
        sl = tp = 0.0
        if config.MT5_SL_PCT > 0:
            delta = price * config.MT5_SL_PCT / 100.0
            sl = price - delta if direction == LONG else price + delta
        if config.MT5_TP_PCT > 0:
            delta = price * config.MT5_TP_PCT / 100.0
            tp = price + delta if direction == LONG else price - delta
        return sl, tp

    # ------------------------------------------------------------------
    # シグナル処理
    # ------------------------------------------------------------------
    async def on_signal(self, signal: MajorSignal) -> None:
        async with self._lock:
            spec = self.symbols.get(signal.asset)
            if spec is None:
                self.skipped += 1
                await self._record(signal, "SKIP", note=f"{signal.asset} の銘柄が未解決")
                return

            if isinstance(self.broker, DryRunBroker) and signal.reference_price:
                self.broker.set_price(signal.asset, signal.reference_price)

            positions = await asyncio.to_thread(self.broker.positions, spec)
            same = [p for p in positions if p.direction == signal.direction]
            opposite = [p for p in positions if p.direction != signal.direction]

            # FLAT = 手仕舞い
            if signal.direction == FLAT:
                for position in positions:
                    await self._close_position(spec, position, signal, "signal flat")
                if not positions:
                    self.skipped += 1
                    await self._record(signal, "SKIP", note="決済対象なし")
                return

            if same:
                self.skipped += 1
                await self._record(signal, "SKIP", direction=signal.direction, note="同方向を保有中")
                return

            if opposite:
                if not config.MT5_CLOSE_ON_OPPOSITE:
                    self.skipped += 1
                    await self._record(signal, "SKIP", direction=signal.direction, note="反対建玉あり（ドテン無効）")
                    return
                for position in opposite:
                    await self._close_position(spec, position, signal, "reverse")

            total_open = 0
            for other in self.symbols.values():
                total_open += len(await asyncio.to_thread(self.broker.positions, other))
            if total_open >= config.MT5_MAX_POSITIONS:
                self.skipped += 1
                await self._record(signal, "SKIP", direction=signal.direction, note=f"建玉数上限 ({total_open})")
                return

            bid, ask = await asyncio.to_thread(self.broker.tick, spec)
            price = ask if signal.direction == LONG else bid
            volume = self.volume_for(spec, price)
            sl, tp = self._sl_tp(signal.direction, price)
            comment = f"copytrade {signal.asset} {signal.direction}"

            result = await asyncio.to_thread(
                self.broker.open, spec, signal.direction, volume, sl, tp, comment
            )
            if result.ok:
                self.opened += 1
                log.info(
                    "MT5 発注 %s %s %.2f ロット @%.*f SL=%.*f TP=%.*f (ticket=%d)%s",
                    spec.name, signal.direction, result.volume or volume,
                    spec.digits, result.price or price, spec.digits, sl, spec.digits, tp,
                    result.ticket, " [dry-run]" if self.broker.dry_run else "",
                )
                await self._record(
                    signal, "OPEN", direction=signal.direction, volume=result.volume or volume,
                    price=result.price or price, sl=sl, tp=tp, ticket=result.ticket,
                    retcode=result.retcode,
                )
            else:
                log.error("MT5 発注失敗 %s: retcode=%s %s", spec.name, result.retcode, result.comment)
                await self._record(
                    signal, "ERROR", direction=signal.direction, volume=volume, price=price,
                    retcode=result.retcode, note=result.comment,
                )

    async def _close_position(
        self, spec: SymbolSpec, position: PositionInfo, signal: MajorSignal, reason: str
    ) -> None:
        result = await asyncio.to_thread(self.broker.close, spec, position, f"copytrade {reason}")
        if result.ok:
            self.closed += 1
            log.info(
                "MT5 決済 %s %s %.2f ロット @%.*f 損益 %+.2f (ticket=%d)%s",
                spec.name, position.direction, position.volume, spec.digits,
                result.price, result.profit, position.ticket,
                " [dry-run]" if self.broker.dry_run else "",
            )
            await self._record(
                signal, "CLOSE", direction=position.direction, volume=position.volume,
                price=result.price, ticket=position.ticket, retcode=result.retcode,
                profit=result.profit, note=reason,
            )
        else:
            log.error("MT5 決済失敗 %s: retcode=%s %s", spec.name, result.retcode, result.comment)
            await self._record(
                signal, "ERROR", direction=position.direction, volume=position.volume,
                ticket=position.ticket, retcode=result.retcode, note=f"close: {result.comment}",
            )

    # ------------------------------------------------------------------
    async def _record(
        self,
        signal: MajorSignal,
        action: str,
        direction: str = "",
        volume: float = 0.0,
        price: float = 0.0,
        sl: float = 0.0,
        tp: float = 0.0,
        ticket: int = 0,
        retcode: int = 0,
        profit: float = 0.0,
        note: str = "",
    ) -> None:
        spec = self.symbols.get(signal.asset)
        await asyncio.to_thread(
            self._insert_row,
            (
                now_ms(), signal.asset, spec.name if spec else "", action, direction, volume,
                price, sl, tp, ticket, retcode, profit, signal.score_usd, signal.wallets,
                1 if self.broker.dry_run else 0, note,
            ),
        )

    def _insert_row(self, values: tuple) -> None:
        if self._conn is None:
            return
        self._conn.execute(
            "INSERT INTO mt5_orders (ts_ms, asset, symbol, action, direction, volume, price, sl, tp,"
            " ticket, retcode, profit, signal_score, signal_wallets, dry_run, note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    async def report(self) -> str:
        lines = ["", "================ MT5 執行状況 ================"]
        if self.account:
            lines.append(f"口座           : {self.account.describe()}")
        lines.append(f"発注/決済/見送り: {self.opened} / {self.closed} / {self.skipped}")
        for asset, spec in self.symbols.items():
            positions = await asyncio.to_thread(self.broker.positions, spec)
            if not positions:
                lines.append(f"  {asset:<4} {spec.name:<12} 建玉なし")
                continue
            for position in positions:
                lines.append(
                    f"  {asset:<4} {spec.name:<12} {position.direction} {position.volume:.2f}"
                    f" ロット @{position.price_open:.{spec.digits}f} 損益 {position.profit:+,.2f}"
                )
        lines.append("==============================================")
        return "\n".join(lines)
