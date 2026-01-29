from typing import List, Dict
import pandas as pd
import numpy as np
from logic.technical import calculate_rsi, calculate_macd, check_volume_surge
from logic.data_fetcher import get_company_news
from logic.sentiment import analyze_sentiment, gemini # Ensure gemini instance access
from logic.calendar_fetcher import get_event_context_string
from logic.calendar import get_market_status_check, get_market_state_check
import streamlit as st # For Visual Progress

class Portfolio:
    def __init__(self, initial_balance: float = 100000.0):
        self.balance = initial_balance
        self.positions: Dict[str, Dict] = {} # ticker -> {quantity, avg_price}
        self.history: List[Dict] = []
        
    def add_funds(self, amount: float):
        """資金を入金"""
        if amount > 0:
            self.balance += amount
            self.history.append({"type": "DEPOSIT", "amount": amount, "balance": self.balance})

    def recalculate_allocation(self):
        """
        Notify that funds changed, triggering a UI update.
        In Streamlit execution flow, simply updating state is enough.
        """
        pass

    def get_total_value(self, current_prices: Dict[str, float]) -> float:
        """現在の評価額合計（現金 + 保有株）を計算"""
        stock_value = 0.0
        for ticker, data in self.positions.items():
            price = current_prices.get(ticker, data['avg_price']) # 価格が取れない場合は取得単価で代用
            stock_value += price * data['quantity']
        return self.balance + stock_value

def calculate_compound_interest(principal: float, monthly_contribution: float, rate_of_return: float, years: int) -> pd.DataFrame:
    """
    複利シミュレーション
    
    Args:
        principal: 初期元本
        monthly_contribution: 毎月の積立額
        rate_of_return: 年利 (例: 0.10 で 10%)
        years: 期間 (年)
        
    Returns:
        pd.DataFrame: 年ごとの資産推移
    """
    data = []
    current_amount = principal
    total_invested = principal
    
    monthly_rate = rate_of_return / 12
    
    for month in range(1, years * 12 + 1):
        current_amount = (current_amount + monthly_contribution) * (1 + monthly_rate)
        total_invested += monthly_contribution
        
        if month % 12 == 0:
            year = month // 12
            data.append({
                "年": year,
                "総資産額": round(current_amount, 2),
                "元本": round(total_invested, 2),
                "利益": round(current_amount - total_invested, 2)
            })
            
    return pd.DataFrame(data)

# List of ~100 Major Global Growth & Value Stocks (Mocking a database)
GLOBAL_REQ_STOCKS = [
    # US Tech / Growth
    "NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "AMD", "NFLX", "PLTR",
    "CRWD", "PANW", "SNOW", "UBER", "ABNB", "COIN", "MSTR", "SMCI", "ARM", "INTC",
    "QCOM", "AVGO", "TXN", "MU", "LRCX", "AMAT", "KLAC", "ADBE", "CRM", "ORCL",
    "IBM", "CSCO", "NOW", "INTU", "SQ", "PYPL", "SHOP", "SE", "DDOG", "NET",
    # JP Majors / Nikkei 225 Leaders
    "7203.T", "9984.T", "6861.T", "8035.T", "6758.T", "6501.T", "6920.T", "7741.T", "6954.T", "6367.T",
    "6146.T", "4063.T", "6981.T", "6971.T", "6762.T", "7974.T", "9983.T", "4568.T", "4519.T", "6098.T",
    "8306.T", "8316.T", "8411.T", "8766.T", "8058.T", "8001.T", "8031.T", "8053.T", "7011.T", "7012.T",
    # Defense / Industrial / Energy
    "LMT", "RTX", "BA", "CAT", "DE", "XOM", "CVX", "COP", "SLB", "EOG",
    # Consumer / Retail
    "WMT", "COST", "TGT", "HD", "LOW", "NKE", "MCD", "SBUX", "DIS", "KO"
]

