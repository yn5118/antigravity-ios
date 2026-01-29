import datetime
from typing import Dict, List, Tuple, Any
import json
import os

class DemoAccount:
    """
    Simulates a trading account for demo trading functionality.
    Handles balance, positions, and order execution.
    """
    def __init__(self, initial_balance: float = 10000000.0):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.positions: Dict[str, Dict[str, Any]] = {}  # {ticker: {'quantity': int, 'avg_price': float}}
        self.trade_history: List[Dict[str, Any]] = []

    def reset(self, new_balance: float = None):
        """Resets the account to initial state."""
        if new_balance is not None:
            self.initial_balance = new_balance
        self.balance = self.initial_balance
        self.positions = {}
        self.trade_history = []

    def get_portfolio_value(self, current_prices: Dict[str, float]) -> float:
        """
        Calculates total portfolio value (Cash + Market Value of Positions).
        current_prices: dictionary mapping ticker to current price
        """
        position_value = 0.0
        for ticker, pos in self.positions.items():
            price = current_prices.get(ticker, pos['avg_price'])  # Fallback to avg price if current not found
            position_value += price * pos['quantity']
        return self.balance + position_value

    def calculate_unrealized_pl(self, current_prices: Dict[str, float]) -> Dict[str, Any]:
        """It calculates unrealized profit/loss for the entire portfolio."""
        total_pl = 0.0
        details = []
        
        for ticker, pos in self.positions.items():
            current_price = current_prices.get(ticker, pos['avg_price'])
            avg_price = pos['avg_price']
            qty = pos['quantity']
            
            val = current_price * qty
            cost = avg_price * qty
            pl = val - cost
            pl_pct = (pl / cost) * 100 if cost > 0 else 0
            
            total_pl += pl
            details.append({
                "ticker": ticker,
                "quantity": qty,
                "avg_price": avg_price,
                "current_price": current_price,
                "pl": pl,
                "pl_pct": pl_pct
            })
            
        return {
            "total_pl": total_pl,
            "details": details
        }

    def execute_order(self, ticker: str, side: str, quantity: float, price: float, order_type: str = "MARKET", tp: float = None, sl: float = None) -> Tuple[bool, str]:
        """
        Executes a simulated order.
        side: "BUY" or "SELL"
        """
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total_cost = quantity * price
        
        if side == "BUY":
            if total_cost > self.balance:
                return False, f"資金不足です。必要額: {total_cost:,.0f}, 残高: {self.balance:,.0f}"
            
            self.balance -= total_cost
            
            if ticker in self.positions:
                current_qty = self.positions[ticker]['quantity']
                current_avg = self.positions[ticker]['avg_price']
                # Calculate new weighted average price
                new_avg = ((current_qty * current_avg) + total_cost) / (current_qty + quantity)
                self.positions[ticker]['quantity'] += quantity
                self.positions[ticker]['avg_price'] = new_avg
                # Update TP/SL if provided, otherwise keep existing or set defaults? 
                # Converting to "Strategy" -> Latest order updates TP/SL for simplest UX
                if tp: self.positions[ticker]['tp'] = tp
                if sl: self.positions[ticker]['sl'] = sl
            else:
                self.positions[ticker] = {
                    'quantity': quantity, 
                    'avg_price': price,
                    'tp': tp,
                    'sl': sl
                }
                
        elif side == "SELL":
            if ticker not in self.positions:
                return False, "この銘柄は保有していません。"
            if self.positions[ticker]['quantity'] < quantity:
                return False, f"保有数量が不足しています。保有: {self.positions[ticker]['quantity']}"
            
            self.balance += total_cost
            self.positions[ticker]['quantity'] -= quantity
            
            if self.positions[ticker]['quantity'] <= 0:
                del self.positions[ticker]
                
        else:
            return False, "無効な取引タイプです(BUY/SELLのみ)。"

        # Record History
        self.trade_history.append({
            "timestamp": timestamp,
            "ticker": ticker,
            "side": side,
            "quantity": quantity,
            "price": price,
            "tp": tp,
            "sl": sl,
            "type": order_type,
            "total": total_cost
        })
        
        return True, f"{side}注文が約定しました: {ticker} x {quantity} @ {price:,.0f}"

    def to_dict(self) -> Dict[str, Any]:
        """Serializes the account state to a dictionary."""
        return {
            "balance": self.balance,
            "positions": self.positions,
            "trade_history": self.trade_history,
            "initial_balance": self.initial_balance
        }

    def from_dict(self, data: Dict[str, Any]):
        """Restores account state from a dictionary."""
        self.balance = data.get("balance", 10000000.0)
        self.positions = data.get("positions", {})
        self.trade_history = data.get("trade_history", [])
        self.initial_balance = data.get("initial_balance", 10000000.0)

    def save(self, filename: str = "demo_data.json"):
        """Saves the account state to a JSON file."""
        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving data: {e}")

    def load(self, filename: str = "demo_data.json"):
        """Loads the account state from a JSON file if it exists."""
        if not os.path.exists(filename):
            return
        
        try:
            with open(filename, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.from_dict(data)
        except Exception as e:
            print(f"Error loading data: {e}")
