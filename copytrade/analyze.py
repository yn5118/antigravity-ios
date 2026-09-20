"""``trades.db`` を分析して「本当に勝っているウォレット」を抽出する。

    python -m copytrade.analyze                     # ウォレット別の成績表
    python -m copytrade.analyze --days 14           # 直近 14 日だけ
    python -m copytrade.analyze --top 3             # 上位 3 件を .env 形式で出力
    python -m copytrade.analyze --tokens            # 銘柄別の成績
    python -m copytrade.analyze --csv out.csv       # CSV に書き出し

スコアは「実現損益 × 勝率の信頼度」ではなく、素直に実現損益・勝率・決済回数を
並べて表示する。判断材料としてサンプル数（決済回数）を必ず見ること。
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from . import config
except ImportError:  # pragma: no cover
    import config


@dataclass
class WalletStats:
    """1 ウォレットのコピートレード成績。"""

    chain: str = ""
    wallet: str = ""
    label: str = ""
    fills: int = 0
    buys: int = 0
    closes: int = 0
    wins: int = 0
    realized_pnl: float = 0.0
    best: float = 0.0
    worst: float = 0.0
    fees: float = 0.0
    invested: float = 0.0
    lag_sum: float = 0.0
    lag_count: int = 0
    hold_minutes: list[float] = field(default_factory=list)
    open_positions: int = 0
    unrealized: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.closes * 100 if self.closes else 0.0

    @property
    def avg_lag_ms(self) -> float:
        return self.lag_sum / self.lag_count if self.lag_count else 0.0

    @property
    def avg_hold_min(self) -> float:
        return sum(self.hold_minutes) / len(self.hold_minutes) if self.hold_minutes else 0.0

    @property
    def roi_pct(self) -> float:
        return self.realized_pnl / self.invested * 100 if self.invested else 0.0

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized

    def as_row(self) -> dict[str, object]:
        return {
            "chain": self.chain,
            "label": self.label,
            "wallet": self.wallet,
            "fills": self.fills,
            "closes": self.closes,
            "win_rate_pct": round(self.win_rate, 1),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized, 2),
            "total_pnl": round(self.total_pnl, 2),
            "roi_pct": round(self.roi_pct, 2),
            "invested_usd": round(self.invested, 2),
            "fees_usd": round(self.fees, 2),
            "best": round(self.best, 2),
            "worst": round(self.worst, 2),
            "avg_hold_min": round(self.avg_hold_min, 1),
            "avg_lag_ms": round(self.avg_lag_ms, 0),
            "open_positions": self.open_positions,
        }


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"DB が見つかりません: {db_path}（先に監視を実行してください）")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def collect_wallets(conn: sqlite3.Connection, since_ms: int = 0) -> dict[str, WalletStats]:
    """約定履歴を走査してウォレット別成績を作る（保有時間は FIFO で対応付け）。"""
    stats: dict[str, WalletStats] = {}
    # (wallet, token) ごとに買いの時刻を FIFO で保持し、売りと突き合わせる
    open_buys: dict[tuple[str, str], list[int]] = {}

    rows = conn.execute(
        "SELECT * FROM trades WHERE status='FILLED' AND filled_at_ms >= ? ORDER BY filled_at_ms",
        (since_ms,),
    )
    for row in rows:
        key = f"{row['chain']}:{row['wallet']}"
        entry = stats.setdefault(
            key, WalletStats(chain=row["chain"], wallet=row["wallet"], label=row["wallet_label"])
        )
        entry.fills += 1
        entry.fees += float(row["fee_usd"])
        entry.lag_sum += float(row["total_lag_ms"])
        entry.lag_count += 1

        pair = (row["wallet"], row["token_address"])
        if row["side"] == "BUY":
            entry.buys += 1
            entry.invested += float(row["notional_usd"])
            open_buys.setdefault(pair, []).append(int(row["filled_at_ms"]))
        else:
            entry.closes += 1
            pnl = float(row["realized_pnl"])
            entry.realized_pnl += pnl
            entry.best = max(entry.best, pnl)
            entry.worst = min(entry.worst, pnl)
            if pnl > 0:
                entry.wins += 1
            queue = open_buys.get(pair)
            if queue:
                opened = queue.pop(0)
                entry.hold_minutes.append((int(row["filled_at_ms"]) - opened) / 60_000)

    for row in conn.execute("SELECT * FROM positions WHERE qty > 0"):
        key = f"{row['chain']}:{row['wallet']}"
        entry = stats.setdefault(
            key, WalletStats(chain=row["chain"], wallet=row["wallet"], label=row["wallet_label"])
        )
        entry.open_positions += 1
        price = float(row["last_price_usd"]) or float(row["avg_price_usd"])
        entry.unrealized += float(row["qty"]) * price - float(row["cost_usd"])

    return stats


def collect_tokens(conn: sqlite3.Connection, since_ms: int = 0) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT token_symbol,"
        " SUM(side='BUY') AS buys,"
        " SUM(side='SELL') AS sells,"
        " SUM(CASE WHEN side='SELL' THEN realized_pnl ELSE 0 END) AS pnl,"
        " SUM(CASE WHEN side='SELL' AND realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,"
        " SUM(CASE WHEN side='BUY' THEN notional_usd ELSE 0 END) AS invested"
        " FROM trades WHERE status='FILLED' AND filled_at_ms >= ?"
        " GROUP BY token_symbol ORDER BY pnl DESC",
        (since_ms,),
    ).fetchall()
    result = []
    for row in rows:
        sells = int(row["sells"] or 0)
        result.append(
            {
                "token": row["token_symbol"],
                "buys": int(row["buys"] or 0),
                "sells": sells,
                "win_rate_pct": round(int(row["wins"] or 0) / sells * 100, 1) if sells else 0.0,
                "pnl": round(float(row["pnl"] or 0.0), 2),
                "invested": round(float(row["invested"] or 0.0), 2),
            }
        )
    return result


def skip_reasons(conn: sqlite3.Connection, since_ms: int = 0) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT note, COUNT(*) AS n FROM trades WHERE status='SKIPPED' AND filled_at_ms >= ?"
        " GROUP BY note ORDER BY n DESC LIMIT 10",
        (since_ms,),
    ).fetchall()
    return [(str(r["note"]), int(r["n"])) for r in rows]


def rank(stats: dict[str, WalletStats], min_closes: int, by: str) -> list[WalletStats]:
    def key(entry: WalletStats) -> float:
        if by == "winrate":
            return entry.win_rate
        if by == "roi":
            return entry.roi_pct
        if by == "total":
            return entry.total_pnl
        return entry.realized_pnl

    eligible = [s for s in stats.values() if s.closes >= min_closes]
    return sorted(eligible, key=key, reverse=True)


def display_width(text: str) -> int:
    """全角を 2 桁として数えた表示幅。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int, align: str = "<") -> str:
    """全角混じりでも桁が揃うようにパディングする。"""
    space = max(0, width - display_width(text))
    if align == ">":
        return " " * space + text
    return text + " " * space