def pre_screen_stocks(pool: List[str]) -> List[pd.Series]:
    """
    Stage 1: Fast Technical Scan.
    Returns a sorted list of candidate dataframes/dicts based on activity (Vol/RSI)
    to select the Top 30 for deep analysis.
    """
    candidates = []
    import random
    
    for ticker in pool:
        # Simulate Fast Calc (In real app, fetch batch data here)
        # We need to return the 't_score' pre-calculated
        rsi = calculate_rsi(ticker) # Mock
        macd = calculate_macd(ticker) # Mock
        is_surging = check_volume_surge(ticker) # Mock
        
        score = 0
        if is_surging: score += 50
        if rsi < 30 or rsi > 70: score += 30
        if macd > 60: score += 20
        
        # Random noise for market variance
        score += random.randint(-10, 10)
        
        candidates.append({
            "ticker": ticker,
            "pre_score": score,
            "rsi": rsi,
            "macd": macd,
            "is_surging": is_surging
        })
        
    # Sort by 'pre_score' descending
    candidates.sort(key=lambda x: x['pre_score'], reverse=True)
    return candidates # Return all, we slice later

def get_best_stocks(candidates_input: List[str], sentiment_scores: Dict[str, int], technical_scores: Dict[str, int], progress_callback=None, status_callback=None) -> List[Dict]:
    """
    ベスト銘柄を選定する (v4.0 Hybrid Intelligence Two-Stage Rocket)
    1. 全100銘柄をテクニカルで軽量スキャン (Pre-Screen)
    2. 上位30銘柄を 'gemini-1.5-flash' で高速スキャン (Stage 1)
    3. 上位5銘柄を 'gemini-1.5-pro' で精密分析 & 損切ライン算出 (Stage 2)
    """
    import random
    
    # 1. Smart Filtering (Stage 1)
    # Scan the WHOLE global pool
    all_candidates = pre_screen_stocks(GLOBAL_REQ_STOCKS)
    
    # Take Top 5 "Active" stocks for Stage 1 Analysis (Flash) to avoid API Rate Limits (429)
    deep_analysis_pool = all_candidates[:5]
    
    ranked_stocks = []
    
    # Pre-fetch Event Context (shared for all stocks)
    event_context = get_event_context_string()
    
    # Check Market Status (Warning Mode)
    market_status = get_market_status_check()
    is_warning_mode = market_status['status'] == "WARNING"
    
    # Check Market STATE (Weekend/Open/Closed) for Temporal Logic
    market_state_info = get_market_state_check()
    time_context_msg = market_state_info['message'] 
    
    # Setup Callbacks
    if not progress_callback:
        internal_bar = st.progress(0)
        progress_callback = internal_bar.progress
    if not status_callback:
        internal_status = st.empty()
        status_callback = internal_status.text
    
    total_candidates = len(deep_analysis_pool)
    
    # --- STAGE 1: Flash Scan (Speed) ---
    for i, candidate in enumerate(deep_analysis_pool):
        ticker = candidate['ticker']
        # Progress 30% -> 80%
        pct_base = 30 + int((i / total_candidates) * 50)
        progress_callback(pct_base)
        status_callback(f"1次スキャン(Flash) {ticker} ... 高速スクリーニング中")
        
        # Analyze with Flash
        analyzed_data = analyze_single_stock(
            ticker, 
            candidate, 
            event_context, 
            market_status, 
            time_context_msg,
            status_callback,
            model_type="flash" 
        )
        ranked_stocks.append(analyzed_data)
        
    # Sort by Score and Take Top 5
    ranked_stocks.sort(key=lambda x: x['score'], reverse=True)
    top_5_candidates = ranked_stocks[:5]
    
    final_top_5 = []
    
    # --- STAGE 2: Pro Analysis (Intelligence) ---
    for i, stock in enumerate(top_5_candidates):
        ticker = stock['ticker']
        # Progress 80% -> 100%
        pct_base = 80 + int((i / 5) * 20)
        progress_callback(pct_base)
        status_callback(f"最終分析(Pro) {ticker} ... 損切ラインと詳細理由を生成中")
        
        # Re-run analysis with Pro for deep reasoning
        # Reuse available data, just re-trigger AI
        deep_data = analyze_single_stock(
            ticker, 
            None, # Use None to force re-calc or just pass stock data if we handled it? 
                  # Actually analyze_single_stock expects candidate_data format or None.
                  # Let's pass 'stock' as candidate_data but it has extra fields. It should be fine.
            event_context,
            market_status,
            time_context_msg,
            status_callback,
            model_type="pro"
        )
        
        # Merge Pro data (reasoning) with some previous data if needed (technical is same)
        final_top_5.append(deep_data)
        
    # Sort Final Top 5 again just in case Pro changed scores
    final_top_5.sort(key=lambda x: x['score'], reverse=True)
    
    # Cleanup Visuals
    if 'internal_bar' in locals():
        internal_bar.empty()
    if 'internal_status' in locals():
        internal_status.empty()
    
    return final_top_5

