from alpaca.trading.client import TradingClient
import os

class TradeEngine:
    def __init__(self):
        # Initializing the Trading Client (Paper trading enabled by default for safety)
        self.client = TradingClient(
            api_key=os.environ.get("ALPACA_API_KEY"),
            secret_key=os.environ.get("ALPACA_SECRET_KEY"),
            paper=True 
        )

    def get_report(self):
        """Jarvis uses this to summarize your financial health."""
        account = self.client.get_account()
        positions = self.client.get_all_positions()
        
        report = (
            f"Current Equity: ${account.equity}\n"
            f"Buying Power: ${account.buying_power}\n"
            f"Open Positions: {len(positions)}\n"
        )
        return report
def calculate_kelly_size(win_rate, win_loss_ratio, account_equity):
    """
    Standard Kelly: f* = (bp - q) / b
    Where:
    - p (win_rate): 0.65 (65%)
    - b (win_loss_ratio): Avg Win / Avg Loss
    - q: 1 - p
    """
    p = win_rate
    q = 1 - p
    b = win_loss_ratio
    
    if b <= 0: return 0
    
    full_kelly = (b * p - q) / b
    
    # Apply Quarter-Kelly (0.25) for safety in 2026 markets
    safe_fraction = max(0, full_kelly * 0.25)
    
    # Cap any single trade at 5% of total equity regardless of Kelly
    final_fraction = min(safe_fraction, 0.05)
    
    suggested_dollar_amount = account_equity * final_fraction
    return round(suggested_dollar_amount, 2)
