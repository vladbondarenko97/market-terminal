import math
import os
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm


def _finite(value):
    """A finite float, or None for None, NaN, infinity and anything that is not a number."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class QuantEngine:
    # name in the caller's dict, ledger column, sign in the composite (higher VIX is bearish; higher GEX and DIX
    # are bullish)
    OSCILLATOR_COMPONENTS = (("vix", "VIX", -1.0), ("gex", "GEX", 1.0), ("dix", "DIX", 1.0))
    RATE_CACHE_SECONDS = 60

    def __init__(self, ledger_file):
        self.ledger_file = ledger_file
        self.rolling_window = 30 # days
        self._rate_cache = None   # (monotonic time, rate)

    def get_historical_baseline(self):
        """Loads the macro ledger and calculates the mean, standard deviation and sample count of each oscillator
        component over the last 30 days (the last 100 rows when the ledger has nothing that recent). A component
        with no recent values has n == 0. Returns None when there is no ledger."""
        if not os.path.exists(self.ledger_file):
            return None

        df = pd.read_csv(self.ledger_file)
        df['Datetime'] = pd.to_datetime(df['Datetime'], format='mixed')

        # Filter for last 30 days
        cutoff = datetime.now() - timedelta(days=self.rolling_window)
        hist_df = df[df['Datetime'] >= cutoff]

        if hist_df.empty:
            hist_df = df.tail(100) # Fallback to last 100 entries if 30 days is empty

        baseline = {}
        for name, column, _ in self.OSCILLATOR_COMPONENTS:
            if column in hist_df:
                series = pd.to_numeric(hist_df[column], errors='coerce').dropna()
            else:
                series = pd.Series(dtype=float)
            baseline[name] = {
                'mean': float(series.mean()) if len(series) else None,
                'std': float(series.std()) if len(series) > 1 else None,
                'n': int(len(series)),
            }
        return baseline

    def calculate_z_score_oscillator(self, current_data):
        """
        Normalizes the available factors into a -100 to +100 oscillator.
        Factors: VIX, GEX (Gamma Exposure), DIX (Dark Pool Index). `current_data` maps 'vix', 'gex' and 'dix' to
        today's value (None when unknown).

        A factor is used only when it has a current value and at least two recent values with some spread in the
        macro ledger; otherwise it is left out and the reason is reported (a missing factor never counts as zero).
        The composite is the mean of the signed z-scores of the factors that were used.

        Returns {'score': float or None, 'components_used': [names], 'components_missing': {name: reason}}.
        'score' is None when no factor could be used.
        """
        try:
            baseline = self.get_historical_baseline()
            no_baseline = "macro ledger not found"
        except (KeyError, ValueError):
            baseline, no_baseline = None, "macro ledger could not be read"
        used, missing, signed_z = [], {}, []
        for name, column, sign in self.OSCILLATOR_COMPONENTS:
            current = _finite((current_data or {}).get(name))
            base = (baseline or {}).get(name)
            if baseline is None:
                missing[name] = no_baseline
            elif current is None:
                missing[name] = f"no current {column} value"
            elif base['n'] == 0:
                missing[name] = f"no recent {column} values in the macro ledger"
            elif not (base['std'] and base['std'] > 0):
                missing[name] = f"recent {column} values have no spread (fewer than two, or all equal)"
            else:
                signed_z.append(sign * (current - base['mean']) / base['std'])
                used.append(name)

        score = None
        if signed_z:
            composite_z = sum(signed_z) / len(signed_z)
            # Cap at -2 to +2 std devs and scale to -100 to +100
            score = float(np.clip(composite_z, -2, 2) * 50)
        return {'score': score, 'components_used': used, 'components_missing': missing}

    def get_risk_free_rate(self):
        """Fetches the 13-week Treasury Bill (^IRX) yield as a risk-free rate proxy (a fraction, 0.043 = 4.3%).
        Returns None when Yahoo does not supply it: callers report the dependent values as missing, there is no
        stand-in rate. A good answer is cached for 60 seconds."""
        now = time.monotonic()
        if self._rate_cache and now - self._rate_cache[0] < self.RATE_CACHE_SECONDS:
            return self._rate_cache[1]
        try:
            irx = yf.Ticker("^IRX")
            hist = irx.history(period="1d")
            if not hist.empty:
                rate = _finite(hist['Close'].iloc[-1])
                if rate is not None:
                    rate = max(0.0, rate / 100.0)
                    self._rate_cache = (now, rate)
                    return rate
        except Exception:
            pass
        return None

    def calculate_strike_probabilities(self, spot, strikes_with_iv, days_list=(3, 5, 7), r=None):
        """
        Calculates log-normal probability of expiring ITM for each specific strike/iv pair over multiple horizons.
        strikes_with_iv: list of dicts [{'strike': K, 'iv': vol}, ...]
        r: the risk-free rate (fraction). Without it nothing can be computed and the result is an empty list.
        A strike with no usable implied volatility (missing or not above zero) is left out.
        """
        if r is None:
            return []

        probs = []
        for item in strikes_with_iv:
            K = _finite(item.get('strike'))
            vol = _finite(item.get('iv'))
            if K is None or K <= 0 or vol is None or vol <= 0:
                continue

            p_data = {'strike': K, 'iv': vol}

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
        using daily log returns from yfinance. Returns None when Yahoo does not supply enough history.
        """
        try:
            tk = yf.Ticker(ticker)
            # Fetch ~2 months to ensure we get 21 trading days
            closes = tk.history(period="2mo")['Close'].dropna()
            if len(closes) < window + 1:
                return None

            # Use last 21 trading days (20 returns)
            prices = closes.tail(window + 1)
            # Log returns: ln(P_t / P_t-1)
            log_returns = np.log(prices / prices.shift(1)).dropna()

            # Annualized historical volatility
            return _finite(log_returns.std() * np.sqrt(252))
        except Exception:
            return None

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

    def calculate_option_analytics(self, spot, strike, days, r, vol, option_type, market_price=None, ticker=None):
        """
        Full institutional-grade option analytics for a single contract.
        Returns all Greeks, probabilities, and trade metrics.

        `ticker` names the underlying whose 20-day realized volatility is compared with the contract's implied
        volatility. Without a ticker, or when Yahoo has no history for it, `hv_pct` and `iv_signal` are None and
        the reason is in the returned `missing` dict. Without a market price (None or 0) `market_price` is None, with
        a reason in `missing`; the breakeven then uses the Black-Scholes price.
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
        missing = {}
        # Without a market price the breakeven uses the Black-Scholes price, and `market_price` is reported as None
        # (the model price is never shown as if it were a quote).
        has_market_price = bool(market_price and market_price > 0)
        price_to_use = market_price if has_market_price else bs_price
        if not has_market_price:
            missing["market_price"] = "no market price was given and the chain has none: breakeven uses the Black-Scholes price"
        if is_call:
            breakeven = float(strike + price_to_use)
        else:
            breakeven = float(strike - price_to_use)

        # === IV vs HV Signal ===
        hv = self.calculate_realized_volatility(ticker) if ticker else None
        if hv is None:
            reason = ("no ticker given for the realized volatility" if not ticker
                      else f"not enough price history for {ticker} to compute 20-day realized volatility")
            missing["hv_pct"] = missing["iv_signal"] = reason
            iv_signal = None
        else:
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
            "market_price": round(market_price, 4) if has_market_price else None,
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
            "hv_pct": round(hv * 100, 2) if hv is not None else None,
            "iv_signal": iv_signal,
            "missing": missing,
        }
