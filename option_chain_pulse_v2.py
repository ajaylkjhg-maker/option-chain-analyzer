from __future__ import annotations

import csv
import os
import json
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from math import exp, log, pi, sqrt
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from PyQt5.QtCore import QPoint, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "Option Chain Pulse V2"
REFRESH_SECONDS = 180
NSE_HOME_URL = "https://www.nseindia.com"
NSE_OPTION_CHAIN_PAGE = "https://www.nseindia.com/option-chain"
NSE_OPTION_CONTRACT_INFO_API = "https://www.nseindia.com/api/option-chain-contract-info"
NSE_OPTION_CHAIN_API = "https://www.nseindia.com/api/option-chain-v3"
NSE_ALL_INDICES_API = "https://www.nseindia.com/api/allIndices"
RISK_FREE_RATE = 0.06

APP_DIR = Path.home() / "AppData" / "Local" / "OptionChainPulse"
STATE_FILE = APP_DIR / "state.json"
LOG_FILE = APP_DIR / "option_chain_pulse_v2.log"
JOURNAL_FILE = APP_DIR / "trade_journal.csv"


def setup_logging() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


@dataclass
class StrikeMetrics:
    strike: int
    ce_oi: int
    pe_oi: int
    ce_coi: int
    pe_coi: int
    ce_volume: int
    pe_volume: int
    ce_ltp: float
    pe_ltp: float
    ce_iv: float
    pe_iv: float


@dataclass
class TradeSetup:
    action: str = "WAIT"
    strike: int = 0
    entry: float = 0.0
    stop_loss: float = 0.0
    targets: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    confidence: int = 50


@dataclass
class AnalysisResult:
    symbol: str
    spot: float
    expiry: str
    timestamp: str
    india_vix: float
    india_vix_status: str
    pcr: float
    supports: List[Tuple[int, int]]
    resistances: List[Tuple[int, int]]
    max_pain: int
    previous_max_pain: int
    max_pain_shift: int
    smart_flow: int
    trend_score: int
    bullish_probability: int
    bearish_probability: int
    market_regime: str
    banknifty_score: int
    banknifty_confirmation: str
    atm_delta: float
    atm_gamma: float
    atm_theta: float
    atm_vega: float
    atm_interpretation: str
    positive_gex: float
    negative_gex: float
    net_gex: float
    gamma_flip: int
    heatmap: List[StrikeMetrics]
    trade: TradeSetup
    verdict: str
    error: Optional[str] = None
    alerts: List[str] = field(default_factory=list)


class NSEClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": NSE_OPTION_CHAIN_PAGE,
                "Connection": "keep-alive",
            }
        )

    def _request_json(self, url: str, params: Optional[Dict] = None, retries: int = 3) -> Dict:
        last_error: Optional[Exception] = None
        for attempt in range(retries):
            try:
                if attempt == 0:
                    self.session.get(NSE_HOME_URL, timeout=10)
                    self.session.get(NSE_OPTION_CHAIN_PAGE, timeout=10)
                response = self.session.get(url, params=params, timeout=15)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_error = exc
                logging.warning("NSE request failed attempt %s: %s", attempt + 1, exc)
                time.sleep(0.7 * (attempt + 1))
        raise RuntimeError(str(last_error))

    def option_chain(self, symbol: str) -> Dict:
        symbol = symbol.upper()
        if symbol == "SENSEX":
            return SensexBrokerClient().option_chain()
        contract = self._request_json(NSE_OPTION_CONTRACT_INFO_API, {"symbol": symbol})
        expiries = contract.get("expiryDates") or []
        if not expiries:
            raise RuntimeError(f"No expiries returned for {symbol}.")
        return self._request_json(
            NSE_OPTION_CHAIN_API,
            {"type": "Indices", "symbol": symbol, "expiry": expiries[0]},
        )

    def india_vix(self) -> float:
        try:
            payload = self._request_json(NSE_ALL_INDICES_API, retries=2)
            for item in payload.get("data", []):
                name = (item.get("index") or item.get("indexSymbol") or "").upper()
                if "INDIA VIX" in name:
                    return float(item.get("last") or item.get("lastPrice") or 0)
        except Exception as exc:
            logging.warning("India VIX fetch failed: %s", exc)
        return 0.0


