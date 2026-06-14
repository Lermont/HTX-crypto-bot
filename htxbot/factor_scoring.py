# -*- coding: utf-8 -*-
"""Cross-sectional factor scoring.

Each entry filter (macro trend, pullback recovery, trigger cross, relative
strength, volume, choppiness, raw signal score) has an underlying *continuous*
metric. Instead of collapsing them into a conjunctive ``AND`` gate (which lets
through ~0 symbols and makes per-filter attribution impossible), this module:

1. converts every metric into a robust cross-sectional z-score over the whole
   tradable universe each closed candle (median / MAD, winsorized), oriented so
   higher z = more favourable for this profile's side;
2. sums them into a composite score with configurable weights (equal by
   default) and ranks the universe;
3. logs the per-symbol z-vector + composite each cycle so the offline
   ``analyze_factors.py`` can run a Fama-MacBeth regression of forward returns
   on the z-scores and prove each filter's marginal predictive power.

Steps 1-3 are pure measurement (no order side effects). Turning the ranking
into live top-K entries is gated separately behind ``config.FACTOR.entry_enabled``
(see ``strategy_filters``/``strategy_entry``).
"""

import math
import random
import time
from typing import Dict, List, Optional, Sequence, Tuple


class FactorScoringMixin:
    # (name, signal_key, kind)
    #   kind == "side": symmetric metric; multiply raw by side sign
    #                   (+1 long / -1 short) so higher means "aligned with side".
    #   kind == "raw":  already side-aware / higher-is-better.
    #   kind == "neg":  lower-is-better (negate before ranking).
    _FACTOR_METRIC_SPECS: Tuple[Tuple[str, str, str], ...] = (
        ("macro", "macro_gap", "side"),
        ("pullback", "pullback_recovery_gap", "raw"),
        ("trigger", "trigger_gap", "side"),
        ("rs60", "rs60", "side"),
        ("rs30", "rs30", "side"),
        ("volume", "volume_ratio", "raw"),
        ("chop", "chop", "neg"),
        ("score", "score", "raw"),
    )

    def _factor_metric_specs(self) -> Tuple[Tuple[str, str, str], ...]:
        return self._FACTOR_METRIC_SPECS

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
        for name, _key, _kind in self._factor_metric_specs():
            weights[name] = max(0.0, self._safe_float(overrides.get(name), 1.0))
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

        z_by_metric: Dict[str, Dict[str, float]] = {}
        for name, key, kind in specs:
            raws = [
                self._factor_directional_value(
                    kind, self._safe_float(signals[s].get(key), 0.0), side_sign
                )
                for s in usable
            ]
            zs = self._factor_robust_z(raws)
            if winsor > 0:
                zs = [max(-winsor, min(winsor, z)) for z in zs]
            z_by_metric[name] = dict(zip(usable, zs))

        composites: Dict[str, float] = {}
        for s in usable:
            z_record = {name: z_by_metric[name].get(s, 0.0) for name, _k, _kd in specs}
            composite = sum(weights.get(name, 1.0) * z for name, z in z_record.items())
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
            "metrics": [name for name, _k, _kd in specs],
            "weights": summary.get("weights") or {},
            "side_budget_multiplier": self._safe_float(
                summary.get("side_budget_multiplier"), 1.0
            ),
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
        current interval. Selection is stable within an interval bucket so the
        same names are chosen on every poll until the bucket rolls over."""
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
        if getattr(settings, "entry_side_budget_scaling", False) and top_k > 0:
            multiplier = self._safe_float(summary.get("side_budget_multiplier"), 1.0)
            top_k = int(math.floor(top_k * max(0.0, multiplier) + 1e-9))

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
