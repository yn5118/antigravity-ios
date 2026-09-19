"""デモ（ペーパー）トレード実行と記録。

:class:`monitor.SwapEvent` を受け取り、仮想口座で建玉を開閉して SQLite
(``trades.db``) に記録する。記録内容:

    account      口座残高・累計損益・手数料
    positions    銘柄ごとの建玉（数量 / 平均取得単価 / 相手の保有数量）
    trades       約定・見送りの全履歴（タイムラグ、価格、PnL 付き）
    equity_curve 時価評価の推移
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:  # パッケージ / 単体の両対応
    from . import config
    from .monitor import BUY, SELL, PriceFeed, SwapEvent, now_ms
except ImportError:  # pragma: no cover
    import config
    from monitor import BUY, SELL, PriceFeed, SwapEvent, now_ms

log = logging.getLogger("copytrade.trader")

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    initial_usd     REAL NOT NULL,
    cash_usd        REAL NOT NULL,
    realized_pnl    REAL NOT NULL DEFAULT 0,
    fees_paid       REAL NOT NULL DEFAULT 0,
    created_at_ms   INTEGER NOT NULL,
    updated_at_ms   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chain           TEXT NOT NULL,
    wallet          TEXT NOT NULL,
    wallet_label    TEXT NOT NULL,
    token_address   TEXT NOT NULL,
    token_symbol    TEXT NOT NULL,
    qty             REAL NOT NULL DEFAULT 0,
    avg_price_usd   REAL NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0,
    source_qty      REAL NOT NULL DEFAULT 0,
    realized_pnl    REAL NOT NULL DEFAULT 0,
    last_price_usd  REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'OPEN',
    opened_at_ms    INTEGER NOT NULL,
    updated_at_ms   INTEGER NOT NULL,
    UNIQUE (chain, wallet, token_address)
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chain           TEXT NOT NULL,
    wallet          TEXT NOT NULL,
    wallet_label    TEXT NOT NULL,
    token_address   TEXT NOT NULL,
    token_symbol    TEXT NOT NULL,
    side            TEXT NOT NULL,
    status          TEXT NOT NULL,              -- FILLED / SKIPPED
    source_tx       TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_usd      REAL NOT NULL DEFAULT 0,    -- 監視ウォレット側の取引金額
    source_price    REAL NOT NULL DEFAULT 0,
    qty             REAL NOT NULL DEFAULT 0,
    fill_price_usd  REAL NOT NULL DEFAULT 0,
    notional_usd    REAL NOT NULL DEFAULT 0,
    fee_usd         REAL NOT NULL DEFAULT 0,
    slippage_usd    REAL NOT NULL DEFAULT 0,
    realized_pnl    REAL NOT NULL DEFAULT 0,
    cash_after      REAL NOT NULL DEFAULT 0,
    block_time_ms   INTEGER NOT NULL DEFAULT 0,
    detected_at_ms  INTEGER NOT NULL DEFAULT 0,
    filled_at_ms    INTEGER NOT NULL DEFAULT 0,
    detect_lag_ms   INTEGER NOT NULL DEFAULT 0, -- ブロック確定 -> 検知
    fill_lag_ms     INTEGER NOT NULL DEFAULT 0, -- 検知 -> 仮想約定
    total_lag_ms    INTEGER NOT NULL DEFAULT 0, -- ブロック確定 -> 仮想約定
    note            TEXT NOT NULL DEFAULT '',
    UNIQUE (source_tx, token_address, side, status)
);

CREATE TABLE IF NOT EXISTS equity_curve (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    cash_usd        REAL NOT NULL,
    positions_usd   REAL NOT NULL,
    equity_usd      REAL NOT NULL,
    realized_pnl    REAL NOT NULL,
    unrealized_pnl  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_token ON trades (token_address);
CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades (wallet);
CREATE INDEX IF NOT EXISTS idx_trades_filled_at ON trades (filled_at_ms);
"""


@dataclass(slots=True)
class Position:
    chain: str
    wallet: str
    wallet_label: str
    token_address: str
    token_symbol: str
    qty: float = 0.0
    avg_price_usd: float = 0.0
    cost_usd: float = 0.0
    source_qty: float = 0.0
    realized_pnl: float = 0.0
    last_price_usd: float = 0.0
    opened_at_ms: int = 0
    updated_at_ms: int = 0

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.chain, self.wallet, self.token_address)

    @property
    def is_open(self) -> bool:
        return self.qty > 1e-12

    def market_value(self) -> float:
        price = self.last_price_usd or self.avg_price_usd
        return self.qty * price

    def unrealized_pnl(self) -> float:
        return self.market_value() - self.cost_usd


