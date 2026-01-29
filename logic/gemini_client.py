import os
import google.generativeai as genai
from typing import Dict, Any, List
from dotenv import load_dotenv
import time
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class GeminiClient:
    """
    A wrapper for the Google Gemini API to handle financial sentiment analysis
    and key person impact scoring.
    Implements Hybrid Intelligence (Flash/Pro) and Robust Error Handling.
    """
    def __init__(self):
        # Force load env vars
        load_dotenv(override=True)
        
        api_key = os.getenv("GOOGLE_API_KEY")
        
        # Streamlit Cloud Specific Fallback
        if not api_key:
            try:
                import streamlit as st
                if "GOOGLE_API_KEY" in st.secrets:
                    api_key = st.secrets["GOOGLE_API_KEY"]
                    print("Success: Found API Key in Streamlit Secrets")
            except Exception as e:
                print(f"Streamlit Secrets lookup failed: {e}")

        # Final Check
        print(f"Loading Env... Key Found: {bool(api_key)}")
        
        if not api_key:
            print("Warning: GOOGLE_API_KEY not found in environment variables OR secrets.")
        
        self.flash_model = None
        self.pro_model = None
        
        if not api_key:
            print("CRITICAL: GOOGLE_API_KEY NOT FOUND. AI features will fail.")
            # Set a flag to handle this gracefully later
            self.api_key_valid = False
        else:
            self.api_key_valid = True

        self.flash_model = None
        self.pro_model = None
        
        if self.api_key_valid:
            try:
                genai.configure(api_key=api_key, transport='rest')
                self._auto_select_models()
            except Exception as e:
                print(f"Error configuring Gemini: {e}")
                self.api_key_valid = False
        
        if not self.api_key_valid:
            # Fallback to prevent crash, but operations will return error messages
            print("Gemini Client initialized in OFFLINE mode.")

    def _auto_select_models(self):
        """Automatically select the best available models."""
        try:
            print("Scanning available Gemini models...")
            available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
            print(f"Available Models: {available_models}")
            
            # --- Flash Selection (Priority: 1.5 Flash > 1.0 Pro) ---
            flash_candidates = [
                "models/gemini-1.5-flash", 
                "models/gemini-1.5-flash-latest",
                "models/gemini-1.5-flash-001",
                "models/gemini-1.5-flash-002",
                "models/gemini-flash",
                "models/gemini-pro" # Fallback to Pro if Flash missing
            ]
            self.flash_model = self._pick_best_model(available_models, flash_candidates)
            print(f"Selected Flash Model: {self.flash_model._model_name}")

            # --- Pro Selection (Priority: 1.5 Pro > 1.0 Pro) ---
            pro_candidates = [
                "models/gemini-1.5-pro", 
                "models/gemini-1.5-pro-latest",
                "models/gemini-1.5-pro-001",
                "models/gemini-1.5-pro-002",
                "models/gemini-pro"
            ]
            self.pro_model = self._pick_best_model(available_models, pro_candidates)
            print(f"Selected Pro Model: {self.pro_model._model_name}")
            
        except Exception as e:
            print(f"Model Auto-Selection Failed: {e}. Falling back to 'gemini-pro'.")
            self.flash_model = genai.GenerativeModel('gemini-pro')
            self.pro_model = genai.GenerativeModel('gemini-pro')

    def _pick_best_model(self, available, candidates):
        # 1. Exact Match
        for c in candidates:
            if c in available:
                return genai.GenerativeModel(c)
        
        # 2. Loose Match (e.g. if candidate is "gemini-1.5-flash" but available is "models/gemini-1.5-flash")
        for c in candidates:
            simple_c = c.replace("models/", "")
            for a in available:
                if simple_c in a:
                    return genai.GenerativeModel(a)
        
        # 3. Last Resort
        return genai.GenerativeModel('gemini-pro')

    def _generate_with_retry(self, model, prompt: str, max_retries: int = 3) -> Any:
        """
        Executes API call with automatic retry logic.
        """
        last_error = None
        for attempt in range(max_retries):
            try:
                response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
                return response
            except Exception as e:
                last_error = e
                str_e = str(e)
                
                # Special Handling for 429 (Rate Limit)
                if "429" in str_e:
                    print(f"API Rate Limit (429) Hit. Cooling down for 10s...")
                    time.sleep(10)
                    wait_time = 2 ** attempt # Still do backoff
                else:
                    wait_time = 2 ** attempt # Exponential backoff: 1s, 2s, 4s
                
                print(f"API Error (Attempt {attempt+1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                
        raise Exception(f"Max retries exceeded. Last error: {last_error}")

    def analyze_sentiment(self, text: str, ticker: str = "", context: str = "", time_context: str = "", model_type: str = "flash") -> Dict[str, Any]:
        """
        Analyzes sentiment using the specified model type (flash or pro).
        Includes automatic fallback from Pro to Flash if Pro fails.
        """
        # Select Model
        active_model = self.pro_model if model_type == "pro" else self.flash_model
        
        # Fallback selection if Pro is requested but not available
        if model_type == "pro" and not self.pro_model:
            print("Pro model requested but not initialized. Falling back to Flash.")
            active_model = self.flash_model
            
        if not active_model:
             # Last resort fallback if primary selection failed
             active_model = self.flash_model or self.pro_model

        if not self.api_key_valid:
            return {"score": 50, "reason": "ERROR: API Key not set in Streamlit Secrets.", "sentiment": "NEUTRAL"}

        if not active_model:
            return {"score": 50, "reason": "AI Models not initialized", "sentiment": "NEUTRAL"}
            
        # User-defined Force Inference Logic & 3-Step Reasoning
        if not text or len(text) < 5:
            # FORCE INFERENCE MODE (No News)
            # 市場が動いていない休日や深夜でも 知能を無理やり起動させる
            prompt = f"""
            Target: {ticker}
            Situation: Latest specific news NOT available.
            Time Context: {time_context}
            Macro Environment: {context}
            
            Mission:
            You are a Bold Hedge Fund Manager. Even without specific news, you MUST predict the likely stock movement for the Next Opening Bell based on:
            1. The Sector's typical correlation with the Macro Environment (e.g. Export sector vs Weak Yen).
            2. The Time Context (Weekends/Holidays trigger gap predictions).
            
            CRITICAL: DO NOT DEFAULT TO NEUTRAL. Take a stance based on the macro trend.
            
            Output valid JSON:
            {{
                "score": Integer 0 (Bearish) to 100 (Bullish),
                "reason": "Macro Inference: [Explain sector correlation & macro trend] (max 120 chars)",
                "sentiment": "BULLISH"/"BEARISH"/"NEUTRAL",
                "stop_loss_reason": "Volatility Risk: [Suggest SL logic based on macro volatility]"
            }}
            """
        else:
            # STANDARD ANALYSIS MODE
            # Pro model gets a more detailed prompt for Stop Loss reasoning
            if model_type == "pro":
                prompt = f"""
                Target: {ticker}
                News Source: {text}
                Time Context: {time_context}
                Macro Environment: {context}
                
                Mission:
                Perform a Deep Analysis of the provided news and market context to predict the stock price movement and suggest a Stop Loss strategy.
                
                Output valid JSON:
                {{
                    "score": Integer 0 (Bearish) to 100 (Bullish),
                    "reason": "Detailed reasoning in Japanese (max 150 chars)",
                    "sentiment": "BULLISH"/"BEARISH"/"NEUTRAL",
                    "stop_loss_reason": "Stop Loss Strategy: [Calculate a logical stop loss % based on volatility risk] (in Japanese)"
                }}
                """
            else:
                # Flash (Speed Mode)
                prompt = f"""
                Target: {ticker}
                News Source: {text}
                Time Context: {time_context}
                Macro Environment: {context}
                
                Mission:
                Analyze the provided news and the current time context to predict the immediate stock price movement.
                
                Output valid JSON:
                {{
                    "score": Integer 0 (Bearish) to 100 (Bullish),
                    "reason": "Brief explanation in Japanese (max 100 chars)",
                    "sentiment": "BULLISH"/"BEARISH"/"NEUTRAL"
                }}
                """
        
        try:
            response = self._generate_with_retry(active_model, prompt)
            import json
            result = json.loads(response.text)
            return result
        except Exception as e:
            print(f"Gemini Sentiment Error ({model_type}): {e}")
            
            # Fallback Logic: If Pro failed, try Flash
            if model_type == "pro" and active_model != self.flash_model and self.flash_model:
                print(">>> FALLBACK TRIGGERED: Pro failed, switching to Flash...")
                return self.analyze_sentiment(text, ticker, context, time_context, model_type="flash")
                
            return {"score": 50, "reason": f"Analysis Failed: {str(e)[:60]}", "sentiment": "NEUTRAL"}

    def analyze_key_person(self, text: str) -> Dict[str, Any]:
        """
        Identifies key persons (e.g., Elon Musk, Sam Altman, Kazuo Ueda) and scores their impact.
        Returns 'person_name', 'impact_score' (0-100), and 'summary'.
        Uses Flash for speed.
        """
        model = self.flash_model or self.pro_model
        if not model:
            return {"person_name": "None", "impact_score": 0, "summary": "API Not Configured"}

        prompt = f"""
        Analyze the following text for mentions of key influential figures in finance/tech (e.g., Elon Musk, Sam Altman, Central Bank Governors).
        If no key person is found, return "None" for person_name.
        
        Text: "{text}"
        
        Output valid JSON only:
        - "person_name": Name of the key person found (or "None").
        - "impact_score": 0-100 indicating the potential market impact of their statement/action.
        - "summary": Brief description (in Japanese) of their action (max 1 sentence).
        """
        
        try:
            response = self._generate_with_retry(model, prompt)
            import json
            result = json.loads(response.text)
            return result
        except Exception as e:
            print(f"Gemini KeyPerson Error: {e}")
            return {"person_name": "None", "impact_score": 0, "summary": f"Error: {str(e)[:40]}"}

    def discover_market_movers(self, news_text: str) -> List[Dict[str, Any]]:
        """
        Scans the provided news text to automatically discover key market movers.
        Returns a list of identified persons and their impacts.
        Uses Flash for speed.
        """
        model = self.flash_model or self.pro_model
        
        if not self.api_key_valid:
            return [{"person": "Setup Error", "asset": "API", "impact": 0, "strategy": "Error", "reason": "API Keyが設定されていません。StreamlitのSecretsを設定してください。"}]

        if not model:
            return []

        prompt = f"""
        You are a top-tier hedge fund AI. Analyze the following news text to identify key influential figures (CEOs, Central Bankers, Politicians) who are currently moving the market.
        
        News Text: "{news_text}"
        
        Identify up to 3 key persons. For each, provide:
        1. Name
        2. Related Stock/Asset (Ticker if possible, e.g. TSLA, USD/JPY)
        3. Market Impact Score (0-100)
        4. Trading Strategy (Buy/Sell/Wait) based on their recent actions.
        5. Reason (Brief explanation in Japanese).
        
        Output a valid JSON list of objects. Example:
        [
            {{
                "person": "Elon Musk",
                "asset": "TSLA",
                "impact": 95,
                "strategy": "Buy on Dip",
                "reason": "新モデルの発表を示唆しており期待感が高まっているため。"
            }}
        ]
        """
        
        try:
            response = self._generate_with_retry(model, prompt)
            import json
            result = json.loads(response.text)
            # Ensure it's a list
            if isinstance(result, list):
                return result
            elif isinstance(result, dict):
                return [result]
            return []
        except Exception as e:
            print(f"Gemini Discover Error: {e}")
            # Return dummy error object to show in UI
            return [{"person": "System Error", "asset": "API", "impact": 0, "strategy": "Wait", "reason": f"{str(e)[:60]}"}]

    def generate_10_search_queries(self, ticker: str, is_us_stock: bool = False) -> List[str]:
        """
        Generates 10 broad & specific search queries for massive information capture.
        Includes Competitors, Tech, Risks, and English keywords for US stocks.
        Uses Flash for speed.
        """
        model = self.flash_model or self.pro_model
        if not model:
            return [ticker] * 10
            
        lang_instruction = "For US stocks, include 5 English queries." if is_us_stock else "All in Japanese."
        
        prompt = f"""
        Generate 10 distinct Google News search queries for ticker: "{ticker}".
        
        Categories:
        1. Official Name + News
        2. Industry Trend (Broad)
        3. Specific Product/Tech
        4. Main Competitor Comparison
        5. Regulatory/Legal Risk
        6. Supply Chain/Macro Impact
        7. CEO/Leadership Statements
        8. Earnings/Financials Keyword
        9. Sentiment Keyword (e.g. "Crash" or "Surge")
        10. Global Context (English if US stock)
        
        {lang_instruction}
        Output valid JSON only: ["q1", "q2", ..., "q10"]
        """
        
        try:
            response = self._generate_with_retry(model, prompt)
            import json
            queries = json.loads(response.text)
            if isinstance(queries, list):
                return queries[:10]
            return [ticker] # Fallback
        except Exception as e:
            print(f"Gemini 10-Query Gen Error: {e}")
            return [ticker, f"{ticker} news", f"{ticker} forecast"]

    def infer_from_macro(self, ticker: str, market_context: str) -> Dict[str, Any]:
        """
        Forced Inference: Generates a prediction based on Macro Data alone when no specific news exists.
        Uses Pro for reasoning capability if available.
        """
        model = self.pro_model or self.flash_model
        if not model:
            return {"score": 50, "reason": "AI Offline", "sentiment": "NEUTRAL"}
            
        prompt = f"""
        CRITICAL TASK: Zero news found for {ticker}.
        You must infer the likely stock movement based solely on the CURRENT MACRO ENVIRONMENT.
        
        Macro Context:
        {market_context}
        (USD/JPY, Interest Rates, General Sector Sentiment)
        
        Task:
        1. Identify the likely sector of {ticker} (e.g. Export, Tech, Defensive).
        2. Correlate with Macro Context (e.g. Weak Yen -> Exporters UP).
        3. Output a predicted sentiment score.
        
        Output valid JSON:
        {{
            "score": Integer 0-100,
            "reason": "Inferred from Macro: [Reasoning] (max 80 chars)",
            "sentiment": "BULLISH"/"BEARISH"/"NEUTRAL"
        }}
        """
        try:
            response = self._generate_with_retry(model, prompt)
            import json
            return json.loads(response.text)
        except Exception as e:
            return {"score": 50, "reason": f"Macro Inference Failed: {str(e)[:50]}", "sentiment": "NEUTRAL"}

    def analyze_past_statements(self, person_name: str, current_market_context: str) -> Dict[str, Any]:
        """
        Performs 'Back-trace Search': Analyzes past statements of a key person to predict future stance.
        Uses Pro for reasoning.
        """
        model = self.pro_model or self.flash_model
        if not model:
            return {"score": 50, "reason": "AI offline", "hawk_prob": 50}
            
        prompt = f"""
        You are an expert geopolitical and financial analyst.
        Target Person: {person_name}
        Current Market Context: {current_market_context}
        
        Task:
        1. Recall/Search this person's major statements from the LAST 3 MONTHS.
        2. Compare their past tone with the Current Market Context (e.g. If Yen is weak, will they become Hawkish?).
        3. Predict their stance for the UPCOMING event.
        
        Output a valid JSON object:
        {{
            "hawk_prob": Integer 0 (Dovish) to 100 (Hawkish),
            "prediction_score": Integer 0 (Negative Impact) to 100 (Positive Impact on Stocks),
            "reason": "Brief reasoning in Japanese (max 80 chars)"
        }}
        """
        
        try:
            response = self._generate_with_retry(model, prompt)
            import json
            return json.loads(response.text)
        except Exception as e:
            print(f"Gemini Back-trace Error: {e}")
            return {"score": 50, "reason": "Analysis Failed", "hawk_prob": 50}
