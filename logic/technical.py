import pandas as pd
import pandas_ta as ta

def add_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    DataFrame にテクニカル指標を追加します。
    以下の指標を計算します:
    - RSI (14)
    - MACD (12, 26, 9)
    - SMA (20, 50, 200)
    - Bollinger Bands (20, 2)
    
    Args:
        df (pd.DataFrame): 'Close', 'High', 'Low' 等を含む株価データ
    
    Returns:
        pd.DataFrame: テクニカル指標が追加された DataFrame
    """
    if df.empty:
        return df
    
    # データのコピーを作成して警告を回避
    df = df.copy()

    # RSI
    df['RSI'] = ta.rsi(df['Close'], length=14)
    
    # MACD
    macd = ta.macd(df['Close'], fast=12, slow=26, signal=9)
    if macd is not None:
        df = pd.concat([df, macd], axis=1)
        # macd のカラム名は通常 MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9 となるため、使いやすくリネームも検討可能だが、
        # pandas_ta のデフォルトに従う方が汎用性が高い。
        # 一般的な名前: 
        # MACD_12_26_9 -> MACD Line
        # MACDs_12_26_9 -> Signal Line
        # MACDh_12_26_9 -> Histogram
    
    # SMA
    df['SMA_20'] = ta.sma(df['Close'], length=20)
    df['SMA_50'] = ta.sma(df['Close'], length=50)
    df['SMA_200'] = ta.sma(df['Close'], length=200)
    
    # Bollinger Bands
    bbands = ta.bbands(df['Close'], length=20, std=2)
    if bbands is not None:
        df = pd.concat([df, bbands], axis=1)
        # BBL_20_2.0, BBM_20_2.0, BBU_20_2.0
    
    return df

def calculate_signal(df: pd.DataFrame) -> str:
    """
    簡易的な売買シグナルを判定します (デモ用)。
    RSI と MACD を使用。
    """
    if df.empty or len(df) < 2:
        return "NEUTRAL"
    
    latest = df.iloc[-1]
    
    # RSI check
    rsi_signal = "NEUTRAL"
    if latest['RSI'] < 30:
        rsi_signal = "BUY"
    elif latest['RSI'] > 70:
        rsi_signal = "SELL"
        
    # MACD Cross check logic would go here (requires previous row)
    
    return rsi_signal

def calculate_rsi(ticker: str) -> int:
    """
    Simulates calculating RSI for a single stock.
    In a real system, this would fetch history and use ta.rsi().
    """
    import random
    # Mocking for demo speed/stability
    return random.randint(30, 80)

def calculate_macd(ticker: str) -> int:
    """
    Simulates calculating MACD score for a single stock.
    Return 0-100 score indicating bullishness.
    """
    import random
    return random.randint(30, 80)

def check_volume_surge(ticker: str) -> bool:
    """
    Checks if the stock is experiencing significant volume surge (e.g. > 1.5x of 20-day avg).
    Returns True if surging.
    """
    import random
    # In real app:
    # df = get_stock_history(ticker, period="1mo")
    # if df.empty: return False
    # avg_vol = df['Volume'].mean()
    # current_vol = df['Volume'].iloc[-1]
    # return current_vol > avg_vol * 1.5
    
    # Mock for demo "action"
    return random.random() < 0.3 # 30% chance of surge
