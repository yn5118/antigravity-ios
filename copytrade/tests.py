"""copytrade の自己テスト（外部 API を使わない）。

    python -m unittest copytrade.tests      # もしくは
    python tests.py
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

try:  # パッケージ / 単体の両対応
    from . import config
    from . import monitor as monitor_mod
    from .demo_trader import DemoTrader
    from .monitor import BUY, SELL, PriceFeed, SwapEvent, classify_swap, now_ms
    from .mt5_trader import DryRunBroker, Mt5Trader
    from .signals import FLAT, LONG, SHORT, SignalEngine
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config
    import monitor as monitor_mod
    from demo_trader import DemoTrader
    from monitor import BUY, SELL, PriceFeed, SwapEvent, classify_swap, now_ms
    from mt5_trader import DryRunBroker, Mt5Trader
    from signals import FLAT, LONG, SHORT, SignalEngine

WALLET_SOL = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
WALLET_EVM = "0x28c6c06298d514db089934071355e5743bf21d60"
TOKEN_SOL = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
TOKEN_EVM = "0x6982508145454ce325ddbe47a25d4ec3d2311933"
USDC_ETH = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


class StubPriceFeed(PriceFeed):
    """DEXScreener を呼ばずに固定価格を返すテスト用フィード。"""

    def __init__(self, prices: dict[str, float], liquidity: float = 1e6) -> None:  # noqa: D107
        self._prices = {k.lower(): v for k, v in prices.items()}
        self._liquidity = liquidity
        self.requests = 0
        self.failures = 0

    async def get(self, chain: str, token_address: str):  # type: ignore[override]
        if token_address.startswith("native:"):
            token_address = config.NATIVE_PRICE_REFERENCE.get(chain, token_address)
        price = self._prices.get(token_address.lower())
        self.requests += 1
        if price is None:
            return None
        return monitor_mod.PriceQuote(
            price_usd=price, symbol="", liquidity_usd=self._liquidity, fetched_at_ms=now_ms()
        )

    async def usd_value(self, chain: str, token_address: str, symbol: str, amount: float) -> float:  # type: ignore[override]
        if config.is_stable(symbol):
            return amount
        quote = await self.get(chain, token_address)
        return amount * quote.price_usd if quote else 0.0


class ClassifySwapTest(unittest.TestCase):
    def test_buy_with_native_sol(self) -> None:
        leg = classify_swap(config.SOLANA, {TOKEN_SOL: (1000.0, "WEN"), config.NATIVE_SOL: (-1.5, "SOL")})
        assert leg is not None
        self.assertEqual(leg.side, BUY)
        self.assertEqual(leg.token_address, TOKEN_SOL)
        self.assertAlmostEqual(leg.token_amount, 1000.0)
        self.assertAlmostEqual(leg.quote_amount, 1.5)
        self.assertEqual(leg.quote_symbol, "SOL")

    def test_sell_into_usdc(self) -> None:
        leg = classify_swap(
            "ethereum", {TOKEN_EVM: (-5_000_000.0, "PEPE"), USDC_ETH: (48.0, "USDC")}
        )
        assert leg is not None
        self.assertEqual(leg.side, SELL)
        self.assertAlmostEqual(leg.quote_amount, 48.0)

    def test_quote_only_swap_is_ignored(self) -> None:
        # USDC -> SOL の両替は「銘柄の売買」ではない
        self.assertIsNone(
            classify_swap(
                config.SOLANA,
                {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": (-100.0, "USDC"),
                 config.NATIVE_SOL: (0.6, "SOL")},
            )
        )

    def test_gas_only_change_is_dust(self) -> None:
        self.assertIsNone(classify_swap(config.SOLANA, {config.NATIVE_SOL: (-0.000005, "SOL")}))


class SolanaParsingTest(unittest.TestCase):
    def _monitor(self, cls):
        wallet = config.WatchedWallet(WALLET_SOL, config.SOLANA, "test-sol")
        return cls(wallet, StubPriceFeed({config.WSOL_MINT: 150.0, TOKEN_SOL: 0.22}), session=None)

    def test_helius_deltas(self) -> None:
        mon = self._monitor(monitor_mod.HeliusSolanaMonitor)
        tx = {
            "signature": "sig1",
            "timestamp": 1_758_000_000,
            "tokenTransfers": [
                {"fromUserAccount": "POOL", "toUserAccount": WALLET_SOL, "mint": TOKEN_SOL, "tokenAmount": 1000.0}
            ],
            "nativeTransfers": [
                {"fromUserAccount": WALLET_SOL, "toUserAccount": "POOL", "amount": 2_000_000_000}
            ],
        }
        deltas = mon._deltas_from_tx(tx)
        self.assertAlmostEqual(deltas[TOKEN_SOL][0], 1000.0)
        self.assertAlmostEqual(deltas[config.NATIVE_SOL][0], -2.0)

        leg = classify_swap(config.SOLANA, deltas)
        event = asyncio.run(mon._build_event(leg, "sig1", 1_758_000_000_000, now_ms()))
        assert event is not None
        self.assertEqual(event.side, BUY)
        self.assertAlmostEqual(event.usd_amount, 300.0)  # 2 SOL * 150 USD
        self.assertAlmostEqual(event.price_usd, 0.3)

    def test_rpc_deltas(self) -> None:
        mon = self._monitor(monitor_mod.SolanaRpcMonitor)
        tx = {
            "blockTime": 1_758_000_100,
            "transaction": {"message": {"accountKeys": [{"pubkey": WALLET_SOL}, {"pubkey": "POOL"}]}},
            "meta": {
                "preBalances": [3_000_000_000, 0],
                "postBalances": [2_000_000_000, 0],
                "preTokenBalances": [
                    {"owner": WALLET_SOL, "mint": TOKEN_SOL, "uiTokenAmount": {"uiAmount": 100.0}}
                ],
                "postTokenBalances": [
                    {"owner": WALLET_SOL, "mint": TOKEN_SOL, "uiTokenAmount": {"uiAmount": 4_100.0}},
                    {"owner": "OTHER", "mint": TOKEN_SOL, "uiTokenAmount": {"uiAmount": 999.0}},
                ],
            },
        }
        deltas = mon._deltas_from_tx(tx)
        self.assertAlmostEqual(deltas[TOKEN_SOL][0], 4000.0)
        self.assertAlmostEqual(deltas[config.NATIVE_SOL][0], -1.0)
        leg = classify_swap(config.SOLANA, deltas)
        assert leg is not None
        self.assertEqual(leg.side, BUY)
        self.assertAlmostEqual(leg.quote_amount, 1.0)


class EvmParsingTest(unittest.TestCase):
    def _monitor(self):
        wallet = config.WatchedWallet(WALLET_EVM, "ethereum", "test-eth")
        return monitor_mod.EvmScanMonitor(
            wallet, StubPriceFeed({TOKEN_EVM: 0.00001}), session=None
        )

    def test_token_bought_with_usdc(self) -> None:
        mon = self._monitor()
        rows = [
            {
                "hash": "0xabc",
                "timeStamp": "1758000000",
                "from": "0xpool",
                "to": WALLET_EVM,
                "contractAddress": TOKEN_EVM,
                "value": str(5_000_000 * 10**18),
                "tokenDecimal": "18",
                "tokenSymbol": "PEPE",
            },
            {
                "hash": "0xabc",
                "timeStamp": "1758000000",
                "from": WALLET_EVM,
                "to": "0xpool",
                "contractAddress": USDC_ETH,
                "value": str(50 * 10**6),
                "tokenDecimal": "6",
                "tokenSymbol": "USDC",
            },
        ]
        deltas = mon._deltas_from_rows(rows)
        leg = classify_swap("ethereum", deltas)
        assert leg is not None
        self.assertEqual(leg.side, BUY)
        self.assertAlmostEqual(leg.token_amount, 5_000_000.0)
        event = asyncio.run(mon._build_event(leg, "0xabc", 1_758_000_000_000, now_ms()))
        assert event is not None
        self.assertAlmostEqual(event.usd_amount, 50.0)  # USDC は 1.0 換算
        self.assertEqual(event.token_symbol, "PEPE")

    def test_native_swap_without_quote_leg(self) -> None:
        """ETH で直接買った場合は銘柄価格から USD を逆算する。"""
        mon = self._monitor()
        rows = [
            {
                "hash": "0xdef",
                "timeStamp": "1758000000",
                "from": "0xpool",
                "to": WALLET_EVM,
                "contractAddress": TOKEN_EVM,
                "value": str(1_000_000 * 10**18),
                "tokenDecimal": "18",
                "tokenSymbol": "PEPE",
            }
        ]
        leg = classify_swap("ethereum", mon._deltas_from_rows(rows))
        assert leg is not None
        event = asyncio.run(mon._build_event(leg, "0xdef", 1_758_000_000_000, now_ms()))
        assert event is not None
        self.assertAlmostEqual(event.usd_amount, 10.0)  # 1,000,000 * 0.00001


def make_event(side: str, token_amount: float, usd: float, price: float, tx: str) -> SwapEvent:
    detected = now_ms()
    return SwapEvent(
        chain=config.SOLANA,
        wallet=WALLET_SOL,
        wallet_label="test",
        tx_hash=tx,
        side=side,
        token_address=TOKEN_SOL,
        token_symbol="WEN",
        token_amount=token_amount,
        usd_amount=usd,
        price_usd=price,
        block_time_ms=detected - 500,
        detected_at_ms=detected,
        source="test",
    )


class DemoTraderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "trades.db"
        # テスト中は設定を固定する
        self._saved = {k: getattr(config, k) for k in
                       ("COPY_MODE", "COPY_FIXED_USD", "SLIPPAGE_BPS", "FEE_BPS",
                        "MIN_SIGNAL_USD", "MIN_TRADE_USD", "SELL_MODE", "MAX_POSITION_USD")}
        config.COPY_MODE = "fixed"
        config.COPY_FIXED_USD = 200.0
        config.SLIPPAGE_BPS = 0.0
        config.FEE_BPS = 0.0
        config.MIN_SIGNAL_USD = 100.0
        config.MIN_TRADE_USD = 25.0
        config.SELL_MODE = "proportional"
        config.MAX_POSITION_USD = 1000.0

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            setattr(config, key, value)
        self._tmp.cleanup()

    def test_buy_then_proportional_sell(self) -> None:
        async def scenario() -> None:
            feed = StubPriceFeed({TOKEN_SOL: 1.0})
            trader = DemoTrader(db_path=self.db, price_feed=feed, initial_usd=10_000.0)
            await trader.start(reset=True)

            fill = await trader.handle_event(make_event(BUY, 1000.0, 1000.0, 1.0, "tx-buy"))
            self.assertEqual(fill.status, "FILLED")
            self.assertAlmostEqual(fill.qty, 200.0)
            self.assertAlmostEqual(trader.cash_usd, 9_800.0)

            # 相手が保有の半分を売却 -> こちらも半分を決済。価格は 2 倍
            feed._prices[TOKEN_SOL.lower()] = 2.0
            fill = await trader.handle_event(make_event(SELL, 500.0, 1000.0, 2.0, "tx-sell"))
            self.assertEqual(fill.status, "FILLED")
            self.assertAlmostEqual(fill.qty, 100.0)
            self.assertAlmostEqual(fill.realized_pnl, 100.0)  # 100 * (2.0 - 1.0)
            self.assertAlmostEqual(trader.cash_usd, 10_000.0)

            position = trader.positions[(config.SOLANA, WALLET_SOL, TOKEN_SOL)]
            self.assertAlmostEqual(position.qty, 100.0)
            snapshot = await trader.mark_to_market(record=False)
            self.assertAlmostEqual(snapshot["equity_usd"], 10_200.0)
            self.assertAlmostEqual(snapshot["unrealized_pnl"], 100.0)

            stats = await trader.stats()
            self.assertEqual(stats["filled"], 2)
            self.assertEqual(stats["closed_trades"], 1)
            self.assertAlmostEqual(stats["win_rate_pct"], 100.0)
            await trader.close()

        asyncio.run(scenario())

    def test_sell_without_position_is_skipped(self) -> None:
        async def scenario() -> None:
            trader = DemoTrader(
                db_path=self.db, price_feed=StubPriceFeed({TOKEN_SOL: 1.0}), initial_usd=1_000.0
            )
            await trader.start(reset=True)
            fill = await trader.handle_event(make_event(SELL, 10.0, 500.0, 1.0, "tx-orphan"))
            self.assertEqual(fill.status, "SKIPPED")
            self.assertIn("建玉なし", fill.note)
            self.assertAlmostEqual(trader.cash_usd, 1_000.0)
            await trader.close()

        asyncio.run(scenario())

    def test_small_signal_and_position_cap(self) -> None:
        async def scenario() -> None:
            trader = DemoTrader(
                db_path=self.db, price_feed=StubPriceFeed({TOKEN_SOL: 1.0}), initial_usd=10_000.0
            )
            await trader.start(reset=True)

            small = await trader.handle_event(make_event(BUY, 10.0, 50.0, 1.0, "tx-small"))
            self.assertEqual(small.status, "SKIPPED")

            config.MAX_POSITION_USD = 300.0
            await trader.handle_event(make_event(BUY, 200.0, 1000.0, 1.0, "tx-b1"))
            second = await trader.handle_event(make_event(BUY, 200.0, 1000.0, 1.0, "tx-b2"))
            self.assertEqual(second.status, "FILLED")
            self.assertAlmostEqual(second.notional_usd, 100.0)  # 上限 300 USD まで
            third = await trader.handle_event(make_event(BUY, 200.0, 1000.0, 1.0, "tx-b3"))
            self.assertEqual(third.status, "SKIPPED")
            await trader.close()

        asyncio.run(scenario())

    def test_state_survives_restart(self) -> None:
        async def scenario() -> None:
            feed = StubPriceFeed({TOKEN_SOL: 1.0})
            trader = DemoTrader(db_path=self.db, price_feed=feed, initial_usd=10_000.0)
            await trader.start(reset=True)
            await trader.handle_event(make_event(BUY, 1000.0, 1000.0, 1.0, "tx-restart"))
            cash = trader.cash_usd
            await trader.close()

            reopened = DemoTrader(db_path=self.db, price_feed=feed, initial_usd=10_000.0)
            await reopened.start()
            self.assertAlmostEqual(reopened.cash_usd, cash)
            self.assertIn((config.SOLANA, WALLET_SOL, TOKEN_SOL), reopened.positions)
            await reopened.close()

        asyncio.run(scenario())


# --------------------------------------------------------------------------
# シグナル集約
# --------------------------------------------------------------------------
def signal_event(
    wallet: str,
    side: str,
    usd: float,
    token: str = TOKEN_SOL,
    symbol: str = "WEN",
    chain: str = config.SOLANA,
    ts_ms: int | None = None,
    price: float = 1.0,
) -> SwapEvent:
    ts = ts_ms if ts_ms is not None else now_ms()
    return SwapEvent(
        chain=chain,
        wallet=wallet,
        wallet_label=wallet,
        tx_hash=f"tx-{wallet}-{ts}-{usd}",
        side=side,
        token_address=token,
        token_symbol=symbol,
        token_amount=usd / price,
        usd_amount=usd,
        price_usd=price,
        block_time_ms=ts - 500,
        detected_at_ms=ts,
        source="test",
    )


class SignalEngineTest(unittest.TestCase):
    def engine(self, **kwargs) -> SignalEngine:
        params = dict(
            window_sec=600, threshold_usd=10_000, min_wallets=2,
            exit_ratio=0.4, proxy_weight=0.5, cooldown_sec=0,
        )
        params.update(kwargs)
        return SignalEngine(**params)

    def test_asset_mapping(self) -> None:
        engine = self.engine()
        self.assertEqual(engine.asset_for_token(config.SOLANA, config.WSOL_MINT, "SOL"), ("SOL", "direct"))
        self.assertEqual(
            engine.asset_for_token("ethereum", "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599", "WBTC"),
            ("BTC", "direct"),
        )
        # メジャー以外はチェーンのネイティブ資産への代理シグナル
        self.assertEqual(engine.asset_for_token(config.SOLANA, TOKEN_SOL, "WEN"), ("SOL", "proxy"))
        # BNB / MATIC は MT5 のメジャーではないので対象外
        self.assertEqual(engine.asset_for_token("bsc", "0xabc", "CAKE"), (None, ""))

    def test_single_wallet_never_triggers(self) -> None:
        engine = self.engine()
        for _ in range(5):
            self.assertIsNone(engine.add(signal_event("w1", BUY, 8_000, token=config.WSOL_MINT, symbol="SOL")))
        self.assertEqual(engine.state("SOL"), FLAT)

    def test_long_then_flat_on_decay(self) -> None:
        engine = self.engine()
        self.assertIsNone(engine.add(signal_event("w1", BUY, 6_000, token=config.WSOL_MINT, symbol="SOL")))
        signal = engine.add(signal_event("w2", BUY, 6_000, token=config.WSOL_MINT, symbol="SOL"))
        assert signal is not None
        self.assertEqual((signal.asset, signal.direction, signal.wallets), ("SOL", LONG, 2))
        self.assertAlmostEqual(signal.score_usd, 12_000.0)

        # 反対売買でネットが縮むと手仕舞い
        exit_signal = engine.add(signal_event("w3", SELL, 9_000, token=config.WSOL_MINT, symbol="SOL"))
        assert exit_signal is not None
        self.assertEqual(exit_signal.direction, FLAT)
        self.assertEqual(engine.state("SOL"), FLAT)

    def test_reverse_to_short(self) -> None:
        engine = self.engine()
        engine.add(signal_event("w1", BUY, 6_000, token=config.WSOL_MINT, symbol="SOL"))
        engine.add(signal_event("w2", BUY, 6_000, token=config.WSOL_MINT, symbol="SOL"))
        engine.add(signal_event("w1", SELL, 14_000, token=config.WSOL_MINT, symbol="SOL"))
        signal = engine.add(signal_event("w2", SELL, 14_000, token=config.WSOL_MINT, symbol="SOL"))
        assert signal is not None
        self.assertEqual(signal.direction, SHORT)

    def test_window_expiry(self) -> None:
        engine = self.engine(window_sec=60)
        old = now_ms() - 120_000
        engine.add(signal_event("w1", BUY, 9_000, token=config.WSOL_MINT, symbol="SOL", ts_ms=old))
        # 古い投票はウィンドウ外なので、新しい 9,000 USD だけではしきい値に届かない
        self.assertIsNone(engine.add(signal_event("w2", BUY, 9_000, token=config.WSOL_MINT, symbol="SOL")))
        self.assertEqual(engine.state("SOL"), FLAT)

    def test_proxy_weight_halves_contribution(self) -> None:
        engine = self.engine(proxy_weight=0.5)
        engine.add(signal_event("w1", BUY, 12_000))  # メジャー以外 -> 6,000 相当
        self.assertIsNone(engine.add(signal_event("w2", BUY, 6_000)))  # 合計 9,000 < 10,000
        signal = engine.add(signal_event("w3", BUY, 6_000))  # 合計 12,000
        assert signal is not None
        self.assertEqual(signal.direction, LONG)
        self.assertAlmostEqual(signal.score_usd, 12_000.0)

    def test_cooldown_blocks_reentry(self) -> None:
        engine = self.engine(cooldown_sec=600)
        engine.add(signal_event("w1", BUY, 6_000, token=config.WSOL_MINT, symbol="SOL"))
        entry = engine.add(signal_event("w2", BUY, 6_000, token=config.WSOL_MINT, symbol="SOL"))
        assert entry is not None and entry.direction == LONG
        exit_signal = engine.add(signal_event("w3", SELL, 9_000, token=config.WSOL_MINT, symbol="SOL"))
        assert exit_signal is not None and exit_signal.direction == FLAT
        # クールダウン中は再エントリーしない
        engine.add(signal_event("w1", BUY, 9_000, token=config.WSOL_MINT, symbol="SOL"))
        self.assertIsNone(engine.add(signal_event("w2", BUY, 9_000, token=config.WSOL_MINT, symbol="SOL")))
        self.assertEqual(engine.state("SOL"), FLAT)


# --------------------------------------------------------------------------
# MT5 執行（dry-run ブローカー）
# --------------------------------------------------------------------------
class Mt5TraderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "trades.db"
        self._saved = {k: getattr(config, k) for k in
                       ("MT5_LOT_MODE", "MT5_FIXED_LOT", "MT5_RISK_PCT", "MT5_SL_PCT",
                        "MT5_TP_PCT", "MT5_MAX_POSITIONS", "MT5_CLOSE_ON_OPPOSITE", "MT5_MAX_LOT")}
        config.MT5_LOT_MODE = "fixed"
        config.MT5_FIXED_LOT = 0.05
        config.MT5_SL_PCT = 2.0
        config.MT5_TP_PCT = 4.0
        config.MT5_MAX_POSITIONS = 3
        config.MT5_MAX_LOT = 1.0
        config.MT5_CLOSE_ON_OPPOSITE = True

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            setattr(config, key, value)
        self._tmp.cleanup()

    def _trader(self) -> tuple[Mt5Trader, DryRunBroker]:
        broker = DryRunBroker(balance=10_000.0, prices={"SOL": 150.0})
        return Mt5Trader(broker, db_path=self.db), broker

    def test_open_reverse_and_flat(self) -> None:
        async def scenario() -> None:
            trader, broker = self._trader()
            await trader.start()
            spec = trader.symbols["SOL"]
            self.assertTrue(spec.name.startswith("SOLUSD"))

            engine = SignalEngine(window_sec=600, threshold_usd=10_000, min_wallets=2, cooldown_sec=0)
            engine.add(signal_event("w1", BUY, 6_000, token=config.WSOL_MINT, symbol="SOL", price=150.0))
            long_signal = engine.add(
                signal_event("w2", BUY, 6_000, token=config.WSOL_MINT, symbol="SOL", price=150.0)
            )
            assert long_signal is not None
            await trader.on_signal(long_signal)
            positions = broker.positions(spec)
            self.assertEqual(len(positions), 1)
            self.assertEqual(positions[0].direction, LONG)
            self.assertAlmostEqual(positions[0].volume, 0.05)
            self.assertLess(positions[0].sl, positions[0].price_open)
            self.assertGreater(positions[0].tp, positions[0].price_open)

            # 同方向シグナルは重複エントリーしない
            await trader.on_signal(long_signal)
            self.assertEqual(len(broker.positions(spec)), 1)
            self.assertEqual(trader.skipped, 1)

            # 反対シグナルでドテン
            engine.add(signal_event("w1", SELL, 14_000, token=config.WSOL_MINT, symbol="SOL", price=150.0))
            short_signal = engine.add(
                signal_event("w2", SELL, 14_000, token=config.WSOL_MINT, symbol="SOL", price=150.0)
            )
            assert short_signal is not None and short_signal.direction == SHORT
            await trader.on_signal(short_signal)
            positions = broker.positions(spec)
            self.assertEqual(len(positions), 1)
            self.assertEqual(positions[0].direction, SHORT)

            # FLAT で手仕舞い
            flat = engine.add(signal_event("w3", BUY, 20_000, token=config.WSOL_MINT, symbol="SOL", price=150.0))
            assert flat is not None and flat.direction == FLAT
            await trader.on_signal(flat)
            self.assertEqual(broker.positions(spec), [])

            rows = sqlite3.connect(self.db).execute(
                "SELECT action, direction FROM mt5_orders ORDER BY id"
            ).fetchall()
            actions = [r[0] for r in rows]
            self.assertEqual(actions.count("OPEN"), 2)
            self.assertEqual(actions.count("CLOSE"), 2)
            self.assertIn("SKIP", actions)
            await trader.close()

        asyncio.run(scenario())

    def test_volume_from_risk(self) -> None:
        async def scenario() -> None:
            config.MT5_LOT_MODE = "risk"
            config.MT5_RISK_PCT = 1.0     # 10,000 USD の 1% = 100 USD
            config.MT5_SL_PCT = 2.0       # 150 USD の 2% = 3.0 USD の値幅
            trader, _ = self._trader()
            await trader.start()
            spec = trader.symbols["SOL"]
            # 3.0 / 0.01 * 1.0 = 300 USD/ロット -> 100/300 = 0.333 -> ステップ 0.01 で切り捨て
            self.assertAlmostEqual(trader.volume_for(spec, 150.0), 0.33)
            # 上限でクランプされる
            config.MT5_MAX_LOT = 0.10
            self.assertAlmostEqual(trader.volume_for(spec, 150.0), 0.10)
            await trader.close()

        asyncio.run(scenario())

    def test_max_positions_guard(self) -> None:
        async def scenario() -> None:
            config.MT5_MAX_POSITIONS = 1
            trader, broker = self._trader()
            await trader.start()

            engine = SignalEngine(window_sec=600, threshold_usd=10_000, min_wallets=2, cooldown_sec=0)
            engine.add(signal_event("w1", BUY, 6_000, token=config.WSOL_MINT, symbol="SOL", price=150.0))
            sol_signal = engine.add(
                signal_event("w2", BUY, 6_000, token=config.WSOL_MINT, symbol="SOL", price=150.0)
            )
            assert sol_signal is not None
            await trader.on_signal(sol_signal)

            engine.add(signal_event("w1", BUY, 6_000, token="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
                                    symbol="WETH", chain="ethereum", price=3_000.0))
            eth_signal = engine.add(
                signal_event("w2", BUY, 6_000, token="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
                             symbol="WETH", chain="ethereum", price=3_000.0)
            )
            assert eth_signal is not None and eth_signal.asset == "ETH"
            await trader.on_signal(eth_signal)

            self.assertEqual(len(broker.positions(trader.symbols["ETH"])), 0)
            self.assertGreaterEqual(trader.skipped, 1)
            await trader.close()

        asyncio.run(scenario())

    def test_live_account_is_refused(self) -> None:
        async def scenario() -> None:
            broker = DryRunBroker()
            original = broker.connect

            def live_connect():
                account = original()
                account.is_demo = False
                return account

            broker.connect = live_connect  # type: ignore[method-assign]
            trader = Mt5Trader(broker, db_path=self.db)
            saved = config.MT5_ALLOW_LIVE
            config.MT5_ALLOW_LIVE = False
            try:
                with self.assertRaises(RuntimeError):
                    await trader.start()
            finally:
                config.MT5_ALLOW_LIVE = saved
                await trader.close()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