@dataclass(slots=True)
class Fill:
    """仮想約定 1 件の結果。"""

    event: SwapEvent
    status: str
    side: str
    qty: float = 0.0
    fill_price_usd: float = 0.0
    notional_usd: float = 0.0
    fee_usd: float = 0.0
    slippage_usd: float = 0.0
    realized_pnl: float = 0.0
    cash_after: float = 0.0
    filled_at_ms: int = 0
    note: str = ""

    @property
    def fill_lag_ms(self) -> int:
        return max(0, self.filled_at_ms - self.event.detected_at_ms)

    @property
    def total_lag_ms(self) -> int:
        if not self.event.block_time_ms:
            return self.fill_lag_ms
        return max(0, self.filled_at_ms - self.event.block_time_ms)

    def describe(self) -> str:
        if self.status != "FILLED":
            return (
                f"見送り {self.side} {self.event.token_symbol} "
                f"({self.note}) tx={self.event.tx_hash[:12]}.."
            )
        pnl = f" pnl={self.realized_pnl:+,.2f}" if self.side == SELL else ""
        return (
            f"約定 {self.side} {self.event.token_symbol} qty={self.qty:,.4f} "
            f"@{self.fill_price_usd:.8g} notional={self.notional_usd:,.2f} "
            f"fee={self.fee_usd:,.2f}{pnl} cash={self.cash_after:,.2f} "
            f"lag(detect/fill/total)={self.event.detect_lag_ms}/{self.fill_lag_ms}/{self.total_lag_ms}ms"
        )


