from logic.calendar_fetcher import CalendarFetcher
import datetime
from logic.calendar_fetcher import CalendarFetcher
import datetime
from typing import Dict, Any, List

class EventLogic:
    def __init__(self):
        self.fetcher = CalendarFetcher()
        
    def get_market_status(self) -> Dict[str, str]:
        """
        Checks for upcoming high-importance events within 24 hours.
        Returns: {"status": "NORMAL" | "WARNING", "message": str}
        """
        next_event = self.fetcher.get_next_major_event()
        if not next_event:
            return {"status": "NORMAL", "message": ""}
            
        today_date = datetime.date.today()
        
        # Parse event date
        try:
            event_date = datetime.datetime.strptime(next_event['date'], "%Y-%m-%d").date()
            days_left = (event_date - today_date).days
            
            # Warning condition: 0 or 1 day left for High Importance event
            if days_left <= 1 and next_event['importance'] == 'High':
                return {
                    "status": "WARNING",
                    "message": f"🚨 {next_event['title']} が接近中 (残り{days_left}日)。ポジション調整を推奨。"
                }
                
        except Exception as e:
            print(f"Calendar Logic Error: {e}")
            
        return {"status": "NORMAL", "message": ""}

    def get_upcoming_key_people(self) -> List[str]:
        """
        Extracts key people or entities from upcoming events for Back-trace Search.
        e.g. "BOJ Gov Ueda Speaks" -> ["Kazuo Ueda"]
        """
        key_people = set()
        # Mock logic: In real app, we parse self.fetcher.get_next_major_event() or list
        # For demo, we verify if specific keywords exist in the next event
        
        next_event = self.fetcher.get_next_major_event()
        if not next_event:
            return []
            
        title = next_event.get('title', '')
        
        if "Ueda" in title or "BOJ" in title:
            key_people.add("Kazuo Ueda")
        if "Powell" in title or "Fed" in title or "FOMC" in title:
            key_people.add("Jerome Powell")
        if "Lagarde" in title or "ECB" in title:
            key_people.add("Christine Lagarde")
            
        return list(key_people)

    def get_current_state(self) -> Dict[str, str]:
        """
        Determines the current market state (WEEKEND, OPEN, CLOSED).
        Assumes JST (Japan Standard Time) for simplicity or system time.
        """
        now = datetime.datetime.now()
        weekday = now.weekday() # 0=Mon, 6=Sun
        hour = now.hour
        
        # Weekend: Saturday (5) or Sunday (6)
        if weekday >= 5:
            return {
                "state": "WEEKEND", 
                "message": "週末戦略モード (Weekend Strategy)",
                "label": "月曜日の注目株"
            }
            
        # JP Market Open: Mon-Fri 09:00 - 15:00
        # (Simplified, ignoring lunch break for system logic)
        # 1. JP Market
        if 9 <= hour < 15:
            return {
                "state": "OPEN", 
                "message": "東京市場 開場中 (Real-time Scan)",
                "label": "今買うべき日本株"
            }
            
        # 2. US Market (Approx 23:30 - 06:00 JST)
        # Handling the day crossover is tricky with simple hour checks.
        # Late Night (23, 0, 1, 2, 3, 4, 5) roughly covers it.
        # Also 22:00 (Pre-market)
        is_us_open = (hour >= 22) or (hour < 6)
        
        # Check if it's weekend logic for US (Fri Night JST is Sat Morning logic? No, Fri Night JST is Fri Morning US)
        # Sat Morning JST (00:00 - 06:00) is Fri Afternoon US -> Still Open
        # Sun Morning JST is Sat Afternoon US -> Closed
        
        # Simple heuristic:
        # If Weekday (0-4) AND (Hour >= 22 or Hour < 6), it's potentially US Open.
        # Special case: Monday Morning < 6 is Sunday US -> Closed.
        # Special case: Saturday Morning < 6 is Friday US -> Open.
        
        is_weekday_us = True
        if weekday == 0 and hour < 6: is_weekday_us = False # Mon Morning JST = Sun US
        if weekday == 5 and hour >= 6: is_weekday_us = False # Sat Day JST = Fri Night US (Closed)
        if weekday == 6: is_weekday_us = False # Sun JST = Sat US
        
        if is_us_open and is_weekday_us:
             return {
                "state": "OPEN", 
                "message": "米国市場 開場中 (US Market Open)",
                "label": "今買うべき米国株"
            }

        # Default: Closed (Weekdays but outside hours)
        return {
            "state": "CLOSED", 
            "message": "市場閉鎖中 (After-Hours Analysis)",
            "label": "翌営業日の注目株"
        }

# Singleton convenience
def get_market_status_check() -> Dict[str, str]:
    logic = EventLogic()
    return logic.get_market_status()

def get_market_state_check() -> Dict[str, str]:
    logic = EventLogic()
    return logic.get_current_state()

def get_key_people_check() -> List[str]:
    logic = EventLogic()
    return logic.get_upcoming_key_people()