def analyze_single_stock(ticker: str, candidate_data: Dict, event_context: str, market_status: Dict, time_context_msg: str, status_callback=None, model_type: str = "flash") -> Dict:
    """
    単一銘柄のAI分析を実行する
    model_type: 'flash' (高速) or 'pro' (高精度)
    """
    import random
    
    if status_callback is None:
        def status_callback(msg): pass

    # Data Unpacking or Defaulting
    if candidate_data:
        is_surging = candidate_data.get('is_surging', False)
        # Handle cases where candidate_data is already a full result object
        rsi_val = candidate_data.get('rsi', 50) 
        if 'technical_score' in candidate_data: # If it's a re-run
             # We can't easily extract raw MACD unless we stored it. 
             # For re-runs, we might just re-calc mock technicals or trust previous.
             # Simplified: Re-calc/Mock technicals is cheap.
             pass
        macd_val = candidate_data.get('macd', 0)
    else:
        # If no pre-calculated data 
        is_surging = check_volume_surge(ticker)
        rsi_val = calculate_rsi(ticker)
        macd_val = calculate_macd(ticker)

    # Determine currency
    is_jpy = ".T" in ticker
    currency_symbol = "¥" if is_jpy else "$"
    
    # Base Price Simulation
    if is_jpy:
        base_price = 3000 + random.uniform(0, 30000) 
    else:
        base_price = 50 + random.uniform(0, 500) 
    current_price = base_price * (1 + random.uniform(-0.02, 0.02))

    # Re-calc Technical Score for Report
    t_score = 50
    tech_reason = []
    
    if is_surging:
        t_score += 15
        tech_reason.append("出来高急増中(機関投資家の参入可能性)")
        
    if rsi_val < 30:
        t_score += 20
        tech_reason.append(f"RSI売られすぎ({rsi_val})")
    elif rsi_val > 70:
        t_score -= 10 
        tech_reason.append(f"RSI過熱感あり({rsi_val})")
        
    if macd_val > 60:
        t_score += 20
        tech_reason.append("MACD好転")
        
    t_score = max(0, min(100, t_score)) 
    
    # AI Sentiment & Event Check
    s_score = 50 
    ai_reason = "AI分析中..."
    stop_loss_reason = ""
    news_source = "Market Context"
    
    # Fetch News
    # Only fetch news if not provided (optimally we should cache news too, but fetch is fast enough or mocked)
    if candidate_data and 'news_source' in candidate_data and candidate_data['news_source'] != "Market Context" and candidate_data['news_source'] != "Macro AI Inference":
         # Maybe reuse? But we want fresh analysis for Pro. 
         # Let's fetch freshly for simplicity.
         pass

    news = get_company_news(ticker, max_items=5) 
    
    if news:
        news_text = " ".join([n['title'] for n in news if n.get('title')])
        news_source = news[0].get('publisher', 'System Context')
        
        # Inject Event Context into Analysis
        combined_text = f"News {news_text} Events {event_context}"
        
        if model_type == "pro":
             status_callback(f"精密分析(Pro) {ticker} ... ニュースとマクロから深層推論・損切ライン算出")
        
        analysis = analyze_sentiment(
            combined_text[:2500], 
            ticker=ticker,
            context=event_context,
            time_context=time_context_msg,
            model_type=model_type
        )
        s_score = analysis.get('score', 50)
        ai_reason = analysis.get('reason', '詳細生成中...')
        stop_loss_reason = analysis.get('stop_loss_reason', '') # Only Pro returns this typically
        
    else:
         # FORCED MACRO INFERENCE (Zero News)
         status_callback(f"分析中 {ticker} ... ニュースなし ({model_type}) マクロ環境から推論")
         
         # Pass empty text to trigger specific Forced Inference Prompt
         analysis = analyze_sentiment(
            "", 
            ticker=ticker, 
            context=event_context,
            time_context=time_context_msg,
            model_type=model_type
         )
         s_score = analysis.get('score', 50)
         ai_reason = analysis.get('reason', 'Macro Inference') + " (推論)"
         stop_loss_reason = analysis.get('stop_loss_reason', '')
         news_source = "Macro AI Inference"

    # Weighted Score (v4.5 Future Weighting)
    # Future/Macro Score is dominant (60%)
    total_score = (s_score * 0.60) + (t_score * 0.40)
    
    # --- Event Gravity (Volatility Adjustment) ---
    upside_mult = 1.15
    downside_mult = 0.95 # -5% SL default
    
    is_warning_mode = market_status['status'] == "WARNING"

    if is_warning_mode:
        upside_mult = 1.05  
        downside_mult = 0.97 
        total_score -= 20   
        final_reason = f"⚠️ {market_status['message']} (不確実性回避のため 目標/損切 を調整)"
    else:
        final_reason = "" # Init

    # Determine advice based on score
    target_price = 0.0
    stop_loss_price = 0.0
    rec_order_type = "成行"
    rec_entry_price = current_price
    
    fmt_price = lambda p: f"{currency_symbol}{p:,.0f}" if is_jpy else f"{currency_symbol}{p:,.2f}"
    
    action = "様子見"
    final_reason += f"【テクニカル】{', '.join(tech_reason)} 【AI分析】{ai_reason}"
    
    if is_warning_mode:
         action = "様子見 (警戒)"
         if total_score < 40:
             action = "リスク回避 (Sell)"

    elif total_score >= 80:
        action = "成行買い (Strong Buy)"
        rec_order_type = "成行"
        rec_entry_price = current_price
        
    elif total_score >= 65:
        action = "押し目買い (Accumulate)"
        rec_order_type = "指値"
        rec_entry_price = current_price * 0.98
        # Adjust entry for accumulation
        if not is_warning_mode:
            upside_mult = 1.08
        
    elif total_score >= 50:
         action = "中立 (Hold)"
         upside_mult = 1.02
    else:
         action = "売り推奨 (Sell)"
         upside_mult = 0.90 
         downside_mult = 1.02 

    # Calculate Final Targets with Gravity applied
    target_price = current_price * upside_mult
    stop_loss_price = current_price * downside_mult
    
    # If Pro generated a Stop Loss reason/suggestion, we might want to adjust SL price.
    # For now, we will just display the reason. Advanced logic could parse "5%" from text.
        
    return {
        "ticker": ticker,
        "score": total_score,
        "sentiment_score": s_score,
        "technical_score": t_score,
        "current_price": current_price,
        "display_price": fmt_price(current_price),
        "display_target": fmt_price(target_price),
        "display_sl": fmt_price(stop_loss_price),
        "reason": final_reason,
        "stop_loss_reason": stop_loss_reason, # New Field
        "action": action,
        "target_price": target_price,
        "stop_loss_price": stop_loss_price,
        "rec_order_type": rec_order_type,
        "rec_entry_price": rec_entry_price,
        "news_source": news_source,
        "is_surging": is_surging 
    }

