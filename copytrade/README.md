# copytrade — Solana / EVM ウォレット監視 + デモトレード検証

特定ウォレット（最大 10 個）の DEX スワップを自動監視し、**ペーパートレード**で
コピートレードの成績（エントリー価格・数量・タイムラグ・損益）を SQLite に記録・検証する。
実際の発注は一切行わない。

## 構成

```
copytrade/
├── config.py        監視アドレス・APIキー・デモ初期資金・サイズ設定の一元管理
├── monitor.py       Helius / Solana RPC / Etherscan V2 でスワップ検知、DEXScreener 価格取得
├── demo_trader.py   仮想注文の執行、建玉管理、PnL 計算、trades.db への保存
├── main.py          asyncio による 10 アドレス並列監視のメインループ
├── tests.py         外部 API を使わない自己テスト（12 件）
└── requirements.txt
```

データフロー:

```
 監視タスク×N (アドレスごと)  --SwapEvent-->  asyncio.Queue  -->  DemoTrader (1タスク)
        ポーリング + 重複排除                                  └-> 定期の時価評価タスク
                                                                    -> trades.db
```

## 必要なライブラリ

| ライブラリ | 用途 |
|---|---|
| `aiohttp` | 非同期 HTTP（Helius / RPC / Etherscan / DEXScreener） |
| `requests` | 同期での単発確認・デバッグ用 |
| `python-dotenv`（任意） | `.env` からの APIキー読み込み |
| `asyncio` / `sqlite3` / `logging` / `dataclasses` | 標準ライブラリ |

```bash
pip install -r copytrade/requirements.txt
```

## セットアップ

1. APIキーを環境変数（または `.env`）に置く。キーはコードに書かない。

```bash
export HELIUS_API_KEY=xxxxxxxx        # Solana（推奨。無い場合は公開 RPC にフォールバック）
export ETHERSCAN_API_KEY=xxxxxxxx     # EVM 全チェーン共通（Etherscan V2）
```

2. 監視アドレスを設定する。`config.py` の `DEFAULT_WALLETS` を編集するか、環境変数で上書き:

```bash
export WATCHED_WALLETS="solana:<addr>:whale1,ethereum:0x...:whale2,base:0x...:trader3"
```

`config.py` の既定値は**形式確認用のダミー**なので、必ず実在アドレスに差し替える。

3. 設定を確認する。

```bash
python -m copytrade.main --check-config
```

## 実行

```bash
python -m copytrade.main --simulate --duration 30   # APIキー無しで全体の流れを確認
python -m copytrade.main                            # 監視開始（Ctrl+C で停止し成績を表示）
python -m copytrade.main --duration 3600            # 1 時間だけ実行
python -m copytrade.main --report                   # trades.db の成績のみ表示
python -m copytrade.main --reset                    # DB を初期化して開始
python tests.py                                     # 自己テスト
```

## 主な設定（すべて環境変数で上書き可能）

| 変数 | 既定値 | 意味 |
|---|---|---|
| `DEMO_INITIAL_BALANCE_USD` | 10000 | デモ口座の初期資金 |
| `COPY_MODE` | `fixed` | `fixed`=1回あたり固定額 / `ratio`=相手の取引額×倍率 |
| `COPY_FIXED_USD` / `COPY_RATIO` | 200 / 0.01 | サイズ決定パラメータ |
| `MAX_POSITION_USD` / `MIN_TRADE_USD` | 1000 / 25 | 1銘柄あたり上限 / 最小発注額 |
| `MIN_SIGNAL_USD` | 100 | これ未満の相手取引はノイズとして無視 |
| `SELL_MODE` | `proportional` | 相手の売却割合に比例して決済 / `full`=全決済 |
| `SLIPPAGE_BPS` / `FEE_BPS` | 50 / 30 | 想定スリッページ / 手数料（bps） |
| `MIN_LIQUIDITY_USD` | 10000 | 流動性がこれ未満の銘柄は約定させない |
| `POLL_INTERVAL_SEC` | 5 | アドレスごとのポーリング間隔 |
| `MARK_INTERVAL_SEC` | 60 | 建玉の時価評価とエクイティ記録の間隔 |
| `EMIT_BACKLOG` | false | true にすると起動前の過去取引も約定させる |
| `MAX_EVENT_AGE_SEC` | 600 | これより古いイベントは約定させない |
| `LOG_LEVEL` / `COPYTRADE_DB_PATH` | INFO / `copytrade/trades.db` | ログレベル / DB パス |

