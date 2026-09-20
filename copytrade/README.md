# copytrade — Solana / EVM ウォレット監視 + デモトレード検証

特定ウォレット（最大 10 個）の DEX スワップを自動監視し、**ペーパートレード**で
コピートレードの成績（エントリー価格・数量・タイムラグ・損益）を SQLite に記録・検証する。
実際の発注は一切行わない。

## 構成

```
copytrade/
├── config.py        監視アドレス・APIキー・デモ初期資金・サイズ/シグナル/MT5 設定
├── monitor.py       Helius / Solana RPC / Etherscan V2 でスワップ検知、DEXScreener 価格取得
├── demo_trader.py   仮想注文の執行、建玉管理、PnL 計算、trades.db への保存
├── signals.py       オンチェーンの売買を BTC/ETH/SOL の方向性シグナルに集約
├── mt5_trader.py    シグナルを MT5（MetaTrader5 パッケージ）で執行。dry-run 対応
├── analyze.py       trades.db から「勝っているウォレット」を抽出する分析 CLI
├── main.py          asyncio による 10 アドレス並列監視のメインループ
├── tests.py         外部 API / MT5 を使わない自己テスト（23 件）
└── requirements.txt
```

データフロー:

```
 監視タスク×N (アドレスごと)  --SwapEvent-->  asyncio.Queue  --> DemoTrader     -> trades.db
        ポーリング + 重複排除                                  └-> SignalEngine -- MajorSignal -->
                                                                   Mt5Trader (--mt5 / --mt5-dry-run)
```

MT5 連携は任意。`--mt5` / `--mt5-dry-run` を付けなければ従来どおりデモ記録のみで動く。

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

## Windows (コマンドプロンプト) での手順

```bat
cd /d C:\Users\<ユーザー名>
mkdir dev & cd dev
git clone https://github.com/yn5118/antigravity-ios.git
cd antigravity-ios
git checkout claude/solana-evm-wallet-demo-trading-t5jut0
python -m pip install -r copytrade\requirements.txt

python -m copytrade.main --simulate --duration 30 --reset
```

- コマンドはすべて `antigravity-ios` フォルダ（`copytrade` フォルダがある階層）で実行する。
- 環境変数は `export` ではなく `set`（そのウィンドウのみ有効）。恒久設定は `setx` か
  リポジトリ直下に `.env` を置く（`python-dotenv` が読み込む）。

```bat
set HELIUS_API_KEY=xxxxxxxx
set ETHERSCAN_API_KEY=xxxxxxxx
set WATCHED_WALLETS=solana:<addr>:whale1,ethereum:0x...:whale2
```

- `python` が見つからない場合は `py -3` を使う（`py -3 -m copytrade.main ...`）。
- Ctrl+C で停止できる（Windows では `signal` モジュール経由でハンドラを登録している）。
  停止時に成績が表示されるが、途中で強制終了しても約定は都度 `trades.db` に書かれているため
  `--report` で後から確認できる。
- `sqlite3` コマンドが無い場合は Python から参照する:

