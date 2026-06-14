# -*- coding: utf-8 -*-
"""Cross-sectional factor scoring.

Each entry filter has an underlying *continuous* metric. Instead of collapsing
them into a conjunctive ``AND`` gate (which lets through ~0 symbols and makes
per-filter attribution impossible), this module:

1. converts every per-symbol metric into a robust cross-sectional z-score over
   the whole tradable universe each closed candle (median / MAD, winsorized),
   oriented so higher z = more favourable for this profile's side;
2. sums them into a composite with configurable weights (equal by default),
   adds a common regime *side offset* (gold/BTC macro + BTC momentum) that shifts
   the whole side's level, and ranks the universe;
3. logs the per-symbol z-vector + composite each cycle so the offline
   ``analyze_factors.py`` can Fama-MacBeth regress forward returns on the
   z-scores and prove each filter's marginal predictive power.

Factor families:
  * cross-sectional (per-symbol): macro_gap, pullback_recovery_gap, trigger_gap,
    rs60, rs30, volume_ratio, chop, plus two external (MEXC) microstructure
    factors -- spread reversion and lead-lag impulse. Each contributes ``w*z``.
  * common (per-side): gold/BTC macro budget + BTC 30m momentum, folded into a
    single additive ``side_offset`` (cannot be cross-sectional -- a market-wide
    value has zero cross-sectional variance -- so it shifts the level and gates
    via the absolute ``entry_min_composite`` threshold instead of the ranking).

Steps 1-3 are pure measurement (no order side effects). Turning the ranking into
live top-K entries is gated behind ``config.FACTOR.entry_enabled``.
"""

import math
import random
import time
from typing import Dict, List, Optional, Sequence, Tuple