class SensexBrokerClient:
    """Optional SENSEX adapter.

    BSE SENSEX options are not available from NSE option-chain APIs. Configure a
    broker/BSE-compatible endpoint with environment variables:

        OCP_SENSEX_API_URL=https://your-provider/sensex-option-chain
        OCP_SENSEX_API_TOKEN=your_token

    The endpoint should return NSE-like JSON with records.data rows containing
    strikePrice, CE, PE, records.expiryDates, and records.underlyingValue.
    """

    def __init__(self) -> None:
        self.url = os.getenv("OCP_SENSEX_API_URL", "").strip()
        self.token = os.getenv("OCP_SENSEX_API_TOKEN", "").strip()

    def option_chain(self) -> Dict:
        if not self.url:
            raise RuntimeError(
                "SENSEX data needs a BSE/broker API. Set OCP_SENSEX_API_URL and "
                "OCP_SENSEX_API_TOKEN, or use NIFTY/BANKNIFTY."
            )
        headers = {"Accept": "application/json", "User-Agent": APP_NAME}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = requests.get(self.url, headers=headers, timeout=15)
        response.raise_for_status()
        payload = response.json()
        if not payload or "records" not in payload:
            raise RuntimeError("Configured SENSEX API did not return NSE-like option-chain JSON.")
        return payload


