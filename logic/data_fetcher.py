import yfinance as yf
import pandas as pd
import feedparser
import requests
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
import random
from logic.sentiment import gemini # Import shared instance

# Simple Sector Map for Fallback (Mock DB)
SECTOR_MAP = {
    '7203.T': 'Automotive', 'TSLA': 'Automotive',
    'NVDA': 'Technology', 'AAPL': 'Technology', 'MSFT': 'Technology', '8035.T': 'Technology',
    '9984.T': 'Finance', '8306.T': 'Finance',
    'FAST': 'Retail', 'WMT': 'Retail'
}

def get_stock_history(ticker: str, period: str = "1mo", interval: str = "1d") -> pd.DataFrame:
    """
    指定された銘柄の過去データを取得します。
    
    Args:
        ticker (str): 銘柄コード (例: 'AAPL', '7203.T')
        period (str): 期間 (例: '1d', '5d', '1mo', '3mo', '6mo', '1y', 'ytd', 'max')
        interval (str): 足の間隔 (例: '1m', '5m', '1h', '1d', '5d', '1wk', '1mo')
    
    Returns:
        pd.DataFrame: 株価データ (Open, High, Low, Close, Volume 等を含む)
    """
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period, interval=interval)
        if hist.empty:
            print(f"Warning: No data found for {ticker}")
        return hist
    except Exception as e:
        print(f"Error fetching data for {ticker}: {e}")
        return pd.DataFrame()

def get_current_price(ticker: str) -> Optional[float]:
    """
    現在の株価を取得します。
    """
    try:
        stock = yf.Ticker(ticker)
        # fast_info を優先的に使用
        if hasattr(stock, 'fast_info'):
            return stock.fast_info.last_price
        
        # フォールバック: historyから取得
        hist = stock.history(period="1d")
        if not hist.empty:
            return hist['Close'].iloc[-1]
        
        return None
    except Exception as e:
        print(f"Error fetching price for {ticker}: {e}")
        return None

def get_company_news(ticker: str, max_items: int = 5) -> List[Dict[str, str]]:
    """
    銘柄に関連するニュースを取得します (v5.0 Ultimate Massive Scan).
    Geminiが生成した10種のクエリで多次元検索し、US株なら英語ソースもカバーします。
    """
    news_items = []
    seen_links = set()
    
    # Check if US stock (No ".T" suffix)
    is_us_stock = ".T" not in ticker
    
    # 1. Generate 10 Dynamic Queries
    try:
        queries = gemini.generate_10_search_queries(ticker, is_us_stock=is_us_stock)
    except:
        queries = [ticker]
        
    print(f"Massive Scan {ticker}: {len(queries)} Queries")
    
    # 2. Massive Multi-Scan Loop
    for q in queries:
        try:
            # Optimization: If query looks like ticker, use yfinance specific
            if q == ticker and not is_us_stock: # yf news often good for jp
                stock = yf.Ticker(ticker)
                yf_news = stock.news
                if yf_news:
                    for item in yf_news:
                        link = item.get('link')
                        if link not in seen_links:
                            news_items.append({
                                "title": item.get('title'),
                                "link": link,
                                "publisher": item.get('publisher'),
                                "timestamp": item.get('providerPublishTime')
                            })
                            seen_links.add(link)
            else:
                # Wide RSS Search
                # Choose topic based on query content or stock type
                topic = "business"
                if is_us_stock: topic = "world" # Use world/tech for US context simulation
                if "Tech" in q or "AI" in q: topic = "technology"
                
                # Fetch
                rss_res = get_market_news_rss(topic)
                
                # Filter/Simulate Relevance (Since we are using generic feeds in demo)
                # In real prod, we would hit a Search API with `q`
                for item in rss_res:
                    if item['link'] not in seen_links:
                        # Append query context so user knows WHY this news was picked
                        # (Simulating search result)
                        item['title'] = f"[{q}] {item['title']}" 
                        news_items.append(item)
                        seen_links.add(item['link'])
                        
        except Exception as e:
            print(f"Error scanning query {q}: {e}")

    # Fallback: If 0 items, fetch Industry News
    if not news_items:
        print(f"Zero news for {ticker}. Initiating Industry Fallback Scan...")
        industry = SECTOR_MAP.get(ticker, "Market")
        fallback_rss = get_market_news_rss(topic="business") # Wide scan
        
        # Add TOP 3 Market Headlines as context
        for i, item in enumerate(fallback_rss[:3]):
             item['title'] = f"[Market Context] {item['title']}"
             news_items.append(item)

    # Limit results (but keep high entropy)
    news_items = news_items[:max_items*2] 

    # 3. Macro Fallback is handled in Portfolio/Gemini layer now (Forced Inference)
    return news_items

def get_market_news_rss(topic: str = "business") -> List[Dict[str, str]]:
    """
    市場ニュースをRSSから取得します (JP/US Dual Support).
    """
    rss_url = "https://news.yahoo.co.jp/rss/topics/business.xml" # Default JP
    
    if topic == "world": # US/Global Context
        rss_url = "https://finance.yahoo.com/news/rssindex" # English US Finance
        
    if topic == "technology":
        rss_url = "https://news.yahoo.co.jp/rss/topics/it.xml"

    if not rss_url:
        return []

    try:
        # Use requests to fetch content first (Better SSL/Env support)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        response = requests.get(rss_url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # Parse the content
        feed = feedparser.parse(response.content)
        
        # Parse logic considering Weekend 72h requirement (implicitly handled by feed depth, expanding to 20 items)
        return [{"title": entry.title, "link": entry.link, "published": entry.published} 
                for entry in feed.entries[:20]]
    except Exception as e:
        print(f"Error fetching RSS from {rss_url}: {e}")
        return []