```bat
python -c "import sqlite3;c=sqlite3.connect(r'copytrade\trades.db');print(*c.execute('select wallet_label,token_symbol,round(realized_pnl,2) from trades where side=\'SELL\' and status=\'FILLED\' order by realized_pnl desc limit 10'),sep=chr(10))"
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

## MT5 へ繋ぐまでの流れ

MT5 のメジャー銘柄（BTCUSD / ETHUSD / SOLUSD）で自動売買することを想定した段階的な手順。

**フェーズ1: データを貯める**

```bash
python -m copytrade.main            # 数日〜数週間動かす
```

**フェーズ2: 勝っているウォレットを絞る**

```bash
python -m copytrade.analyze --days 14 --min-closes 10 --tokens --skips
python -m copytrade.analyze --top 3 --min-closes 10   # .env に貼れる形式で出力
```

決済回数が少ないウォレットは偶然勝っているだけの可能性が高いので、`--min-closes` で
足切りしてから判断する。

**フェーズ3: シグナル化の確認（MT5 に繋がない）**

```bash
python -m copytrade.main --mt5-dry-run --duration 3600
```

`SignalEngine` がどのタイミングで LONG / SHORT を出すかをログで確認し、
`SIGNAL_NET_USD_THRESHOLD` などを調整する。

**フェーズ4: MT5 デモ口座で執行**

```bash
pip install MetaTrader5          # Windows のみ
python -m copytrade.main --mt5
```

MT5 ターミナルを起動してデモ口座にログインし、「アルゴリズム取引」を有効にしておく。
実口座に接続していると `MT5_ALLOW_LIVE=1` を明示しない限り起動時に停止する。

**フェーズ5: 小ロットで実弾** — フェーズ4の成績を確認してから。

## シグナルの作り方

個々のスワップを「投票」として扱い、ローリングウィンドウ内のネット金額で方向を決める。

| 種別 | 対象 | 重み |
|---|---|---|
| 直接 (direct) | WBTC / WETH / SOL など、メジャー資産そのものの売買 | 1.0 |
| 代理 (proxy) | それ以外のトークンの売買を、そのチェーンのネイティブ資産への強気/弱気とみなす | `SIGNAL_PROXY_WEIGHT`（既定 0.5）|

- ネット金額が `SIGNAL_NET_USD_THRESHOLD` を超え、かつ同じ方向に寄与したウォレットが
  `SIGNAL_MIN_WALLETS` 以上のときだけ LONG / SHORT を出す（1 件の大口では発火しない）。
- ネットがしきい値の `SIGNAL_EXIT_RATIO` 倍を下回ると FLAT（手仕舞い）。
- 同じ資産で連続エントリーしないよう `SIGNAL_COOLDOWN_SEC` のクールダウンを持つ。
- BNB / POL は MT5 のメジャーではないため代理シグナルの対象外（`CHAIN_PROXY_ASSET`）。

**代理シグナルは解釈であって資金フローそのものではない。** SOL でミームコインを買う行為は
厳密には SOL を手放しているが、ここでは「そのチェーンに強気」と解釈している。
この解釈を使いたくない場合は `SIGNAL_PROXY_WEIGHT=0` にすれば直接マッピングだけになる。

## MT5 の主な設定

| 変数 | 既定値 | 意味 |
|---|---|---|
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | 空 | 省略時は起動中のターミナルのログイン状態を使う |
| `MT5_TERMINAL_PATH` | 空 | `terminal64.exe` のパス（複数インストール時） |
| `MT5_SYMBOL_BTC` / `_ETH` / `_SOL` | 自動 | ブローカー固有の銘柄名で上書き（例: `BTCUSD.pro`）|
| `MT5_LOT_MODE` | `fixed` | `fixed`=固定ロット / `risk`=残高に対するリスク%から逆算 |
| `MT5_FIXED_LOT` / `MT5_RISK_PCT` | 0.01 / 0.5 | ロット決定パラメータ |
| `MT5_SL_PCT` / `MT5_TP_PCT` | 1.5 / 3.0 | 損切り / 利確（価格に対する%、0 で無効）|
| `MT5_MAX_POSITIONS` / `MT5_MAX_LOT` | 3 / 1.0 | 同時建玉数 / 1 回あたりの上限ロット |
| `MT5_CLOSE_ON_OPPOSITE` | true | 反対シグナルでドテンする |
| `MT5_MAGIC` | 20260920 | このプログラムの建玉を識別する番号。他の建玉には触らない |
| `MT5_ALLOW_LIVE` | false | true にしない限り実口座では発注しない |

発注結果は `trades.db` の `mt5_orders` テーブルに記録される（OPEN / CLOSE / SKIP / ERROR）。

## 分析 CLI (`analyze.py`)

```bash
python -m copytrade.analyze                      # ウォレット別成績
python -m copytrade.analyze --sort roi           # 並び替え: realized / total / roi / winrate
python -m copytrade.analyze --days 7 --tokens    # 期間指定 + 銘柄別
python -m copytrade.analyze --skips              # 見送り理由の内訳（設定が厳しすぎないか）
python -m copytrade.analyze --csv report.csv     # CSV 出力
```

表示項目: 約定数 / 決済数 / 勝率 / 実現損益 / 含み損益 / ROI / 平均保有時間 / 平均タイムラグ。

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
- オンチェーン側はデモ（ペーパートレード）専用。秘密鍵を扱わず、DEX への実発注機能は持たない。
- **MT5 とオンチェーンの銘柄は一致しない。** MT5 には BONK / WIF / PEPE のような
  トークンは存在せず、扱えるのは BTCUSD / ETHUSD / SOLUSD などのメジャー CFD だけ。
  そのためオンチェーンの動きは「メジャー銘柄の方向性を示す指標」としてのみ使う。
- `mt5_trader.py` は MetaTrader5 パッケージ（Windows 専用）が必要。この検証環境では
  実 MT5 への接続は確認できていないため、ロジックは `DryRunBroker` で検証している。
  Windows では必ず `--mt5-dry-run` → MT5 デモ口座 → 小ロット実弾の順で確認すること。
- MT5 は週末クローズ・スプレッド・スワップ・最小ロットがあり、24/365 の DEX とは
  執行条件が異なる。同じシグナルでも成績は一致しない。