class FactorScoringMixin:
    # (name, source, key, kind)
    #   source == "signal":   read key from the per-symbol signal dict.
    #   source == "external": read key from the MEXC external-price context.
    #   kind == "side": symmetric metric; multiply raw by side sign
    #                   (+1 long / -1 short) so higher means "aligned with side".
    #   kind == "raw":  already side-aware / higher-is-better.
    #   kind == "neg":  lower-is-better (negate before ranking).
    _FACTOR_METRIC_SPECS: Tuple[Tuple[str, str, str, str], ...] = (
        ("macro", "signal", "macro_gap", "side"),
        ("pullback", "signal", "pullback_recovery_gap", "raw"),
        ("trigger", "signal", "trigger_gap", "side"),
        ("rs60", "signal", "rs60", "side"),
        ("rs30", "signal", "rs30", "side"),
        ("volume", "signal", "volume_ratio", "raw"),
        ("chop", "signal", "chop", "neg"),
    )
    # External (MEXC) microstructure factors. raw values are produced by
    # _factor_external_raw already oriented for kind="side":
    #   ext_spread = -spread_bps   (long favours HTX discount, short HTX premium)
    #   ext_lead   = mexc_change_1m - htx_change_1m  (MEXC leading the move)
    _FACTOR_EXTERNAL_SPECS: Tuple[Tuple[str, str, str, str], ...] = (
        ("ext_spread", "external", "ext_spread", "side"),
        ("ext_lead", "external", "ext_lead", "side"),
    )

    def _factor_metric_specs(self) -> Tuple[Tuple[str, str, str, str], ...]:
        import config

        settings = getattr(config, "FACTOR", None)
        specs = self._FACTOR_METRIC_SPECS
        if getattr(settings, "external_factors_enabled", True):
            specs = specs + self._FACTOR_EXTERNAL_SPECS
        return specs

    def _factor_side_sign(self) -> float:
        import config

        return -1.0 if config.POSITION_SIDE == "short" else 1.0

    def _factor_directional_value(self, kind: str, raw: float, side_sign: float) -> float:
        if kind == "side":
            return side_sign * raw
        if kind == "neg":
            return -raw
        return raw

    def _factor_weights(self) -> Dict[str, float]:
        import config

        settings = getattr(config, "FACTOR", None)
        overrides = dict(getattr(settings, "weights", ()) or ())
        weights = {}
        for name, _source, _key, _kind in self._factor_metric_specs():
            # Signed weights are allowed: a negative weight inverts a factor
            # (e.g. treating relative strength as cross-sectional reversion
            # instead of momentum). Missing factors default to +1.0.
            weights[name] = self._safe_float(overrides.get(name), 1.0)
        return weights

    @staticmethod
    def _factor_median(values: Sequence[float]) -> float:
        ordered = sorted(values)
        n = len(ordered)
        if n == 0:
            return 0.0
        mid = n // 2
        if n % 2:
            return ordered[mid]
        return 0.5 * (ordered[mid - 1] + ordered[mid])

    def _factor_robust_z(self, values: Sequence[float]) -> List[float]:
        """Robust cross-sectional z via median/MAD, falling back to mean/std
        when MAD collapses (e.g. many identical values)."""
        n = len(values)
        if n == 0:
            return []
        center = self._factor_median(values)
        mad = self._factor_median([abs(v - center) for v in values])
        scale = 1.4826 * mad
        if scale <= 1e-12:
            mean = sum(values) / n
            var = sum((v - mean) ** 2 for v in values) / n if n > 0 else 0.0
            scale = math.sqrt(var)
            center = mean
        if scale <= 1e-12:
            return [0.0] * n
        return [(v - center) / scale for v in values]

    def _factor_side_budget_multiplier(self) -> float:
        import config

        getter = getattr(self, "_macro_context_for_trading", None)
        if not getter:
            return 1.0
        context = getter() or {}
        key = (
            "short_budget_multiplier"
            if config.POSITION_SIDE == "short"
            else "long_budget_multiplier"
        )
        return max(0.0, self._safe_float(context.get(key), 1.0))

    def _factor_regime_side_offset(self, btc_return: float = 0.0) -> float:
        """Common (market-wide) regime tilt added to every symbol's composite.

        Gold/BTC macro budget penalises the disfavoured side; BTC 30m momentum
        adds a symmetric tilt. A constant shift does not change within-side
        ranking -- it gates trade count via the absolute ``entry_min_composite``
        threshold (and reallocates slots when sides are pooled)."""
        import config

        settings = getattr(config, "FACTOR", None)
        if not settings or not getattr(settings, "entry_side_budget_scaling", False):
            return 0.0
        offset = 0.0
        kappa_macro = max(
            0.0, self._safe_float(getattr(settings, "macro_offset_strength", 0.0), 0.0)
        )
        if kappa_macro > 0:
            offset += kappa_macro * (self._factor_side_budget_multiplier() - 1.0)
        kappa_btc = max(
            0.0, self._safe_float(getattr(settings, "btc_offset_strength", 0.0), 0.0)
        )
        if kappa_btc > 0:
            ref = max(
                1e-9,
                self._safe_float(getattr(settings, "btc_return_reference", 0.005), 0.005),
            )
            offset += (
                self._factor_side_sign()
                * kappa_btc
                * math.tanh(self._safe_float(btc_return, 0.0) / ref)
            )
        return offset

    def _factor_external_raw(self, symbol: str) -> Optional[Dict[str, float]]:
        """Directional raw values for the external factors, or None when the
        MEXC reference is missing/invalid (factor stays neutral, never blocks)."""
        getter = getattr(self, "_external_price_context", None)
        if not getter:
            return None
        try:
            context = getter(symbol)
        except Exception:
            return None
        if not context or not context.get("valid"):
            return None
        spread = self._safe_float(context.get("spread_bps"), 0.0)
        htx_change = self._safe_float(context.get("htx_change_1m_bps"), 0.0)
        mexc_change = self._safe_float(context.get("mexc_change_1m_bps"), 0.0)
        return {"ext_spread": -spread, "ext_lead": mexc_change - htx_change}

    def _factor_external_value(
        self, symbol: str, key: str, cache: Dict[str, Optional[Dict[str, float]]]
    ) -> Optional[float]:
        if symbol not in cache:
            cache[symbol] = self._factor_external_raw(symbol)
        raws = cache[symbol]
        if not raws:
            return None
        return raws.get(key)

    def _factor_usable_symbols(self, signals: Dict[str, dict]) -> List[str]:
        usable = []
        for symbol, signal in (signals or {}).items():
            if signal and self._signal_data_valid(signal):
                usable.append(symbol)
        usable.sort()
        return usable

    def _compute_factor_scores(self, signals: Dict[str, dict]) -> dict:
        """Compute z-scores + composite and write them back into each signal.

        Returns a summary dict (empty when scoring is disabled or the universe
        is too small). Never raises for data issues; callers also guard.
        """
        import config

        settings = getattr(config, "FACTOR", None)
        if not settings or not getattr(settings, "scoring_enabled", False):
            return {}

        specs = self._factor_metric_specs()
        usable = self._factor_usable_symbols(signals)
        min_symbols = max(2, int(getattr(settings, "min_symbols", 5)))
        if len(usable) < min_symbols:
            return {}

        side_sign = self._factor_side_sign()
        winsor = max(0.0, self._safe_float(getattr(settings, "winsor", 3.0), 3.0))
        weights = self._factor_weights()

        external_cache: Dict[str, Optional[Dict[str, float]]] = {}
        z_by_metric: Dict[str, Dict[str, float]] = {}
        for name, source, key, kind in specs:
            raw_by_symbol: Dict[str, float] = {}
            for s in usable:
                if source == "external":
                    value = self._factor_external_value(s, key, external_cache)
                    if value is None:
                        continue  # missing reference -> neutral (absent => z=0)
                    raw_by_symbol[s] = self._factor_directional_value(
                        kind, value, side_sign
                    )
                else:
                    raw_by_symbol[s] = self._factor_directional_value(
                        kind, self._safe_float(signals[s].get(key), 0.0), side_sign
                    )
            present = list(raw_by_symbol.keys())
            zs = self._factor_robust_z([raw_by_symbol[s] for s in present])
            if winsor > 0:
                zs = [max(-winsor, min(winsor, z)) for z in zs]
            z_by_metric[name] = dict(zip(present, zs))

        btc_return = 0.0
        for s in usable:
            value = signals[s].get("btc_return_30m")
            if value is not None:
                btc_return = self._safe_float(value, 0.0)
                break
        side_offset = self._factor_regime_side_offset(btc_return)

        composites: Dict[str, float] = {}
        for s in usable:
            z_record = {
                name: z_by_metric[name].get(s, 0.0) for name, _src, _k, _kd in specs
            }
            composite = (
                sum(weights.get(name, 1.0) * z for name, z in z_record.items())
                + side_offset
            )
            composites[s] = composite
            signal = signals[s]
            signal["factor_z"] = z_record
            signal["factor_composite"] = composite

        ranked = sorted(usable, key=lambda s: (-composites[s], s))
        for rank, s in enumerate(ranked, start=1):
            signals[s]["factor_rank"] = rank
            signals[s]["factor_universe"] = len(usable)

        return {
            "ranked": ranked,
            "composites": composites,
            "z_by_metric": z_by_metric,
            "universe": len(usable),
            "weights": weights,
            "side_budget_multiplier": self._factor_side_budget_multiplier(),
            "side_offset": side_offset,
            "btc_return_30m": btc_return,
            "winsor": winsor,
        }

    def _log_factor_snapshot(self, signals: Dict[str, dict], summary: dict) -> None:
        """Append one compact per-cycle record holding the whole cross-section."""
        import config

        settings = getattr(config, "FACTOR", None)
        if not settings or not getattr(settings, "snapshot_logging_enabled", False):
            return
        path = getattr(self, "factor_snapshot_jsonl_path", None)
        if not path:
            return
        ranked = summary.get("ranked") or []
        if not ranked:
            return

        specs = self._factor_metric_specs()
        rows = []
        for s in ranked:
            signal = signals.get(s) or {}
            rows.append(
                {
                    "s": s,
                    "c": self._safe_float(signal.get("factor_composite"), 0.0),
                    "r": int(signal.get("factor_rank") or 0),
                    "z": signal.get("factor_z") or {},
                    "valid": int(bool(signal.get("entry_valid", False))),
                }
            )

        signal_ts = None
        for s in ranked:
            sig_ts = (signals.get(s) or {}).get("ts")
            if sig_ts is not None:
                signal_ts = sig_ts
                break

        payload = {
            "ts": int(time.time()),
            "signal_ts": signal_ts,
            "profile": self._current_profile_name(),
            "side": config.POSITION_SIDE,
            "universe": int(summary.get("universe") or 0),
            "metrics": [name for name, _src, _k, _kd in specs],
            "weights": summary.get("weights") or {},
            "side_budget_multiplier": self._safe_float(
                summary.get("side_budget_multiplier"), 1.0
            ),
            "side_offset": self._safe_float(summary.get("side_offset"), 0.0),
            "btc_return_30m": self._safe_float(summary.get("btc_return_30m"), 0.0),
            "rows": rows,
        }
        self._append_jsonl(path, payload)

    # ------------------------------------------------------------------
    # Phase 2: turn the ranking into live entries (gated, default OFF)
    # ------------------------------------------------------------------
    def _factor_entry_interval_bucket(self, signal_ts: Optional[float], now: float) -> int:
        import config

        settings = getattr(config, "FACTOR", None)
        minutes = max(0.0, self._safe_float(getattr(settings, "entry_interval_minutes", 0.0), 0.0))
        if minutes <= 0:
            return 0
        window_sec = minutes * 60.0
        # signal_ts is the closed-candle timestamp in ms; fall back to wall clock.
        anchor = self._safe_float(signal_ts, 0.0)
        anchor_sec = anchor / 1000.0 if anchor > 1e11 else (anchor or now)
        return int(anchor_sec // window_sec)

    def _factor_select_entries(
        self,
        competing: Sequence[str],
        signals: Dict[str, dict],
        summary: dict,
        now: Optional[float] = None,
        signal_ts: Optional[float] = None,
    ) -> dict:
        """Pick the top-K competitors by composite (+ random controls) for the
        current interval. Regime tilt is already baked into the composite via the
        side offset, so selection is top-K intersected with the absolute
        ``entry_min_composite`` threshold. Stable within an interval bucket."""
        import config

        now = time.time() if now is None else now
        settings = getattr(config, "FACTOR", None)
        composites = summary.get("composites") or {}
        competing = [s for s in competing if s in composites]
        if not competing:
            return {
                "top": [],
                "random": [],
                "allowed": set(),
                "bucket": self._factor_entry_interval_bucket(signal_ts, now),
            }

        ranked = sorted(competing, key=lambda s: (-self._safe_float(composites.get(s), 0.0), s))
        top_k = max(0, int(getattr(settings, "entry_top_k", 0)))

        min_composite = self._safe_float(getattr(settings, "entry_min_composite", 0.0), 0.0)
        top = [
            s
            for s in ranked[:top_k]
            if self._safe_float(composites.get(s), 0.0) + 1e-12 >= min_composite
        ]

        bucket = self._factor_entry_interval_bucket(signal_ts, now)
        random_n = max(0, int(getattr(settings, "entry_random_control", 0)))
        random_pick: List[str] = []
        pool = [s for s in ranked if s not in set(top)]
        if random_n > 0 and pool:
            seed_src = f"{self._current_profile_name()}|{bucket}|{len(competing)}"
            rng = random.Random(seed_src)
            random_pick = rng.sample(pool, min(random_n, len(pool)))

        allowed = set(top) | set(random_pick)
        return {
            "top": top,
            "random": random_pick,
            "allowed": allowed,
            "bucket": bucket,
            "ranked": ranked,
            "effective_top_k": top_k,
        }


__all__ = ["FactorScoringMixin"]
