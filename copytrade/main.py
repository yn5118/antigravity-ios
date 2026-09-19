"""エントリポイント。asyncio で最大 10 アドレスを並列監視する。

使い方:
    python -m copytrade.main --check-config       # 設定の確認のみ
    python -m copytrade.main                      # 監視 + デモトレード開始
    python -m copytrade.main --duration 600       # 10 分だけ動かす
    python -m copytrade.main --simulate --duration 30   # API キー無しの動作確認
    python -m copytrade.main --report             # trades.db の成績を表示して終了

構成:
    モニタ (アドレスごとに 1 タスク) --> asyncio.Queue --> DemoTrader (1 タスク)
                                                        --> 定期的な時価評価タスク
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

try:  # `python -m copytrade.main` / `python main.py` の両対応
    from . import config
    from . import monitor as monitor_mod
    from .demo_trader import DemoTrader
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config
    import monitor as monitor_mod
    from demo_trader import DemoTrader

log = logging.getLogger("copytrade")


# --------------------------------------------------------------------------
# ログ設定（ミリ秒まで出す）
# --------------------------------------------------------------------------
def setup_logging(level: str = config.LOG_LEVEL, log_file: Path | None = config.LOG_FILE) -> None:
    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)-18s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)

    logging.getLogger("aiohttp").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# 各タスク
# --------------------------------------------------------------------------
async def trade_worker(
    queue: "asyncio.Queue[monitor_mod.SwapEvent]",
    trader: DemoTrader,
    stop: asyncio.Event,
) -> None:
    """キューからイベントを取り出してデモ約定させる。"""
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            if stop.is_set() and queue.empty():
                return
            continue
        try:
            await trader.handle_event(event)
        except Exception:  # noqa: BLE001 - 1 件の失敗で停止させない
            log.exception("約定処理に失敗: %s", event.describe())
        finally:
            queue.task_done()


async def mark_worker(trader: DemoTrader, stop: asyncio.Event, interval: float) -> None:
    """定期的に建玉を時価評価してエクイティを記録する。"""
    while not stop.is_set():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
        if stop.is_set():
            return
        try:
            snapshot = await trader.mark_to_market()
            log.info(
                "時価評価 | 総資産 %.2f USD (%+.2f%%) | 現金 %.2f | 建玉 %.2f (%d) | 実現 %+.2f | 含み %+.2f",
                snapshot["equity_usd"],
                snapshot["return_pct"],
                snapshot["cash_usd"],
                snapshot["positions_usd"],
                int(snapshot["open_positions"]),
                snapshot["realized_pnl"],
                snapshot["unrealized_pnl"],
            )
        except Exception:  # noqa: BLE001
            log.exception("時価評価に失敗")


async def stopper(stop: asyncio.Event, duration: float | None) -> None:
    if not duration:
        return
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=duration)
    if not stop.is_set():
        log.info("指定時間 %.0fs が経過したため停止します", duration)
        stop.set()


def install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows 等
            pass


# --------------------------------------------------------------------------
# メイン
# --------------------------------------------------------------------------
async def run(args: argparse.Namespace) -> int:
    problems = config.validate()
    if problems:
        for problem in problems:
            log.error("設定エラー: %s", problem)
        return 2
    if not args.simulate and not args.report:
        for warning in config.missing_credentials():
            log.warning(warning)

    log.info("\n%s", config.summary())

    trader = DemoTrader(price_feed=None)
    await trader.start(reset=args.reset)

    if args.report:
        print(await trader.report())
        await trader.close()
        return 0

    session = monitor_mod.create_session()
    # シミュレーションでは架空アドレスのため価格 API を呼ばない
    live_price = not args.no_live_price and not args.simulate
    price_feed = monitor_mod.PriceFeed(session, enabled=live_price)
    trader.price_feed = price_feed

    wallets = config.WATCHED_WALLETS[: args.max_wallets] if args.max_wallets else config.WATCHED_WALLETS
    monitors = monitor_mod.build_monitors(wallets, price_feed, session, simulate=args.simulate)
    if not monitors:
        log.error("監視可能なウォレットがありません（API キーを確認してください）")
        await session.close()
        await trader.close()
        return 3

    queue: "asyncio.Queue[monitor_mod.SwapEvent]" = asyncio.Queue(maxsize=1000)
    stop = asyncio.Event()
    install_signal_handlers(stop)

    log.info(
        "監視開始: %d アドレス (%s) | 間隔 %.1fs | Ctrl+C で停止",
        len(monitors),
        ", ".join(f"{m.wallet.name}/{m.source}" for m in monitors),
        config.POLL_INTERVAL_SEC,
    )

    tasks = [asyncio.create_task(m.run(queue, stop), name=f"monitor:{m.wallet.name}") for m in monitors]
    tasks.append(asyncio.create_task(trade_worker(queue, trader, stop), name="trader"))
    tasks.append(
        asyncio.create_task(mark_worker(trader, stop, config.MARK_INTERVAL_SEC), name="mark")
    )
    tasks.append(asyncio.create_task(stopper(stop, args.duration), name="stopper"))

    try:
        await stop.wait()
    finally:
        log.info("停止処理中... 残りイベントを処理します")
        stop.set()
        await asyncio.sleep(0)
        for task in tasks:
            if task.get_name() != "trader":
                task.cancel()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(queue.join(), timeout=15)
        await asyncio.gather(*tasks, return_exceptions=True)

        for m in monitors:
            log.info(
                "[%s] ポーリング %d 回 / エラー %d 回 / 検知 %d 件",
                m.wallet.name,
                m.polls,
                m.errors,
                m.emitted,
            )
        log.info("価格 API: %d リクエスト / %d 失敗", price_feed.requests, price_feed.failures)
        print(await trader.report())
        await session.close()
        await trader.close()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Solana / EVM ウォレットのコピートレード デモ検証")
    parser.add_argument("--duration", type=float, default=None, help="指定秒数だけ実行して終了")
    parser.add_argument("--simulate", action="store_true", help="API を使わず擬似イベントで動作確認")
    parser.add_argument("--report", action="store_true", help="trades.db の成績を表示して終了")
    parser.add_argument("--reset", action="store_true", help="trades.db を初期化してから開始")
    parser.add_argument("--check-config", action="store_true", help="設定を検証して終了")
    parser.add_argument("--no-live-price", action="store_true", help="DEXScreener を使わずイベント価格で約定")
    parser.add_argument("--max-wallets", type=int, default=None, help="先頭 N 件だけ監視")
    parser.add_argument("--log-level", default=config.LOG_LEVEL, help="DEBUG / INFO / WARNING")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level.upper())

    if args.check_config:
        print(config.summary())
        problems = config.validate()
        for problem in problems:
            print(f"[NG] {problem}")
        for warning in config.missing_credentials():
            print(f"[警告] {warning}")
        if not problems:
            print("[OK] 設定に致命的な問題はありません")
        return 1 if problems else 0

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        log.info("中断されました")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