class DemoTrader:
    """仮想口座でコピートレードを実行し、結果を SQLite に記録する。"""

    def __init__(
        self,
        db_path: Path | str = config.DB_PATH,
        price_feed: PriceFeed | None = None,
        initial_usd: float = config.DEMO_INITIAL_BALANCE_USD,
    ) -> None:
        self.db_path = Path(db_path)
        self.price_feed = price_feed
        self.initial_usd = initial_usd
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self.cash_usd = initial_usd
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.positions: dict[tuple[str, str, str], Position] = {}
        self.processed = 0
        self.filled = 0
        self.skipped = 0

    # ------------------------------------------------------------------
    # 初期化 / 終了
    # ------------------------------------------------------------------
    async def start(self, reset: bool = False) -> None:
        await asyncio.to_thread(self._open_db, reset)
        log.info(
            "デモ口座を読み込み: cash=%.2f USD / 建玉 %d 件 / 実現損益 %+.2f USD",
            self.cash_usd,
            sum(1 for p in self.positions.values() if p.is_open),
            self.realized_pnl,
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._close_db)

    def _open_db(self, reset: bool) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        if reset:
            conn.executescript(
                "DELETE FROM trades; DELETE FROM positions;"
                " DELETE FROM equity_curve; DELETE FROM account;"
            )
        ts = now_ms()
        conn.execute(
            "INSERT OR IGNORE INTO account"
            " (id, initial_usd, cash_usd, realized_pnl, fees_paid, created_at_ms, updated_at_ms)"
            " VALUES (1, ?, ?, 0, 0, ?, ?)",
            (self.initial_usd, self.initial_usd, ts, ts),
        )
        conn.commit()
        self._conn = conn

        row = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        self.initial_usd = float(row["initial_usd"])
        self.cash_usd = float(row["cash_usd"])
        self.realized_pnl = float(row["realized_pnl"])
        self.fees_paid = float(row["fees_paid"])

        self.positions = {}
        for row in conn.execute("SELECT * FROM positions"):
            position = Position(
                chain=row["chain"],
                wallet=row["wallet"],
                wallet_label=row["wallet_label"],
                token_address=row["token_address"],
                token_symbol=row["token_symbol"],
                qty=float(row["qty"]),
                avg_price_usd=float(row["avg_price_usd"]),
                cost_usd=float(row["cost_usd"]),
                source_qty=float(row["source_qty"]),
                realized_pnl=float(row["realized_pnl"]),
                last_price_usd=float(row["last_price_usd"]),
                opened_at_ms=int(row["opened_at_ms"]),
                updated_at_ms=int(row["updated_at_ms"]),
            )
            self.positions[position.key] = position

    def _close_db(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("DemoTrader.start() を先に呼んでください")
        return self._conn

    # ------------------------------------------------------------------
    # 価格
    # ------------------------------------------------------------------
    async def _mark_price(self, event_chain: str, token_address: str, fallback: float) -> tuple[float, float]:
        """(価格, 流動性) を返す。取得できなければ fallback を使う。"""
        if self.price_feed is None:
            return fallback, float("inf")
        quote = await self.price_feed.get(event_chain, token_address)
        if quote is None or quote.price_usd <= 0:
            return fallback, float("inf")
        return quote.price_usd, quote.liquidity_usd

    # ------------------------------------------------------------------
    # 発注サイズ
    # ------------------------------------------------------------------
    def _target_usd(self, event: SwapEvent) -> float:
        if config.COPY_MODE == "ratio":
            size = event.usd_amount * config.COPY_RATIO
        else:
            size = config.COPY_FIXED_USD
        return max(0.0, size * (event.weight or 1.0))

    # ------------------------------------------------------------------
    # イベント処理
    # ------------------------------------------------------------------
    async def handle_event(self, event: SwapEvent) -> Fill:
        async with self._lock:
            self.processed += 1
            fill = await self._execute(event)
            if fill.status == "FILLED":
                self.filled += 1
                log.info("%s | %s", fill.describe(), f"src={event.wallet_label}")
            else:
                self.skipped += 1
                log.info("%s", fill.describe())
            await asyncio.to_thread(self._persist_fill, fill)
            return fill

    async def _execute(self, event: SwapEvent) -> Fill:
        filled_at = now_ms()

        if event.usd_amount < config.MIN_SIGNAL_USD:
            return self._skip(event, filled_at, f"シグナルが小さい ({event.usd_amount:,.2f} USD)")

        market_price, liquidity = await self._mark_price(
            event.chain, event.token_address, event.price_usd
        )
        if market_price <= 0:
            return self._skip(event, filled_at, "価格取得不可")
        if liquidity < config.MIN_LIQUIDITY_USD:
            return self._skip(event, filled_at, f"流動性不足 ({liquidity:,.0f} USD)")

        slip = config.SLIPPAGE_BPS / 10_000.0
        fee_rate = config.FEE_BPS / 10_000.0
        key = (event.chain, event.wallet, event.token_address)
        position = self.positions.get(key)
        filled_at = now_ms()

        if event.side == BUY:
            return self._buy(event, position, key, market_price, slip, fee_rate, filled_at)
        return self._sell(event, position, market_price, slip, fee_rate, filled_at)

    # -- 買い ----------------------------------------------------------
    def _buy(
        self,
        event: SwapEvent,
        position: Position | None,
        key: tuple[str, str, str],
        market_price: float,
        slip: float,
        fee_rate: float,
        filled_at: int,
    ) -> Fill:
        open_count = sum(1 for p in self.positions.values() if p.is_open)
        if (position is None or not position.is_open) and open_count >= config.MAX_OPEN_POSITIONS:
            return self._skip(event, filled_at, f"建玉数上限 ({open_count})")

        target = self._target_usd(event)
        held = position.cost_usd if position else 0.0
        room = max(0.0, config.MAX_POSITION_USD - held)
        notional = min(target, room, self.cash_usd / (1 + fee_rate))

        if notional < config.MIN_TRADE_USD:
            reason = "資金不足" if self.cash_usd < config.MIN_TRADE_USD else "サイズ上限"
            return self._skip(event, filled_at, f"{reason} (発注可能 {notional:,.2f} USD)")

        fill_price = market_price * (1 + slip)
        qty = notional / fill_price
        fee = notional * fee_rate
        self.cash_usd -= notional + fee
        self.fees_paid += fee

        if position is None:
            position = Position(
                chain=event.chain,
                wallet=event.wallet,
                wallet_label=event.wallet_label,
                token_address=event.token_address,
                token_symbol=event.token_symbol,
                opened_at_ms=filled_at,
            )
            self.positions[key] = position
        if not position.is_open:
            position.opened_at_ms = filled_at
            position.source_qty = 0.0

        position.qty += qty
        position.cost_usd += notional
        position.avg_price_usd = position.cost_usd / position.qty if position.qty else 0.0
        position.source_qty += event.token_amount
        position.last_price_usd = market_price
        position.token_symbol = event.token_symbol or position.token_symbol
        position.updated_at_ms = filled_at

        return Fill(
            event=event,
            status="FILLED",
            side=BUY,
            qty=qty,
            fill_price_usd=fill_price,
            notional_usd=notional,
            fee_usd=fee,
            slippage_usd=notional * slip / (1 + slip),
            cash_after=self.cash_usd,
            filled_at_ms=filled_at,
        )

    # -- 売り ----------------------------------------------------------
    def _sell(
        self,
        event: SwapEvent,
        position: Position | None,
        market_price: float,
        slip: float,
        fee_rate: float,
        filled_at: int,
    ) -> Fill:
        if position is None or not position.is_open:
            return self._skip(event, filled_at, "対応する建玉なし")

        fraction = 1.0
        if config.SELL_MODE == "proportional" and position.source_qty > 0:
            fraction = min(1.0, event.token_amount / position.source_qty)
        qty = position.qty * fraction
        if qty <= 0:
            return self._skip(event, filled_at, "決済数量が 0")

        remaining_value = (position.qty - qty) * market_price
        if 0 < remaining_value < config.MIN_TRADE_USD:
            qty = position.qty  # 端玉が残らないよう全決済に丸める
            fraction = 1.0

        fill_price = market_price * (1 - slip)
        proceeds = qty * fill_price
        fee = proceeds * fee_rate
        cost_part = position.avg_price_usd * qty
        realized = proceeds - fee - cost_part

        self.cash_usd += proceeds - fee
        self.fees_paid += fee
        self.realized_pnl += realized

        position.qty -= qty
        position.cost_usd = max(0.0, position.cost_usd - cost_part)
        position.source_qty = max(0.0, position.source_qty - event.token_amount)
        position.realized_pnl += realized
        position.last_price_usd = market_price
        position.updated_at_ms = filled_at
        if not position.is_open:
            position.qty = 0.0
            position.cost_usd = 0.0
            position.avg_price_usd = 0.0
            position.source_qty = 0.0

        return Fill(
            event=event,
            status="FILLED",
            side=SELL,
            qty=qty,
            fill_price_usd=fill_price,
            notional_usd=proceeds,
            fee_usd=fee,
            slippage_usd=proceeds * slip / (1 - slip) if slip < 1 else 0.0,
            realized_pnl=realized,
            cash_after=self.cash_usd,
            filled_at_ms=filled_at,
            note=f"決済割合 {fraction:.0%}",
        )

    def _skip(self, event: SwapEvent, filled_at: int, note: str) -> Fill:
        return Fill(
            event=event,
            status="SKIPPED",
            side=event.side,
            cash_after=self.cash_usd,
            filled_at_ms=filled_at,
            note=note,
        )

    # ------------------------------------------------------------------
    # 永続化
    # ------------------------------------------------------------------
    def _persist_fill(self, fill: Fill) -> None:
        event = fill.event
        conn = self.conn
        ts = now_ms()
        conn.execute(
            "INSERT OR IGNORE INTO trades ("
            " chain, wallet, wallet_label, token_address, token_symbol, side, status,"
            " source_tx, source, source_usd, source_price, qty, fill_price_usd, notional_usd,"
            " fee_usd, slippage_usd, realized_pnl, cash_after, block_time_ms, detected_at_ms,"
            " filled_at_ms, detect_lag_ms, fill_lag_ms, total_lag_ms, note"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.chain,
                event.wallet,
                event.wallet_label,
                event.token_address,
                event.token_symbol,
                fill.side,
                fill.status,
                event.tx_hash,
                event.source,
                event.usd_amount,
                event.price_usd,
                fill.qty,
                fill.fill_price_usd,
                fill.notional_usd,
                fill.fee_usd,
                fill.slippage_usd,
                fill.realized_pnl,
                fill.cash_after,
                event.block_time_ms,
                event.detected_at_ms,
                fill.filled_at_ms,
                event.detect_lag_ms,
                fill.fill_lag_ms,
                fill.total_lag_ms,
                fill.note,
            ),
        )
        if fill.status == "FILLED":
            position = self.positions.get((event.chain, event.wallet, event.token_address))
            if position is not None:
                self._upsert_position(conn, position, ts)
            self._save_account(conn, ts)
        conn.commit()

    @staticmethod
    def _upsert_position(conn: sqlite3.Connection, position: Position, ts: int) -> None:
        conn.execute(
            "INSERT INTO positions ("
            " chain, wallet, wallet_label, token_address, token_symbol, qty, avg_price_usd,"
            " cost_usd, source_qty, realized_pnl, last_price_usd, status, opened_at_ms, updated_at_ms"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (chain, wallet, token_address) DO UPDATE SET"
            " token_symbol=excluded.token_symbol, qty=excluded.qty,"
            " avg_price_usd=excluded.avg_price_usd, cost_usd=excluded.cost_usd,"
            " source_qty=excluded.source_qty, realized_pnl=excluded.realized_pnl,"
            " last_price_usd=excluded.last_price_usd, status=excluded.status,"
            " updated_at_ms=excluded.updated_at_ms",
            (
                position.chain,
                position.wallet,
                position.wallet_label,
                position.token_address,
                position.token_symbol,
                position.qty,
                position.avg_price_usd,
                position.cost_usd,
                position.source_qty,
                position.realized_pnl,
                position.last_price_usd,
                "OPEN" if position.is_open else "CLOSED",
                position.opened_at_ms or ts,
                position.updated_at_ms or ts,
            ),
        )

    def _save_account(self, conn: sqlite3.Connection, ts: int) -> None:
        conn.execute(
            "UPDATE account SET cash_usd=?, realized_pnl=?, fees_paid=?, updated_at_ms=? WHERE id=1",
            (self.cash_usd, self.realized_pnl, self.fees_paid, ts),
        )

    # ------------------------------------------------------------------
    # 時価評価
    # ------------------------------------------------------------------
    async def mark_to_market(self, record: bool = True) -> dict[str, float]:
        """建玉を最新価格で評価し、エクイティを記録して返す。"""
        async with self._lock:
            open_positions = [p for p in self.positions.values() if p.is_open]
            for position in open_positions:
                price, _ = await self._mark_price(
                    position.chain, position.token_address, position.last_price_usd or position.avg_price_usd
                )
                position.last_price_usd = price
                position.updated_at_ms = now_ms()

            positions_usd = sum(p.market_value() for p in open_positions)
            unrealized = sum(p.unrealized_pnl() for p in open_positions)
            snapshot = {
                "cash_usd": self.cash_usd,
                "positions_usd": positions_usd,
                "equity_usd": self.cash_usd + positions_usd,
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": unrealized,
                "fees_paid": self.fees_paid,
                "open_positions": float(len(open_positions)),
                "return_pct": (
                    (self.cash_usd + positions_usd - self.initial_usd) / self.initial_usd * 100
                    if self.initial_usd
                    else 0.0
                ),
            }
            if record:
                await asyncio.to_thread(self._persist_snapshot, open_positions, snapshot)
            return snapshot

    def _persist_snapshot(self, positions: Iterable[Position], snapshot: dict[str, float]) -> None:
        conn = self.conn
        ts = now_ms()
        for position in positions:
            self._upsert_position(conn, position, ts)
        conn.execute(
            "INSERT INTO equity_curve (ts_ms, cash_usd, positions_usd, equity_usd, realized_pnl, unrealized_pnl)"
            " VALUES (?,?,?,?,?,?)",
            (
                ts,
                snapshot["cash_usd"],
                snapshot["positions_usd"],
                snapshot["equity_usd"],
                snapshot["realized_pnl"],
                snapshot["unrealized_pnl"],
            ),
        )
        self._save_account(conn, ts)
        conn.commit()

    # ------------------------------------------------------------------
    # レポート
    # ------------------------------------------------------------------
    async def stats(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._stats_sync)

    def _stats_sync(self) -> dict[str, Any]:
        conn = self.conn
        row = conn.execute(
            "SELECT COUNT(*) AS n,"
            " SUM(status='FILLED') AS filled,"
            " SUM(status='SKIPPED') AS skipped,"
            " AVG(CASE WHEN status='FILLED' THEN detect_lag_ms END) AS avg_detect_lag,"
            " AVG(CASE WHEN status='FILLED' THEN total_lag_ms END) AS avg_total_lag,"
            " MAX(CASE WHEN status='FILLED' THEN total_lag_ms END) AS max_total_lag"
            " FROM trades"
        ).fetchone()
        closes = conn.execute(
            "SELECT COUNT(*) AS n, SUM(realized_pnl > 0) AS wins,"
            " SUM(realized_pnl) AS total, AVG(realized_pnl) AS avg,"
            " MAX(realized_pnl) AS best, MIN(realized_pnl) AS worst"
            " FROM trades WHERE status='FILLED' AND side='SELL'"
        ).fetchone()
        per_wallet = conn.execute(
            "SELECT wallet_label,"
            " SUM(status='FILLED') AS fills,"
            " SUM(CASE WHEN side='SELL' THEN realized_pnl ELSE 0 END) AS pnl,"
            " AVG(CASE WHEN status='FILLED' THEN total_lag_ms END) AS avg_lag"
            " FROM trades GROUP BY wallet_label ORDER BY pnl DESC"
        ).fetchall()

        closed = int(closes["n"] or 0)
        wins = int(closes["wins"] or 0)
        return {
            "trades": int(row["n"] or 0),
            "filled": int(row["filled"] or 0),
            "skipped": int(row["skipped"] or 0),
            "avg_detect_lag_ms": float(row["avg_detect_lag"] or 0.0),
            "avg_total_lag_ms": float(row["avg_total_lag"] or 0.0),
            "max_total_lag_ms": float(row["max_total_lag"] or 0.0),
            "closed_trades": closed,
            "wins": wins,
            "win_rate_pct": (wins / closed * 100) if closed else 0.0,
            "realized_total": float(closes["total"] or 0.0),
            "realized_avg": float(closes["avg"] or 0.0),
            "best_trade": float(closes["best"] or 0.0),
            "worst_trade": float(closes["worst"] or 0.0),
            "per_wallet": [dict(r) for r in per_wallet],
        }

    async def report(self) -> str:
        snapshot = await self.mark_to_market(record=False)
        stats = await self.stats()
        lines = [
            "",
            "================ デモトレード成績 ================",
            f"初期資金       : {self.initial_usd:>12,.2f} USD",
            f"現金           : {snapshot['cash_usd']:>12,.2f} USD",
            f"建玉評価額     : {snapshot['positions_usd']:>12,.2f} USD ({int(snapshot['open_positions'])} 銘柄)",
            f"総資産         : {snapshot['equity_usd']:>12,.2f} USD ({snapshot['return_pct']:+.2f}%)",
            f"実現損益       : {snapshot['realized_pnl']:>12,.2f} USD",
            f"含み損益       : {snapshot['unrealized_pnl']:>12,.2f} USD",
            f"支払手数料     : {snapshot['fees_paid']:>12,.2f} USD",
            "-------------------------------------------------",
            f"検知イベント   : {stats['trades']} (約定 {stats['filled']} / 見送り {stats['skipped']})",
            f"決済回数       : {stats['closed_trades']} / 勝率 {stats['win_rate_pct']:.1f}%",
            f"平均/最高/最悪 : {stats['realized_avg']:+,.2f} / {stats['best_trade']:+,.2f} / {stats['worst_trade']:+,.2f} USD",
            f"平均タイムラグ : 検知 {stats['avg_detect_lag_ms']:,.0f}ms / 約定まで {stats['avg_total_lag_ms']:,.0f}ms"
            f" (最大 {stats['max_total_lag_ms']:,.0f}ms)",
        ]
        if stats["per_wallet"]:
            lines.append("--- ウォレット別 -------------------------------")
            for row in stats["per_wallet"]:
                lines.append(
                    f"  {str(row['wallet_label'])[:18]:<18} 約定 {int(row['fills'] or 0):>3}"
                    f"  実現損益 {float(row['pnl'] or 0.0):+10,.2f} USD"
                    f"  平均ラグ {float(row['avg_lag'] or 0.0):>7,.0f}ms"
                )
        open_positions = [p for p in self.positions.values() if p.is_open]
        if open_positions:
            lines.append("--- 保有建玉 -----------------------------------")
            for position in sorted(open_positions, key=lambda p: -p.market_value()):
                lines.append(
                    f"  {position.token_symbol[:10]:<10} qty={position.qty:>14,.4f}"
                    f"  平均={position.avg_price_usd:.8g}  時価={position.market_value():>10,.2f} USD"
                    f"  含み={position.unrealized_pnl():+10,.2f} USD  [{position.wallet_label}]"
                )
        lines.append("=================================================")
        return "\n".join(lines)