class QuantEngine:
    def __init__(self) -> None:
        self.state = self._load_state()

    def _load_state(self) -> Dict:
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def analyze(self, symbol: str, raw: Dict, india_vix: float, bank_score: int = 0) -> AnalysisResult:
        records = raw.get("records", {})
        rows = records.get("data") or []
        expiries = records.get("expiryDates") or []
        expiry = expiries[0] if expiries else ""
        spot = float(records.get("underlyingValue") or self._spot_from_rows(rows))
        strike_rows = self._extract_rows(rows, expiry)
        if not strike_rows:
            raise RuntimeError("No option-chain rows found for nearest expiry.")

        total_ce_oi = sum(row.ce_oi for row in strike_rows)
        total_pe_oi = sum(row.pe_oi for row in strike_rows)
        total_ce_coi = sum(row.ce_coi for row in strike_rows)
        total_pe_coi = sum(row.pe_coi for row in strike_rows)
        pcr = total_pe_oi / total_ce_oi if total_ce_oi else 0

        supports = self._strength_levels(strike_rows, "PE")[:2]
        resistances = self._strength_levels(strike_rows, "CE")[:2]
        max_pain = self._max_pain(strike_rows)
        previous_max_pain = int(self.state.get(symbol, {}).get("max_pain") or max_pain)
        max_pain_shift = max_pain - previous_max_pain
        self.state.setdefault(symbol, {})["max_pain"] = max_pain
        self._save_state()

        smart_flow = self._smart_flow(pcr, total_pe_coi, total_ce_coi, spot, supports, resistances, max_pain)
        pos_gex, neg_gex, net_gex, gamma_flip = self._gex(strike_rows, spot, expiry)
        atm_delta, atm_gamma, atm_theta, atm_vega = self._atm_greeks(strike_rows, spot, expiry)
        vix_status = self._vix_status(india_vix)
        market_regime = self._market_regime(india_vix, self._avg_atm_iv(strike_rows, spot), net_gex, spot, supports, resistances)
        trend_score = self._trend_score(smart_flow, pcr, total_pe_coi, total_ce_coi, india_vix, max_pain_shift, bank_score)
        bull_prob, bear_prob = self._breakout_probability(trend_score, smart_flow, pcr, total_pe_coi, total_ce_coi, india_vix, bank_score)
        bank_confirmation = self._bank_confirmation(trend_score, bank_score)
        trade = self._trade_setup(strike_rows, spot, bull_prob, bear_prob, trend_score, smart_flow, supports, resistances)
        verdict = self._verdict(trade, trend_score, market_regime)
        alerts = self._alerts(symbol, spot, trade, resistances, supports)

        return AnalysisResult(
            symbol=symbol,
            spot=spot,
            expiry=expiry,
            timestamp=datetime.now().strftime("%H:%M:%S"),
            india_vix=india_vix,
            india_vix_status=vix_status,
            pcr=pcr,
            supports=supports,
            resistances=resistances,
            max_pain=max_pain,
            previous_max_pain=previous_max_pain,
            max_pain_shift=max_pain_shift,
            smart_flow=smart_flow,
            trend_score=trend_score,
            bullish_probability=bull_prob,
            bearish_probability=bear_prob,
            market_regime=market_regime,
            banknifty_score=bank_score,
            banknifty_confirmation=bank_confirmation,
            atm_delta=atm_delta,
            atm_gamma=atm_gamma,
            atm_theta=atm_theta,
            atm_vega=atm_vega,
            atm_interpretation=self._greek_interpretation(atm_delta, atm_gamma, atm_theta, atm_vega),
            positive_gex=pos_gex,
            negative_gex=neg_gex,
            net_gex=net_gex,
            gamma_flip=gamma_flip,
            heatmap=sorted(strike_rows, key=lambda row: abs(row.strike - spot))[:9],
            trade=trade,
            verdict=verdict,
            alerts=alerts,
        )

    def score_only(self, raw: Dict) -> int:
        result = self.analyze("BANKNIFTY", raw, india_vix=0, bank_score=0)
        return result.trend_score

    def _extract_rows(self, rows: List[Dict], expiry: str) -> List[StrikeMetrics]:
        parsed = []
        for row in rows:
            row_expiry = row.get("expiryDate") or row.get("expiryDates")
            if expiry and row_expiry != expiry:
                continue
            ce = row.get("CE") or {}
            pe = row.get("PE") or {}
            parsed.append(
                StrikeMetrics(
                    strike=int(row.get("strikePrice") or 0),
                    ce_oi=int(ce.get("openInterest") or 0),
                    pe_oi=int(pe.get("openInterest") or 0),
                    ce_coi=int(ce.get("changeinOpenInterest") or 0),
                    pe_coi=int(pe.get("changeinOpenInterest") or 0),
                    ce_volume=int(ce.get("totalTradedVolume") or 0),
                    pe_volume=int(pe.get("totalTradedVolume") or 0),
                    ce_ltp=float(ce.get("lastPrice") or 0),
                    pe_ltp=float(pe.get("lastPrice") or 0),
                    ce_iv=float(ce.get("impliedVolatility") or 0),
                    pe_iv=float(pe.get("impliedVolatility") or 0),
                )
            )
        return [row for row in parsed if row.strike > 0]

    def _spot_from_rows(self, rows: List[Dict]) -> float:
        for row in rows:
            for leg in ("CE", "PE"):
                value = (row.get(leg) or {}).get("underlyingValue")
                if value:
                    return float(value)
        return 0.0

    def _strength_levels(self, rows: List[StrikeMetrics], side: str) -> List[Tuple[int, int]]:
        total_oi = sum(row.pe_oi if side == "PE" else row.ce_oi for row in rows) or 1
        scored = []
        for row in rows:
            oi = row.pe_oi if side == "PE" else row.ce_oi
            coi = max(row.pe_coi if side == "PE" else row.ce_coi, 0)
            volume = row.pe_volume if side == "PE" else row.ce_volume
            concentration = oi / total_oi
            raw_score = min(100, int((concentration * 230) + min(coi / 1200, 35) + min(volume / 5000, 25)))
            scored.append((row.strike, max(1, raw_score)))
        return sorted(scored, key=lambda item: item[1], reverse=True)

    def _max_pain(self, rows: List[StrikeMetrics]) -> int:
        pain = {}
        strikes = [row.strike for row in rows]
        for settlement in strikes:
            pain[settlement] = sum(
                max(0, settlement - row.strike) * row.ce_oi + max(0, row.strike - settlement) * row.pe_oi
                for row in rows
            )
        return min(pain, key=pain.get) if pain else 0

    def _smart_flow(self, pcr: float, pe_coi: int, ce_coi: int, spot: float, supports: List[Tuple[int, int]], resistances: List[Tuple[int, int]], max_pain: int) -> int:
        score = 0
        score += 25 if pcr > 1.2 else 12 if pcr > 1.05 else -25 if pcr < 0.8 else -12 if pcr < 0.95 else 0
        score += 20 if pe_coi > ce_coi else -20 if ce_coi > pe_coi else 0
        if supports and spot and abs(spot - supports[0][0]) / spot <= 0.005:
            score += 15
        if resistances and spot and abs(spot - resistances[0][0]) / spot <= 0.005:
            score -= 15
        if max_pain:
            score += 10 if spot > max_pain else -10 if spot < max_pain else 0
        return max(-100, min(100, score))

    def _gex(self, rows: List[StrikeMetrics], spot: float, expiry: str) -> Tuple[float, float, float, int]:
        years = max(self._days_to_expiry(expiry) / 365, 1 / (365 * 6.5))
        pos_gex = 0.0
        neg_gex = 0.0
        by_strike = {}
        for row in rows:
            ce_gamma = self._bs_gamma(spot, row.strike, years, row.ce_iv / 100)
            pe_gamma = self._bs_gamma(spot, row.strike, years, row.pe_iv / 100)
            strike_gex = (ce_gamma * row.ce_oi - pe_gamma * row.pe_oi) * spot * spot / 100
            by_strike[row.strike] = strike_gex
            if strike_gex >= 0:
                pos_gex += strike_gex
            else:
                neg_gex += strike_gex
        net_gex = pos_gex + neg_gex
        gamma_flip = min(by_strike, key=lambda strike: abs(by_strike[strike])) if by_strike else 0
        return pos_gex, neg_gex, net_gex, gamma_flip

    def _atm_greeks(self, rows: List[StrikeMetrics], spot: float, expiry: str) -> Tuple[float, float, float, float]:
        atm = min(rows, key=lambda row: abs(row.strike - spot))
        iv = max((atm.ce_iv + atm.pe_iv) / 200, 0.01)
        years = max(self._days_to_expiry(expiry) / 365, 1 / (365 * 6.5))
        d1 = (log(spot / atm.strike) + (RISK_FREE_RATE + 0.5 * iv * iv) * years) / (iv * sqrt(years))
        d2 = d1 - iv * sqrt(years)
        density = exp(-0.5 * d1 * d1) / sqrt(2 * pi)
        delta = self._norm_cdf(d1)
        gamma = density / (spot * iv * sqrt(years))
        theta = (-(spot * density * iv) / (2 * sqrt(years)) - RISK_FREE_RATE * atm.strike * exp(-RISK_FREE_RATE * years) * self._norm_cdf(d2)) / 365
        vega = spot * density * sqrt(years) / 100
        return delta, gamma, theta, vega

    def _bs_gamma(self, spot: float, strike: float, years: float, volatility: float) -> float:
        if spot <= 0 or strike <= 0 or years <= 0 or volatility <= 0:
            return 0.0
        d1 = (log(spot / strike) + (RISK_FREE_RATE + 0.5 * volatility * volatility) * years) / (volatility * sqrt(years))
        return exp(-0.5 * d1 * d1) / sqrt(2 * pi) / (spot * volatility * sqrt(years))

    def _norm_cdf(self, value: float) -> float:
        return 0.5 * (1 + self._erf(value / sqrt(2)))

    def _erf(self, value: float) -> float:
        # Abramowitz-Stegun approximation; avoids scipy dependency for a small exe.
        sign = 1 if value >= 0 else -1
        value = abs(value)
        t = 1 / (1 + 0.3275911 * value)
        coefficients = (0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429)
        poly = sum(coeff * t ** (index + 1) for index, coeff in enumerate(coefficients))
        return sign * (1 - poly * exp(-value * value))

    def _days_to_expiry(self, expiry: str) -> float:
        for fmt in ("%d-%b-%Y", "%d-%m-%Y"):
            try:
                return max((datetime.strptime(expiry, fmt) - datetime.now()).total_seconds() / 86400, 0.15)
            except ValueError:
                continue
        return 1.0

    def _avg_atm_iv(self, rows: List[StrikeMetrics], spot: float) -> float:
        near = sorted(rows, key=lambda row: abs(row.strike - spot))[:3]
        ivs = [value for row in near for value in (row.ce_iv, row.pe_iv) if value > 0]
        return sum(ivs) / len(ivs) if ivs else 0.0

    def _vix_status(self, india_vix: float) -> str:
        if india_vix <= 0:
            return "UNAVAILABLE"
        if india_vix < 12:
            return "LOW"
        if india_vix <= 18:
            return "NORMAL"
        return "HIGH"

    def _market_regime(self, india_vix: float, atm_iv: float, net_gex: float, spot: float, supports: List[Tuple[int, int]], resistances: List[Tuple[int, int]]) -> str:
        if india_vix >= 18 or atm_iv >= 22 or net_gex < 0:
            return "VOLATILE"
        if supports and resistances and spot:
            band = abs(resistances[0][0] - supports[0][0]) / spot
            if band < 0.01:
                return "RANGEBOUND"
        return "TRENDING"

    def _trend_score(self, smart: int, pcr: float, pe_coi: int, ce_coi: int, vix: float, max_pain_shift: int, bank_score: int) -> int:
        score = 50 + int(smart * 0.28)
        score += 10 if pe_coi > ce_coi else -10 if ce_coi > pe_coi else 0
        score += 6 if pcr > 1.05 else -6 if pcr < 0.95 else 0
        score += -8 if vix >= 18 else 4 if 0 < vix < 12 else 0
        score += 4 if max_pain_shift > 0 else -4 if max_pain_shift < 0 else 0
        score += 8 if bank_score >= 60 else -8 if bank_score <= 40 else 0
        return max(0, min(100, score))

    def _breakout_probability(self, trend: int, smart: int, pcr: float, pe_coi: int, ce_coi: int, vix: float, bank_score: int) -> Tuple[int, int]:
        bull = trend
        bull += 8 if smart > 25 else -8 if smart < -25 else 0
        bull += 6 if pcr > 1.1 else -6 if pcr < 0.9 else 0
        bull += 5 if pe_coi > ce_coi else -5 if ce_coi > pe_coi else 0
        bull += 5 if bank_score >= 60 else -5 if bank_score <= 40 else 0
        bull += -6 if vix >= 18 else 0
        bull = max(5, min(95, bull))
        return bull, 100 - bull

    def _bank_confirmation(self, trend: int, bank_score: int) -> str:
        if bank_score == 0:
            return "Unavailable"
        if trend >= 60 and bank_score >= 60:
            return "High Bullish"
        if trend <= 40 and bank_score <= 40:
            return "High Bearish"
        if 45 <= bank_score <= 55:
            return "Neutral"
        return "Divergence"

    def _trade_setup(self, rows: List[StrikeMetrics], spot: float, bull: int, bear: int, trend: int, smart: int, supports: List[Tuple[int, int]], resistances: List[Tuple[int, int]]) -> TradeSetup:
        step = 100 if spot > 40000 else 50
        atm = int(round(spot / step) * step)
        if bull >= 65 and trend >= 60 and smart > 20:
            strike = atm if atm >= spot else atm + step
            entry = max(spot, strike + 5)
            sl = supports[0][0] - step * 0.3 if supports else spot - step * 0.7
            return TradeSetup("BUY CE", strike, entry, sl, (entry + step, entry + 2 * step, entry + 3 * step), bull)
        if bear >= 65 and trend <= 40 and smart < -20:
            strike = atm if atm <= spot else atm - step
            entry = min(spot, strike - 5)
            sl = resistances[0][0] + step * 0.3 if resistances else spot + step * 0.7
            return TradeSetup("BUY PE", strike, entry, sl, (entry - step, entry - 2 * step, entry - 3 * step), bear)
        return TradeSetup("WAIT", atm, spot, 0, (0, 0, 0), max(bull, bear))

    def _verdict(self, trade: TradeSetup, trend: int, regime: str) -> str:
        if trade.action == "BUY CE":
            return "STRONG BULLISH" if trade.confidence >= 75 else "BULLISH"
        if trade.action == "BUY PE":
            return "STRONG BEARISH" if trade.confidence >= 75 else "BEARISH"
        if regime == "RANGEBOUND":
            return "SIDEWAYS / RANGEBOUND"
        return "WAIT FOR CONFIRMATION"

    def _greek_interpretation(self, delta: float, gamma: float, theta: float, vega: float) -> str:
        gamma_text = "fast moves" if gamma > 0.002 else "stable moves"
        theta_text = "high decay" if theta < -20 else "normal decay"
        vega_text = "IV sensitive" if vega > 15 else "low IV sensitivity"
        return f"Delta {delta:.2f}, {gamma_text}, {theta_text}, {vega_text}"

    def _alerts(self, symbol: str, spot: float, trade: TradeSetup, resistances: List[Tuple[int, int]], supports: List[Tuple[int, int]]) -> List[str]:
        alerts = []
        if trade.action != "WAIT" and trade.confidence >= 70:
            alerts.append(f"{trade.action} {trade.strike} | SL {trade.stop_loss:.0f}")
        if resistances and spot > resistances[0][0]:
            alerts.append(f"{symbol} resistance broken: {resistances[0][0]}")
        if supports and spot < supports[0][0]:
            alerts.append(f"{symbol} support broken: {supports[0][0]}")
        return alerts


