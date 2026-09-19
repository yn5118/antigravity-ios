"""copytrade の自己テスト（外部 API を使わない）。

    python -m unittest copytrade.tests      # もしくは
    python tests.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

try:  # パッケージ / 単体の両対応
    from . import config
    from . import monitor as monitor_mod
    from .demo_trader import DemoTrader
    from .monitor import BUY, SELL, PriceFeed, SwapEvent, classify_swap, now_ms
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config
    import monitor as monitor_mod
    from demo_trader import DemoTrader
    from monitor import BUY, SELL, PriceFeed, SwapEvent, classify_swap, now_ms

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