## 記録される内容（`trades.db`）

- `trades` — 約定・見送りの全履歴。`detect_lag_ms`（ブロック確定→検知）、
  `fill_lag_ms`（検知→仮想約定）、`total_lag_ms`、約定価格、手数料、実現損益、見送り理由。
- `positions` — 銘柄ごとの数量・平均取得単価・相手の保有数量（比例決済用）・実現損益。
- `account` — 現金・累計実現損益・累計手数料。
- `equity_curve` — 時価評価の推移（総資産・含み損益）。

ログは標準出力と `copytrade/copytrade.log`（5MB×3 ローテート）にミリ秒精度で出力される。

```
2026-09-19 11:38:00.113 | INFO | copytrade.monitor | 検知 [solana/sol-whale-01] BUY JUP qty=2,716.6874 usd=2,377.81 px=0.875 lag=1255ms tx=sim-sol-whal..
2026-09-19 11:38:00.113 | INFO | copytrade.trader  | 約定 BUY JUP qty=227.3661 @0.8796 notional=200.00 fee=0.60 cash=6,015.91 lag(detect/fill/total)=1255/1/1256ms
```

## 売買判定のしくみ

1. トランザクションからウォレットの**残高差分**を集計する
   （Helius の `tokenTransfers`/`nativeTransfers`、RPC の `pre/postTokenBalances`、
   Etherscan の `tokentx` を tx ハッシュでグルーピング）。
2. 差分を「決済通貨側（SOL/ETH/BNB/USDC/USDT…、`config.QUOTE_TOKENS`）」と
   「銘柄側」に分け、**銘柄側の差分が正なら BUY・負なら SELL** と判定する。
3. USD 換算は決済通貨側の数量 × その価格。決済通貨側が取れない場合（ネイティブ直スワップ等）は
   銘柄価格から逆算する。ステーブルは 1.0 USD 固定。
4. ガス代相当の端数は `DUST` / `NATIVE_DUST` で無視し、USDC↔SOL のような
   決済通貨同士の両替はシグナルとして扱わない。

## 制約・注意点

- **検証環境のネットワーク制限により、実 API（DEXScreener / Helius / Etherscan）への
  疎通確認は未実施**（`api.dexscreener.com` へは 403 = プロキシのポリシー拒否）。
  パーサとデモ約定ロジックは `tests.py` の固定データで検証済み。実キーでの初回起動時は
  `--log-level DEBUG --max-wallets 1` で 1 アドレスから確認することを推奨する。
- 価格取得に失敗した場合はイベントから逆算した価格で約定し、流動性チェックはスキップされる
  （`--no-live-price` で常にこの挙動にできる）。
- ポーリング方式のため、検知タイムラグは概ね「ブロック確定 + ポーリング間隔以内」。
  より低遅延にしたい場合は Helius Webhooks / Geyser や EVM の WebSocket 購読で
  `BaseWalletMonitor.run()` を置き換える（他のレイヤは変更不要）。
- 無料枠のレート制限に注意。10 アドレス × 5 秒間隔で 1 分あたり約 120 リクエストになる。
- 建玉は「ウォレット×銘柄」単位で管理するため、複数ウォレットが同じ銘柄を買うと建玉も複数になる。
- デモ（ペーパートレード）専用。秘密鍵を扱わず、実際の発注機能は持たない。