class DataWorker(threading.Thread):
    def __init__(self, symbol: str, outbox: queue.Queue):
        super().__init__(daemon=True)
        self.symbol = symbol
        self.outbox = outbox
        self.client = NSEClient()
        self.engine = QuantEngine()

    def run(self) -> None:
        try:
            india_vix = self.client.india_vix()
            bank_score = 0
            if self.symbol != "BANKNIFTY":
                try:
                    bank_score = self.engine.score_only(self.client.option_chain("BANKNIFTY"))
                except Exception as exc:
                    logging.warning("BankNifty confirmation failed: %s", exc)
            result = self.engine.analyze(self.symbol, self.client.option_chain(self.symbol), india_vix, bank_score)
        except Exception as exc:
            logging.exception("Data worker failed")
            result = AnalysisResult(
                symbol=self.symbol,
                spot=0,
                expiry="--",
                timestamp=datetime.now().strftime("%H:%M:%S"),
                india_vix=0,
                india_vix_status="UNAVAILABLE",
                pcr=0,
                supports=[],
                resistances=[],
                max_pain=0,
                previous_max_pain=0,
                max_pain_shift=0,
                smart_flow=0,
                trend_score=50,
                bullish_probability=50,
                bearish_probability=50,
                market_regime="UNKNOWN",
                banknifty_score=0,
                banknifty_confirmation="Unavailable",
                atm_delta=0,
                atm_gamma=0,
                atm_theta=0,
                atm_vega=0,
                atm_interpretation="Unavailable",
                positive_gex=0,
                negative_gex=0,
                net_gex=0,
                gamma_flip=0,
                heatmap=[],
                trade=TradeSetup(),
                verdict="DATA UNAVAILABLE",
                error=str(exc),
            )
        self.outbox.put(result)


class PulseWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.outbox: queue.Queue[AnalysisResult] = queue.Queue()
        self.worker: Optional[DataWorker] = None
        self.drag_pos: Optional[QPoint] = None
        self.alert_history: List[str] = []

        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(620, 760)

        self._build_ui()
        self._build_tray()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_worker)
        self.poll_timer.start(400)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start(REFRESH_SECONDS * 1000)
        self.refresh()

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QHBoxLayout()
        self.symbol_box = QComboBox()
        self.symbol_box.addItems(["NIFTY", "BANKNIFTY", "SENSEX"])
        self.title_label = QLabel("OPTION CHAIN PULSE V2")
        self.title_label.setFont(QFont("Segoe UI Semibold", 13))
        self.refresh_button = QPushButton("Refresh")
        self.min_button = QPushButton("_")
        self.close_button = QPushButton("X")
        self.refresh_button.clicked.connect(self.refresh)
        self.min_button.clicked.connect(self.hide)
        self.close_button.clicked.connect(QApplication.quit)
        title.addWidget(self.symbol_box)
        title.addStretch(1)
        title.addWidget(self.title_label)
        title.addStretch(1)
        title.addWidget(self.refresh_button)
        title.addWidget(self.min_button)
        title.addWidget(self.close_button)
        layout.addLayout(title)

        self.verdict = QLabel("Loading...")
        self.verdict.setAlignment(Qt.AlignCenter)
        self.verdict.setFont(QFont("Segoe UI Semibold", 20))
        layout.addWidget(self.verdict)

        grid = QGridLayout()
        self.cards: Dict[str, QLabel] = {}
        labels = [
            "Spot", "PCR", "India VIX", "Regime",
            "Smart Flow", "Trend Score", "Bull Prob", "Bear Prob",
            "Max Pain", "Pain Shift", "GEX", "Gamma Flip",
            "Support", "Resistance", "BankNifty", "Updated",
        ]
        for index, label in enumerate(labels):
            card = self._card(label)
            self.cards[label] = card.findChild(QLabel, "value")
            grid.addWidget(card, index // 4, index % 4)
        layout.addLayout(grid)

        self.trade_box = QLabel("Suggested Trade: WAIT")
        self.trade_box.setObjectName("tradeBox")
        self.trade_box.setWordWrap(True)
        layout.addWidget(self.trade_box)

        self.greeks_box = QLabel("ATM Greeks: --")
        self.greeks_box.setObjectName("panelText")
        self.greeks_box.setWordWrap(True)
        layout.addWidget(self.greeks_box)

        self.heatmap = QTableWidget(0, 3)
        self.heatmap.setHorizontalHeaderLabels(["Strike", "CE COI", "PE COI"])
        self.heatmap.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.heatmap.verticalHeader().setVisible(False)
        self.heatmap.setFixedHeight(205)
        layout.addWidget(self.heatmap)

        lower = QHBoxLayout()
        self.alerts = QTextEdit()
        self.alerts.setPlaceholderText("Notification history")
        self.alerts.setFixedHeight(90)
        self.alerts.setReadOnly(True)
        lower.addWidget(self.alerts, 2)
        journal = QVBoxLayout()
        self.journal_fields = {name: QLineEdit() for name in ["Strike", "Entry", "Exit", "PnL"]}
        self.notes = QLineEdit()
        for name, field in self.journal_fields.items():
            field.setPlaceholderText(name)
            journal.addWidget(field)
        self.notes.setPlaceholderText("Notes")
        journal.addWidget(self.notes)
        add_journal = QPushButton("Add Journal")
        export_journal = QPushButton("Export CSV")
        add_journal.clicked.connect(self.add_journal)
        export_journal.clicked.connect(self.export_journal)
        journal.addWidget(add_journal)
        journal.addWidget(export_journal)
        lower.addLayout(journal, 1)
        layout.addLayout(lower)

        self.setCentralWidget(root)
        self.setStyleSheet(self._stylesheet())

    def _card(self, label: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 5, 8, 5)
        key = QLabel(label)
        key.setObjectName("key")
        value = QLabel("--")
        value.setObjectName("value")
        value.setAlignment(Qt.AlignCenter)
        value.setFont(QFont("Segoe UI Semibold", 10))
        layout.addWidget(key)
        layout.addWidget(value)
        return frame

    def _stylesheet(self) -> str:
        return """
        QWidget#root { background: rgba(17, 24, 39, 225); border-radius: 14px; color: #e5e7eb; }
        QLabel { color: #e5e7eb; font-family: Segoe UI; }
        QLabel#key { color: #9ca3af; font-size: 10px; }
        QFrame#card { background: rgba(31, 41, 55, 190); border: 1px solid #374151; border-radius: 8px; }
        QLabel#tradeBox { background: rgba(2, 6, 23, 160); border: 1px solid #475569; border-radius: 8px; padding: 8px; font: 12px 'Segoe UI Semibold'; }
        QLabel#panelText { background: rgba(31, 41, 55, 150); border-radius: 7px; padding: 7px; color: #cbd5e1; }
        QPushButton, QComboBox { background: #2563eb; color: white; border: 0; border-radius: 5px; padding: 5px 8px; }
        QPushButton:hover { background: #1d4ed8; }
        QLineEdit, QTextEdit, QTableWidget { background: #0f172a; color: #e5e7eb; border: 1px solid #334155; border-radius: 6px; }
        QHeaderView::section { background: #1f2937; color: #e5e7eb; border: 0; padding: 4px; }
        """

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(self.style().SP_ComputerIcon))
        menu = QMenu()
        restore = menu.addAction("Restore")
        refresh = menu.addAction("Refresh")
        quit_action = menu.addAction("Quit")
        restore.triggered.connect(self.showNormal)
        refresh.triggered.connect(self.refresh)
        quit_action.triggered.connect(QApplication.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.showNormal() if reason == QSystemTrayIcon.Trigger else None)
        self.tray.show()

    def refresh(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        symbol = self.symbol_box.currentText()
        self.refresh_button.setText("...")
        self.worker = DataWorker(symbol, self.outbox)
        self.worker.start()

    def _poll_worker(self) -> None:
        try:
            result = self.outbox.get_nowait()
        except queue.Empty:
            return
        self.refresh_button.setText("Refresh")
        self.render(result)

    def render(self, result: AnalysisResult) -> None:
        color = "#22c55e" if result.trend_score >= 60 else "#ef4444" if result.trend_score <= 40 else "#facc15"
        self.verdict.setText(result.verdict)
        self.verdict.setStyleSheet(f"color: {color};")
        self._set("Spot", f"{result.symbol} {result.spot:,.2f}")
        self._set("PCR", f"{result.pcr:.2f}")
        self._set("India VIX", f"{result.india_vix:.2f} {result.india_vix_status}")
        self._set("Regime", result.market_regime)
        self._set("Smart Flow", f"{result.smart_flow:+d}")
        self._set("Trend Score", f"{result.trend_score}/100")
        self._set("Bull Prob", f"{result.bullish_probability}%")
        self._set("Bear Prob", f"{result.bearish_probability}%")
        self._set("Max Pain", str(result.max_pain))
        self._set("Pain Shift", f"{result.max_pain_shift:+d}")
        self._set("GEX", f"{result.net_gex / 1_000_000:+.1f}M")
        self._set("Gamma Flip", str(result.gamma_flip))
        self._set("Support", self._levels(result.supports))
        self._set("Resistance", self._levels(result.resistances))
        self._set("BankNifty", f"{result.banknifty_score}/100 {result.banknifty_confirmation}")
        self._set("Updated", result.timestamp)

        trade = result.trade
        targets = " / ".join(f"{target:.0f}" for target in trade.targets if target)
        self.trade_box.setText(
            f"Suggested Trade: {trade.action} {trade.strike if trade.strike else ''}\n"
            f"Entry: {trade.entry:.0f} | SL: {trade.stop_loss:.0f} | Targets: {targets or '--'} | Confidence: {trade.confidence}%"
        )
        self.greeks_box.setText(
            f"ATM Greeks: Delta {result.atm_delta:.2f} | Gamma {result.atm_gamma:.5f} | "
            f"Theta {result.atm_theta:.2f} | Vega {result.atm_vega:.2f}\n{result.atm_interpretation}"
        )
        self._render_heatmap(result.heatmap)
        if result.error:
            self._add_alert(f"Error: {result.error}")
        for alert in result.alerts:
            self._add_alert(alert, popup=True)

        if result.symbol == "SENSEX" and result.error:
            self.trade_box.setText(
                "SENSEX data source not configured.\n"
                "Use a broker/BSE option-chain API and set:\n"
                "OCP_SENSEX_API_URL and OCP_SENSEX_API_TOKEN"
            )

    def _set(self, key: str, value: str) -> None:
        self.cards[key].setText(value)

    def _levels(self, levels: List[Tuple[int, int]]) -> str:
        return ", ".join(f"{strike}({score})" for strike, score in levels) or "--"

    def _render_heatmap(self, rows: List[StrikeMetrics]) -> None:
        self.heatmap.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [str(row.strike), f"{row.ce_coi:,}", f"{row.pe_coi:,}"]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignCenter)
                if col == 1 and row.ce_coi > max(row.pe_coi, 0):
                    item.setBackground(QColor("#7f1d1d"))
                elif col == 2 and row.pe_coi > max(row.ce_coi, 0):
                    item.setBackground(QColor("#14532d"))
                else:
                    item.setBackground(QColor("#374151"))
                self.heatmap.setItem(row_index, col, item)

    def _add_alert(self, text: str, popup: bool = False) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        message = f"{stamp} | {text}"
        if self.alert_history and self.alert_history[-1].endswith(text):
            return
        self.alert_history.append(message)
        self.alerts.setPlainText("\n".join(self.alert_history[-8:]))
        QApplication.beep()
        if popup and self.tray.isVisible():
            self.tray.showMessage(APP_NAME, text, QSystemTrayIcon.Information, 5000)

    def add_journal(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        is_new = not JOURNAL_FILE.exists()
        with JOURNAL_FILE.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if is_new:
                writer.writerow(["Date", "Strike", "Entry", "Exit", "PnL", "Notes"])
            writer.writerow(
                [
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    self.journal_fields["Strike"].text(),
                    self.journal_fields["Entry"].text(),
                    self.journal_fields["Exit"].text(),
                    self.journal_fields["PnL"].text(),
                    self.notes.text(),
                ]
            )
        self._add_alert(f"Journal saved: {JOURNAL_FILE}")

    def export_journal(self) -> None:
        if not JOURNAL_FILE.exists():
            QMessageBox.information(self, APP_NAME, "No journal entries yet.")
            return
        target, _ = QFileDialog.getSaveFileName(self, "Export Journal", "trade_journal.csv", "CSV Files (*.csv)")
        if target:
            Path(target).write_text(JOURNAL_FILE.read_text(encoding="utf-8"), encoding="utf-8")
            self._add_alert(f"Journal exported: {target}")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if self.drag_pos and event.buttons() == Qt.LeftButton:
            self.move(event.globalPos() - self.drag_pos)

    def mouseReleaseEvent(self, event) -> None:
        self.drag_pos = None


def main() -> int:
    setup_logging()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = PulseWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