def calculate_ai_performance(years: int = 1) -> pd.DataFrame:
    """
    もし過去にAI推奨通りに売買していた場合のパフォーマンス（デモ用）
    """
    import random
    
    dates = pd.date_range(end=pd.Timestamp.now(), periods=years*12, freq='M')
    ai_portfolio = [1000000] # Start with 1M JPY
    market_benchmark = [1000000]
    
    for i in range(1, len(dates)):
        # AI strategy: mostly wins, small losses
        ai_change = random.uniform(-0.02, 0.08) 
        market_change = random.uniform(-0.05, 0.05)
        
        ai_portfolio.append(ai_portfolio[-1] * (1 + ai_change))
        market_benchmark.append(market_benchmark[-1] * (1 + market_change))
        
    return pd.DataFrame({
        "Date": dates,
        "AI資産推移": [round(x) for x in ai_portfolio],
        "市場平均": [round(x) for x in market_benchmark]
    })

def calculate_position_size(balance: float, price: float, risk_pct: float = 0.05) -> Dict:
    """
    資金管理に基づく推奨購入数を計算
    """
    target_amount = balance * risk_pct
    if price <= 0: return {"qty": 0, "amount": 0}
    
    qty = int(target_amount // price)
    if qty == 0 and target_amount > 0: qty = 1 # Minimum 1 if funds allow
    
    return {
        "qty": qty,
        "amount": qty * price,
        "pct": risk_pct * 100
    }
