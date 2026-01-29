from logic.gemini_client import GeminiClient

# Global Gemini Client instance
gemini = GeminiClient()

def analyze_sentiment(text: str, ticker: str = "", context: str = "", time_context: str = "", model_type: str = "flash") -> dict:
    """
    Uses Gemini API to analyze text sentiment.
    Returns dict: {"score": int, "reason": str}
    """
    return gemini.analyze_sentiment(text, ticker=ticker, context=context, time_context=time_context, model_type=model_type)

def analyze_key_person_impact(text: str) -> dict:
    """
    Uses Gemini API to identify key persons and score their market impact.
    Returns dict: {"person_name": str, "impact_score": int, "summary": str}
    """
    return gemini.analyze_key_person(text)

def discover_market_movers(text: str) -> list:
    """
    Uses Gemini API to discover key market movers from text.
    Returns list of dicts.
    """
    return gemini.discover_market_movers(text)