def print_table(entries: list[WalletStats]) -> None:
    columns = [
        ("ラベル", 18, "<"), ("chain", 10, "<"), ("約定", 6, ">"), ("決済", 6, ">"),
        ("勝率", 8, ">"), ("実現損益", 14, ">"), ("含み", 12, ">"), ("ROI", 9, ">"),
        ("平均保有", 10, ">"), ("平均ラグ", 11, ">"),
    ]
    print(" ".join(pad(name, width, align) for name, width, align in columns))
    print("-" * sum(width + 1 for _, width, _ in columns))
    for entry in entries:
        values = [
            entry.label[:18],
            entry.chain,
            str(entry.fills),
            str(entry.closes),
            f"{entry.win_rate:.1f}%",
            f"{entry.realized_pnl:+,.2f}",
            f"{entry.unrealized:+,.2f}",
            f"{entry.roi_pct:+.2f}%",
            f"{entry.avg_hold_min:.1f}分",
            f"{entry.avg_lag_ms:,.0f}ms",
        ]
        print(" ".join(pad(v, w, a) for v, (_, w, a) in zip(values, columns)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="trades.db からウォレット成績を分析する")
    parser.add_argument("--db", default=str(config.DB_PATH), help="trades.db のパス")
    parser.add_argument("--days", type=float, default=0.0, help="直近 N 日だけを対象にする")
    parser.add_argument("--min-closes", type=int, default=1, help="決済回数がこれ未満のウォレットを除外")
    parser.add_argument(
        "--sort", default="realized", choices=["realized", "total", "roi", "winrate"],
        help="並び替えの基準（既定: 実現損益）",
    )
    parser.add_argument("--top", type=int, default=0, help="上位 N 件を WATCHED_WALLETS 形式で出力")
    parser.add_argument("--tokens", action="store_true", help="銘柄別の成績も表示")
    parser.add_argument("--skips", action="store_true", help="見送り理由の内訳を表示")
    parser.add_argument("--csv", default="", help="ウォレット別成績を CSV に書き出す")
    args = parser.parse_args(argv)

    conn = connect(Path(args.db))
    since_ms = 0
    if args.days > 0:
        import time

        since_ms = int((time.time() - args.days * 86_400) * 1000)

    stats = collect_wallets(conn, since_ms)
    if not stats:
        print("約定履歴がありません。監視を実行してデータを貯めてください。")
        return 1

    entries = rank(stats, args.min_closes, args.sort)
    period = f"直近 {args.days:g} 日" if args.days else "全期間"
    print(f"\n=== ウォレット別成績（{period} / 決済 {args.min_closes} 回以上 / {args.sort} 順）===")
    print_table(entries)

    total_realized = sum(e.realized_pnl for e in stats.values())
    total_unrealized = sum(e.unrealized for e in stats.values())
    print("-" * 110)
    print(
        f"合計: 実現 {total_realized:+,.2f} USD / 含み {total_unrealized:+,.2f} USD"
        f" / 対象 {len(entries)} ウォレット（全 {len(stats)} 件中）"
    )

    if args.tokens:
        print("\n=== 銘柄別 ===")
        token_columns = [
            ("銘柄", 14, "<"), ("買", 6, ">"), ("売", 6, ">"),
            ("勝率", 8, ">"), ("実現損益", 14, ">"), ("投入額", 14, ">"),
        ]
        print(" ".join(pad(name, width, align) for name, width, align in token_columns))
        print("-" * sum(width + 1 for _, width, _ in token_columns))
        for row in collect_tokens(conn, since_ms):
            values = [
                str(row["token"])[:14],
                str(row["buys"]),
                str(row["sells"]),
                f"{float(row['win_rate_pct']):.1f}%",
                f"{float(row['pnl']):+,.2f}",
                f"{float(row['invested']):,.2f}",
            ]
            print(" ".join(pad(v, w, a) for v, (_, w, a) in zip(values, token_columns)))

    if args.skips:
        print("\n=== 見送り理由 ===")
        for note, count in skip_reasons(conn, since_ms):
            print(f"  {count:>5} 件  {note}")

    if args.top:
        best = entries[: args.top]
        print(f"\n=== 上位 {len(best)} 件（.env にそのまま貼れます）===")
        spec = ",".join(f"{e.chain}:{e.wallet}:{e.label or 'top'}" for e in best)
        print(f"WATCHED_WALLETS={spec}")
        print("\n※ 決済回数が少ないウォレットは偶然勝っている可能性があります。")
        print("　 --min-closes で足切りし、数週間分のデータで判断してください。")

    if args.csv:
        path = Path(args.csv)
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            rows = [e.as_row() for e in entries]
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nCSV を書き出しました: {path}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
