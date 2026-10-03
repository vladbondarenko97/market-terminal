import numpy as np
import pandas as pd
from scipy.stats import norm
import os
from datetime import datetime, timedelta
import yfinance as yf

class QuantEngine:
    def __init__(self, ledger_file):
        self.ledger_file = ledger_file
        self.rolling_window = 30 # days
        
    def get_historical_baseline(self):
        """Loads historical data and calculates means/std devs for normalization."""
        if not os.path.exists(self.ledger_file):
            return None
        
        df = pd.read_csv(self.ledger_file)
        df['Datetime'] = pd.to_datetime(df['Datetime'], format='mixed')
        
        # Filter for last 30 days
        cutoff = datetime.now() - timedelta(days=self.rolling_window)
        hist_df = df[df['Datetime'] >= cutoff]
        
        if hist_df.empty:
            hist_df = df.tail(100) # Fallback to last 100 entries if 30 days is empty
            
        metrics = {
            'vix': {'mean': hist_df['VIX'].mean(), 'std': hist_df['VIX'].std()},
            'gex': {'mean': hist_df['GEX'].mean(), 'std': hist_df['GEX'].std()},
            'dix': {'mean': hist_df['DIX'].mean(), 'std': hist_df['DIX'].std()},
        }
        return metrics

    def calculate_z_score_oscillator(self, current_data):
        """
        Normalizes multiple factors into a -100 to +100 oscillator.
        Factors: VIX, GEX (Gamma Exposure), DIX (Dark Pool Index).
        """
        baseline = self.get_historical_baseline()
        if not baseline:
            return 0.0
            
        # 1. VIX Z-Score (Higher VIX = Bearish/Extension)
        vix_z = (current_data['vix'] - baseline['vix']['mean']) / baseline['vix']['std'] if baseline['vix']['std'] > 0 else 0
        
        # 2. GEX Z-Score (Lower GEX = Higher Volatility/Short Gamma)
        gex_z = (current_data['gex'] - baseline['gex']['mean']) / baseline['gex']['std'] if baseline['gex']['std'] > 0 else 0
        
        # 3. DIX Z-Score (Higher DIX = Dark Pool Accumulation/Bullish)
        dix_z = (current_data['dix'] - baseline['dix']['mean']) / baseline['dix']['std'] if baseline['dix']['std'] > 0 else 0
        
        # Composite Z-Score (Inverse VIX, normal GEX, normal DIX)
        composite_z = (-vix_z + gex_z + dix_z) / 3
        
        # Cap at -3 to +3 std devs and scale to -100 to +100
        score = np.clip(composite_z, -2, 2) * 50 # Scale 2SD to 100
        return float(score)

    def get_risk_free_rate(self):
        """Fetches the 13-week Treasury Bill (^IRX) yield as a risk-free rate proxy."""
        try:
            irx = yf.Ticker("^IRX")
            hist = irx.history(period="1d")
            if not hist.empty:
                rate = hist['Close'].iloc[-1] / 100.0
                return max(0.0, float(rate))
        except Exception:
            pass
        return 0.05 # Fallback

    def calculate_strike_probabilities(self, spot, strikes_with_iv, days_list=[3, 5, 7]):
        """
        Calculates log-normal probability of expiring ITM for each specific strike/iv pair over multiple horizons.
        strikes_with_iv: list of dicts [{'strike': K, 'iv': vol}, ...]
        """
        r = self.get_risk_free_rate()
        
        probs = []
        for item in strikes_with_iv:
            K = item['strike']
            vol = item.get('iv', 0.2)
            if vol <= 0:
                vol = 0.001
            
            p_data = {'strike': K, 'iv': float(vol)}
            
            for days in days_list:
                T = days / 365.0
                # Skew-adjusted Black-Scholes d2
                d2 = (np.log(spot / K) + (r - 0.5 * vol**2) * T) / (vol * np.sqrt(T))
                # Probability S > K (for Calls)
                p_data[f'prob_{days}d'] = float(norm.cdf(d2))
                
            probs.append(p_data)
            
        return probs

    def calculate_realized_volatility(self, ticker="SPY", window=20):
        """
        Calculates the exact 20-day trailing historical volatility (HV) 
        using daily log returns from yfinance.
        """
        try:
            tk = yf.Ticker(ticker)
            # Fetch ~2 months to ensure we get 21 trading days
            hist = tk.history(period="2mo")
            if len(hist) < window:
                return 0.15 # Fallback
            
            # Use last 21 trading days (20 returns)
            prices = hist['Close'].tail(window + 1)
            # Log returns: ln(P_t / P_t-1)
            log_returns = np.log(prices / prices.shift(1)).dropna()
            
            # Annualized historical volatility
            hv = log_returns.std() * np.sqrt(252)
            return float(hv)
        except Exception:
            return 0.15 # Fallback

    def calculate_bs_delta(self, spot, strike, t, r, vol, is_call=True):
        if t <= 0 or vol <= 0:
            return 1.0 if is_call and spot > strike else (0.0 if is_call else -1.0 if spot < strike else 0.0)
        d1 = (np.log(spot / strike) + (r + 0.5 * vol**2) * t) / (vol * np.sqrt(t))
        if is_call:
            return norm.cdf(d1)
        else:
            return norm.cdf(d1) - 1.0

    def calculate_vanna(self, spot, strike, days, r, vol, is_call=True):
        """Sensitivity of Delta to Volatility (1% change)."""
        t = days / 365.0
        if t <= 0: return 0.0
        delta_up = self.calculate_bs_delta(spot, strike, t, r, vol + 0.01, is_call)
        delta_down = self.calculate_bs_delta(spot, strike, t, r, max(0.0001, vol - 0.01), is_call)
        return (delta_up - delta_down) / 0.02

    def calculate_charm(self, spot, strike, days, r, vol, is_call=True):
        """Sensitivity of Delta to Time (decay over 1 day)."""
        t = days / 365.0
        if t <= 1/365.0: return 0.0
        delta_today = self.calculate_bs_delta(spot, strike, t, r, vol, is_call)
        delta_tomorrow = self.calculate_bs_delta(spot, strike, t - 1/365.0, r, vol, is_call)
        return delta_tomorrow - delta_today

    def get_gamma_state(self, spot, zero_gamma, previous_spot=None):
        """
        Calculates distance and velocity relative to Zero Gamma.
        """
        distance = spot - zero_gamma
        distance_pct = distance / spot
        
        velocity = 0.0
        if previous_spot is not None:
            velocity = spot - previous_spot # Points change
            
        return {
            'distance': float(distance),
            'distance_pct': float(distance_pct),
            'velocity': float(velocity),
            'short_gamma_active': distance < 0
        }

    def calculate_option_analytics(self, spot, strike, days, r, vol, option_type, market_price=None):
        """
        Full institutional-grade option analytics for a single contract.
        Returns all Greeks, probabilities, and trade metrics.
        """
        is_call = option_type.lower() == 'call'
        T = max(days / 365.0, 1e-6)
        vol = max(vol, 1e-6)

        # === Core BS ===
        d1 = (np.log(spot / strike) + (r + 0.5 * vol**2) * T) / (vol * np.sqrt(T))
        d2 = d1 - vol * np.sqrt(T)

        # === Probabilities ===
        if is_call:
            prob_itm = float(norm.cdf(d2))           # Risk-neutral prob of expiring ITM
            prob_otm = float(1 - prob_itm)
            bs_price = spot * norm.cdf(d1) - strike * np.exp(-r * T) * norm.cdf(d2)
        else:
            prob_itm = float(norm.cdf(-d2))
            prob_otm = float(1 - prob_itm)
            bs_price = strike * np.exp(-r * T) * norm.cdf(-d2) - spot * norm.cdf(-d1)
        bs_price = max(bs_price, 0.0)

        # === Delta ===
        delta = float(norm.cdf(d1) if is_call else norm.cdf(d1) - 1.0)

        # === Gamma ===
        gamma = float(norm.pdf(d1) / (spot * vol * np.sqrt(T)))

        # === Theta (per day) ===
        theta_raw = (
            -(spot * norm.pdf(d1) * vol) / (2 * np.sqrt(T))
            - r * strike * np.exp(-r * T) * (norm.cdf(d2) if is_call else norm.cdf(-d2))
        )
        if not is_call:
            theta_raw = theta_raw + r * strike * np.exp(-r * T)
        theta = float(theta_raw / 365.0)  # per calendar day

        # === Vega (per 1% vol move) ===
        vega = float(spot * norm.pdf(d1) * np.sqrt(T) / 100.0)

        # === Rho (per 1% rate move) ===
        if is_call:
            rho = float(strike * T * np.exp(-r * T) * norm.cdf(d2) / 100.0)
        else:
            rho = float(-strike * T * np.exp(-r * T) * norm.cdf(-d2) / 100.0)

        # === Intrinsic / Extrinsic ===
        if is_call:
            intrinsic = float(max(spot - strike, 0.0))
        else:
            intrinsic = float(max(strike - spot, 0.0))
        extrinsic = float(max(bs_price - intrinsic, 0.0))

        # === Breakeven ===
        price_to_use = market_price if market_price and market_price > 0 else bs_price
        if is_call:
            breakeven = float(strike + price_to_use)
        else:
            breakeven = float(strike - price_to_use)

        # === IV vs HV Signal ===
        hv = self.calculate_realized_volatility()
        iv_hv_diff = vol - hv
        if iv_hv_diff > 0.05:
            iv_signal = "OVERPRICED — SELL"
        elif iv_hv_diff < -0.05:
            iv_signal = "UNDERPRICED — BUY"
        else:
            iv_signal = "FAIRLY PRICED"

        # === Moneyness ===
        moneyness_pct = (spot - strike) / strike * 100
        if abs(moneyness_pct) < 0.5:
            moneyness = "ATM"
        elif (is_call and moneyness_pct > 0) or (not is_call and moneyness_pct < 0):
            moneyness = f"ITM ({abs(moneyness_pct):.1f}%)"
        else:
            moneyness = f"OTM ({abs(moneyness_pct):.1f}%)"

        # === Expected Move at expiry ===
        expected_move = float(spot * vol * np.sqrt(T))

        return {
            "bs_price": round(bs_price, 4),
            "market_price": round(price_to_use, 4),
            "prob_itm": round(prob_itm * 100, 2),
            "prob_otm": round(prob_otm * 100, 2),
            "delta": round(delta, 4),
            "gamma": round(gamma, 6),
            "theta": round(theta, 4),
            "vega": round(vega, 4),
            "rho": round(rho, 4),
            "intrinsic": round(intrinsic, 4),
            "extrinsic": round(extrinsic, 4),
            "breakeven": round(breakeven, 4),
            "moneyness": moneyness,
            "expected_move": round(expected_move, 2),
            "iv_pct": round(vol * 100, 2),
            "hv_pct": round(hv * 100, 2),
            "iv_signal": iv_signal,
        }
