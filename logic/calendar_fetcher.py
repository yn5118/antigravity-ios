from typing import List, Dict
import datetime

class CalendarFetcher:
    """
    Fetches major economic events and VIP schedules (Central Bank Governors, Key CEOs).
    Uses a static definition for critical near-term events (simulated for reliability)
    and can be extended to parse RSS feeds.
    """
    
    def __init__(self):
        # Static list of major events (Mocking recent/upcoming events for demo relevance)
        # In a real production app, this would be scraped or fetched via API (e.g. Alpha Vantage, Investing.com)
        self.static_events = [
            {
                "date": "2026-01-28",
                "time": "28:00", # JST converted implies next day early morning
                "title": "FOMC 政策金利発表 (Federal Funds Rate)",
                "importance": "High",
                "country": "US",
                "type": "CentralBank"
            },
            {
                "date": "2026-01-28",
                "time": "28:30",
                "title": "パウエル議長 定例記者会見",
                "importance": "High",
                "country": "US",
                "type": "VIP"
            },
            {
                "date": "2026-02-06",
                "time": "22:30",
                "title": "米 雇用統計 (Non-Farm Payrolls)",
                "importance": "High",
                "country": "US",
                "type": "Economic"
            },
            {
                "date": "2026-01-30",
                "time": "08:30",
                "title": "東京消費者物価指数 (CPI)",
                "importance": "Medium",
                "country": "JP",
                "type": "Economic"
            },
            {
                "date": "2026-02-14",
                "time": "15:30",
                "title": "植田日銀総裁 講演",
                "importance": "High",
                "country": "JP",
                "type": "VIP"
            }
        ]

    def get_upcoming_events(self, limit: int = 5) -> List[Dict]:
        """
        Returns a sorted list of upcoming events.
        """
        today = datetime.date.today()
        # Mocking today as Jan 24, 2026 for demo consistency with 'today' checks
        # In real app: today = datetime.date.today()
        
        # Sort by date
        sorted_events = sorted(self.static_events, key=lambda x: x['date'])
        
        # Filter future events (simple string comparison works for YYYY-MM-DD)
        future_events = [
            e for e in sorted_events 
            if e['date'] >= today.strftime("%Y-%m-%d")
        ]
        
        return future_events[:limit]

    def get_next_major_event(self) -> Dict:
        """
        Returns the single most important next event for the Dashboard widget.
        """
        events = self.get_upcoming_events(limit=10)
        # Prioritize 'High' importance
        high_imp = [e for e in events if e['importance'] == 'High']
        
        if high_imp:
            return high_imp[0]
        elif events:
            return events[0]
        else:
            return None

def get_event_context_string() -> str:
    """
    Returns a string summary of upcoming events for AI Context.
    e.g. "Upcoming: FOMC (1/28), Non-Farm Payrolls (2/6)"
    """
    fetcher = CalendarFetcher()
    events = fetcher.get_upcoming_events(limit=3)
    if not events:
        return "直近の重要経済イベントはありません。"
        
    summary = "直近の重要イベント: "
    summary += ", ".join([f"{e['title']} ({e['date']})" for e in events])
    return summary
