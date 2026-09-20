"""監視対象ウォレットと動作パラメータの一元管理。

APIキーなどの秘密情報は環境変数（または .env）から読み込む。
コード内に鍵を直接書かないこと。

主な環境変数:
    HELIUS_API_KEY      Helius Enhanced Transactions API のキー（Solana 推奨）
    SOLANA_RPC_URL      Solana RPC（Helius キーが無い場合のフォールバック）
    ETHERSCAN_API_KEY   Etherscan V2 マルチチェーン API のキー（EVM 用）
    WATCHED_WALLETS     "chain:address:label" をカンマ区切りで指定すると既定値を上書き
    DEMO_INITIAL_BALANCE_USD / COPY_MODE / COPY_FIXED_USD / ... （下記参照）
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # .env があれば読む（無くても動く）
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - python-dotenv は任意依存
    pass


# --------------------------------------------------------------------------
# 環境変数ヘルパ
# --------------------------------------------------------------------------
def _env_str(key: str, default: str = "") -> str:
    value = os.environ.get(key)
    return value.strip() if value and value.strip() else default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ[key])
    except (KeyError, TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------
# チェーン定義
# --------------------------------------------------------------------------
SOLANA = "solana"
EVM_CHAIN_IDS = {  # Etherscan V2 の chainid
    "ethereum": 1,
    "bsc": 56,
    "polygon": 137,
    "base": 8453,
    "arbitrum": 42161,
}
#: DEXScreener の chain slug（このプログラムではチェーン名と一致させている）
DEXSCREENER_CHAINS = {SOLANA: "solana", **{c: c for c in EVM_CHAIN_IDS}}
SUPPORTED_CHAINS = tuple(DEXSCREENER_CHAINS)


# --------------------------------------------------------------------------
# 監視対象ウォレット（最大 10 個）
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class WatchedWallet:
    """監視する 1 ウォレットの設定。"""

    address: str
    chain: str
    label: str = ""
    #: このウォレットだけ建玉サイズを増減させたいときの倍率
    weight: float = 1.0

    @property
    def key(self) -> str:
        return f"{self.chain}:{self.address}"

    @property
    def name(self) -> str:
        return self.label or f"{self.address[:4]}..{self.address[-4:]}"


MAX_WALLETS = 10

#: 既定のサンプル。実運用では WATCHED_WALLETS 環境変数か、この配列を書き換える。
#: （アドレスは形式確認用のダミー。必ず実在のアドレスに差し替えること）
DEFAULT_WALLETS: list[WatchedWallet] = [
    WatchedWallet("5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9", SOLANA, "sol-whale-01"),
    WatchedWallet("9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", SOLANA, "sol-whale-02"),
    WatchedWallet("CuieVDEDtLo7FypA9SbLM9saXFdb1dsshEkyErMqkRQq", SOLANA, "sol-sniper-01"),
    WatchedWallet("2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S", SOLANA, "sol-sniper-02"),
    WatchedWallet("BXP2gNKuqbmjzsiHpVJQTkPLtd6ENvPuYS9uPmQvpump", SOLANA, "sol-memecoin-01"),
    WatchedWallet("0x28c6c06298d514db089934071355e5743bf21d60", "ethereum", "eth-whale-01"),
    WatchedWallet("0x21a31ee1afc51d94c2efccaa2092ad1028285549", "ethereum", "eth-whale-02"),
    WatchedWallet("0xdfd5293d8e347dfe59e90efd55b2956a1343963d", "base", "base-trader-01"),
    WatchedWallet("0x56eddb7aa87536c09ccc2793473599fd21a8b17f", "bsc", "bsc-trader-01"),
    WatchedWallet("0x1f9090aae28b8a3dceadf281b0f12828e676c326", "arbitrum", "arb-trader-01"),
]


def _parse_wallets_env(raw: str) -> list[WatchedWallet]:
    """"chain:address:label,chain:address" 形式を WatchedWallet 群に変換する。"""
    wallets: list[WatchedWallet] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split(":")]
        if len(parts) < 2:
            raise ValueError(f"WATCHED_WALLETS の書式が不正です: {chunk!r}")
        chain, address = parts[0].lower(), parts[1]
        label = parts[2] if len(parts) > 2 else ""
        wallets.append(WatchedWallet(address=address, chain=chain, label=label))
    return wallets


_wallets_env = _env_str("WATCHED_WALLETS")
WATCHED_WALLETS: list[WatchedWallet] = (
    _parse_wallets_env(_wallets_env) if _wallets_env else list(DEFAULT_WALLETS)
)[:MAX_WALLETS]


# --------------------------------------------------------------------------
# API / エンドポイント
# --------------------------------------------------------------------------
HELIUS_API_KEY = _env_str("HELIUS_API_KEY")
HELIUS_BASE_URL = _env_str("HELIUS_BASE_URL", "https://api.helius.xyz")
SOLANA_RPC_URL = _env_str(
    "SOLANA_RPC_URL",
    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    if HELIUS_API_KEY
    else "https://api.mainnet-beta.solana.com",
)
ETHERSCAN_API_KEY = _env_str("ETHERSCAN_API_KEY")
ETHERSCAN_V2_URL = _env_str("ETHERSCAN_V2_URL", "https://api.etherscan.io/v2/api")
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens"

#: 1 回のポーリングで取得する最大トランザクション数
FETCH_LIMIT = _env_int("FETCH_LIMIT", 25)
#: HTTP タイムアウト（秒）
HTTP_TIMEOUT_SEC = _env_float("HTTP_TIMEOUT_SEC", 15.0)


# --------------------------------------------------------------------------
# ポーリング設定
# --------------------------------------------------------------------------
#: アドレスごとのポーリング間隔（秒）。無料枠のレート制限に注意
POLL_INTERVAL_SEC = _env_float("POLL_INTERVAL_SEC", 5.0)
#: 起動直後の過去履歴を約定させるか（既定 False = 既読扱いにして捨てる）
EMIT_BACKLOG = _env_bool("EMIT_BACKLOG", False)
#: 連続エラー時の最大バックオフ（秒）
MAX_BACKOFF_SEC = _env_float("MAX_BACKOFF_SEC", 60.0)
#: 起動時にアドレスごとにずらす秒数（レート制限の平準化）
STAGGER_SEC = _env_float("STAGGER_SEC", 0.4)
#: 検知済み署名を保持する上限件数
SEEN_CACHE_SIZE = _env_int("SEEN_CACHE_SIZE", 2000)
#: ブロック時刻がこれより古いイベントは約定させない（秒）
MAX_EVENT_AGE_SEC = _env_float("MAX_EVENT_AGE_SEC", 600.0)


# --------------------------------------------------------------------------
# デモ口座 / コピートレード設定
# --------------------------------------------------------------------------
DEMO_INITIAL_BALANCE_USD = _env_float("DEMO_INITIAL_BALANCE_USD", 10_000.0)
#: "fixed"  = 1 トレードあたり固定 USD
#: "ratio"  = 監視ウォレットの取引額 × COPY_RATIO
COPY_MODE = _env_str("COPY_MODE", "fixed").lower()
COPY_FIXED_USD = _env_float("COPY_FIXED_USD", 200.0)
COPY_RATIO = _env_float("COPY_RATIO", 0.01)
#: 1 銘柄あたりの最大建玉（USD）
MAX_POSITION_USD = _env_float("MAX_POSITION_USD", 1_000.0)
#: 発注の最小サイズ（USD）。これ未満は見送る
MIN_TRADE_USD = _env_float("MIN_TRADE_USD", 25.0)
#: 同時保有できる建玉数
MAX_OPEN_POSITIONS = _env_int("MAX_OPEN_POSITIONS", 20)
#: 監視ウォレットの取引額がこれ未満ならノイズとして無視（USD）
MIN_SIGNAL_USD = _env_float("MIN_SIGNAL_USD", 100.0)
#: 売却の追随方法 "proportional"（相手の売却割合に比例） / "full"（全決済）
SELL_MODE = _env_str("SELL_MODE", "proportional").lower()
#: 想定スリッページ（bps, 1bps = 0.01%）
SLIPPAGE_BPS = _env_float("SLIPPAGE_BPS", 50.0)
#: 想定手数料（bps）: DEX 手数料 + ガス相当をまとめて近似
FEE_BPS = _env_float("FEE_BPS", 30.0)
#: DEXScreener 価格のキャッシュ有効期間（秒）
PRICE_CACHE_TTL_SEC = _env_float("PRICE_CACHE_TTL_SEC", 5.0)
#: 流動性がこれ未満のトークンは約定させない（USD）
MIN_LIQUIDITY_USD = _env_float("MIN_LIQUIDITY_USD", 10_000.0)
#: 建玉の時価評価とエクイティ記録の間隔（秒）
MARK_INTERVAL_SEC = _env_float("MARK_INTERVAL_SEC", 60.0)


# --------------------------------------------------------------------------
# 「決済通貨」側として扱うトークン（スワップの Buy / Sell 判定に使う）
# --------------------------------------------------------------------------
WSOL_MINT = "So11111111111111111111111111111111111111112"
#: ネイティブ SOL を token delta として表現するための擬似 mint
NATIVE_SOL = "native:SOL"

STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "USDC.E", "USDBC", "FDUSD", "TUSD", "BUSD"}

QUOTE_TOKENS: dict[str, dict[str, str]] = {
    SOLANA: {
        NATIVE_SOL: "SOL",
        WSOL_MINT: "SOL",
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    },
    "ethereum": {
        "native:ETH": "ETH",
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
        "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
        "0x6b175474e89094c44da98b954eedeac495271d0f": "DAI",
    },
    "base": {
        "native:ETH": "ETH",
        "0x4200000000000000000000000000000000000006": "WETH",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": "USDBC",
    },
    "bsc": {
        "native:BNB": "BNB",
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": "WBNB",
        "0x55d398326f99059ff775485246999027b3197955": "USDT",
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": "USDC",
    },
    "polygon": {
        "native:POL": "POL",
        "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270": "WPOL",
        "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": "USDC",
        "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": "USDT",
    },
    "arbitrum": {
        "native:ETH": "ETH",
        "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": "WETH",
        "0xaf88d065e77c8cc2239327c5edb3a432268e5831": "USDC",
        "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": "USDT",
    },
}

#: USD 換算の基準にする「ネイティブ通貨」の価格参照先（DEXScreener 用アドレス）
NATIVE_PRICE_REFERENCE: dict[str, str] = {
    SOLANA: WSOL_MINT,
    "ethereum": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "base": "0x4200000000000000000000000000000000000006",
    "bsc": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    "polygon": "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",
    "arbitrum": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
}


def is_quote_token(chain: str, address: str) -> bool:
    """スワップの「支払い側」として扱うトークンかどうか。"""
    table = QUOTE_TOKENS.get(chain, {})
    return address.lower() in {k.lower() for k in table}


def quote_symbol(chain: str, address: str) -> str:
    table = {k.lower(): v for k, v in QUOTE_TOKENS.get(chain, {}).items()}
    return table.get(address.lower(), "")


def is_stable(symbol: str) -> bool:
    return symbol.upper() in STABLE_SYMBOLS


# --------------------------------------------------------------------------
# シグナル集約（オンチェーンの動き -> MT5 メジャー銘柄の方向性）
# --------------------------------------------------------------------------
#: MT5 側で扱うメジャー資産
MAJOR_ASSETS = ("BTC", "ETH", "SOL")

#: 直接マッピングできるトークン（シンボル or アドレス小文字 -> 資産）
MAJOR_TOKEN_MAP: dict[str, str] = {
    # Bitcoin 系
    "WBTC": "BTC", "CBBTC": "BTC", "BTCB": "BTC", "TBTC": "BTC",
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "BTC",  # WBTC (Ethereum)
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh": "BTC",  # WBTC (Solana / Wormhole)
    # Ethereum 系
    "WETH": "ETH", "STETH": "ETH", "WSTETH": "ETH", "CBETH": "ETH",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "ETH",
    "0x4200000000000000000000000000000000000006": "ETH",
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": "ETH",
    # Solana 系
    "SOL": "SOL", "WSOL": "SOL", "MSOL": "SOL", "JITOSOL": "SOL",
    WSOL_MINT: "SOL",
    NATIVE_SOL: "SOL",
}

#: 「その他のトークンの売買」をリスクオン/オフの代理指標として結び付けるネイティブ資産
CHAIN_PROXY_ASSET: dict[str, str | None] = {
    SOLANA: "SOL",
    "ethereum": "ETH",
    "base": "ETH",
    "arbitrum": "ETH",
    "bsc": None,      # BNB は MT5 のメジャーではないので代理指標にしない
    "polygon": None,
}

#: 集計のローリングウィンドウ（秒）
SIGNAL_WINDOW_SEC = _env_float("SIGNAL_WINDOW_SEC", 900.0)
#: LONG/SHORT を出すネット金額のしきい値（USD）
SIGNAL_NET_USD_THRESHOLD = _env_float("SIGNAL_NET_USD_THRESHOLD", 50_000.0)
#: しきい値到達に必要な異なるウォレット数
SIGNAL_MIN_WALLETS = _env_int("SIGNAL_MIN_WALLETS", 2)
#: 建玉を閉じるときのしきい値比率（ヒステリシス）
SIGNAL_EXIT_RATIO = _env_float("SIGNAL_EXIT_RATIO", 0.4)
#: メジャー以外のトークン売買をネイティブ資産のシグナルに換算する重み
SIGNAL_PROXY_WEIGHT = _env_float("SIGNAL_PROXY_WEIGHT", 0.5)
#: 同じ資産で連続シグナルを出すまでの最短間隔（秒）
SIGNAL_COOLDOWN_SEC = _env_float("SIGNAL_COOLDOWN_SEC", 300.0)


# --------------------------------------------------------------------------
# MT5 連携（MetaTrader5 Python パッケージ）
# --------------------------------------------------------------------------
#: 実口座への発注を許可するか。既定は False（デモ口座以外では発注しない）
MT5_ALLOW_LIVE = _env_bool("MT5_ALLOW_LIVE", False)
MT5_LOGIN = _env_int("MT5_LOGIN", 0)
MT5_PASSWORD = _env_str("MT5_PASSWORD")          # ログには絶対に出さない
MT5_SERVER = _env_str("MT5_SERVER")
MT5_TERMINAL_PATH = _env_str("MT5_TERMINAL_PATH")  # terminal64.exe のパス（任意）

#: ブローカーごとに銘柄名が違うので候補を順に試す
MT5_SYMBOL_CANDIDATES: dict[str, list[str]] = {
    "BTC": ["BTCUSD", "BTCUSD.", "BTCUSDm", "BTCUSD.cash", "BTCUSD_", "Bitcoin"],
    "ETH": ["ETHUSD", "ETHUSD.", "ETHUSDm", "ETHUSD.cash", "ETHUSD_", "Ethereum"],
    "SOL": ["SOLUSD", "SOLUSD.", "SOLUSDm", "SOLUSD.cash"],
}
#: 環境変数で個別に上書き（例: MT5_SYMBOL_BTC=BTCUSD.pro）
for _asset in MAJOR_ASSETS:
    _override = _env_str(f"MT5_SYMBOL_{_asset}")
    if _override:
        MT5_SYMBOL_CANDIDATES[_asset] = [_override]

#: ロット決定 "risk"（残高に対するリスク%から逆算）/ "fixed"（固定ロット）
MT5_LOT_MODE = _env_str("MT5_LOT_MODE", "fixed").lower()
MT5_FIXED_LOT = _env_float("MT5_FIXED_LOT", 0.01)
MT5_RISK_PCT = _env_float("MT5_RISK_PCT", 0.5)
#: 損切り / 利確（エントリー価格に対する%）。0 で無効
MT5_SL_PCT = _env_float("MT5_SL_PCT", 1.5)
MT5_TP_PCT = _env_float("MT5_TP_PCT", 3.0)
#: 同時に持つポジション数と 1 銘柄あたりの上限ロット
MT5_MAX_POSITIONS = _env_int("MT5_MAX_POSITIONS", 3)
MT5_MAX_LOT = _env_float("MT5_MAX_LOT", 1.0)
#: 反対シグナルが出たら決済してドテンするか
MT5_CLOSE_ON_OPPOSITE = _env_bool("MT5_CLOSE_ON_OPPOSITE", True)
MT5_MAGIC = _env_int("MT5_MAGIC", 20260920)
MT5_DEVIATION = _env_int("MT5_DEVIATION", 20)
#: dry-run 時に使う参考価格（実際の MT5 に接続しない検証用）
MT5_DRYRUN_PRICES = {"BTC": 60_000.0, "ETH": 3_000.0, "SOL": 150.0}


# --------------------------------------------------------------------------
# 保存先 / ログ
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(_env_str("COPYTRADE_DB_PATH", str(BASE_DIR / "trades.db")))
LOG_LEVEL = _env_str("LOG_LEVEL", "INFO").upper()
LOG_FILE = Path(_env_str("COPYTRADE_LOG_FILE", str(BASE_DIR / "copytrade.log")))


# --------------------------------------------------------------------------
# 検証
# --------------------------------------------------------------------------
def validate() -> list[str]:
    """設定の問題点を洗い出して返す（致命的でないものは警告として扱う）。"""
    problems: list[str] = []

    if not WATCHED_WALLETS:
        problems.append("監視対象ウォレットが 0 件です（config.DEFAULT_WALLETS / WATCHED_WALLETS を設定）")
    if len(WATCHED_WALLETS) > MAX_WALLETS:
        problems.append(f"監視対象は最大 {MAX_WALLETS} 件までです")

    seen: set[str] = set()
    for wallet in WATCHED_WALLETS:
        if wallet.chain not in SUPPORTED_CHAINS:
            problems.append(f"未対応チェーン: {wallet.chain} ({wallet.name})")
        if wallet.key in seen:
            problems.append(f"アドレスが重複しています: {wallet.key}")
        seen.add(wallet.key)
        if wallet.chain in EVM_CHAIN_IDS and not wallet.address.startswith("0x"):
            problems.append(f"EVM アドレスの形式が不正: {wallet.address}")

    if COPY_MODE not in {"fixed", "ratio"}:
        problems.append(f"COPY_MODE は fixed / ratio のいずれか: {COPY_MODE}")
    if SELL_MODE not in {"proportional", "full"}:
        problems.append(f"SELL_MODE は proportional / full のいずれか: {SELL_MODE}")
    if DEMO_INITIAL_BALANCE_USD <= 0:
        problems.append("DEMO_INITIAL_BALANCE_USD は正の数で指定してください")
    if MIN_TRADE_USD > MAX_POSITION_USD:
        problems.append("MIN_TRADE_USD が MAX_POSITION_USD を超えています")

    return problems


def missing_credentials() -> list[str]:
    """キーが無く監視できないチェーンを列挙する。"""
    missing: list[str] = []
    chains = {w.chain for w in WATCHED_WALLETS}
    if SOLANA in chains and not HELIUS_API_KEY:
        missing.append("HELIUS_API_KEY 未設定 → Solana は公開 RPC にフォールバック（レート制限あり）")
    if chains & set(EVM_CHAIN_IDS) and not ETHERSCAN_API_KEY:
        missing.append("ETHERSCAN_API_KEY 未設定 → EVM チェーンの監視はスキップされます")
    return missing


def summary() -> str:
    lines = [
        "=== copytrade 設定 ===",
        f"監視ウォレット数 : {len(WATCHED_WALLETS)} / {MAX_WALLETS}",
        f"初期資金         : {DEMO_INITIAL_BALANCE_USD:,.2f} USD",
        f"サイズ決定       : {COPY_MODE} "
        + (f"({COPY_FIXED_USD:,.2f} USD/trade)" if COPY_MODE == "fixed" else f"(x{COPY_RATIO})"),
        f"上限/最小        : max {MAX_POSITION_USD:,.0f} USD / min {MIN_TRADE_USD:,.0f} USD",
        f"売却追随         : {SELL_MODE}",
        f"スリッページ/手数料: {SLIPPAGE_BPS:.0f}bps / {FEE_BPS:.0f}bps",
        f"ポーリング間隔   : {POLL_INTERVAL_SEC:.1f}s",
        f"DB               : {DB_PATH}",
    ]
    for wallet in WATCHED_WALLETS:
        lines.append(f"  - [{wallet.chain:<8}] {wallet.name:<16} {wallet.address}")
    return "\n".join(lines)
