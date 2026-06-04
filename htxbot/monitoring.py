# -*- coding: utf-8 -*-

import csv
import json
import logging
import os
import re
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from pathlib import Path
from typing import Sequence
from typing import Any, Dict, Optional

import config

from .concurrency import instance_rlock
from .fileio import replace_path_with_retry


_monitoring_global_lock = threading.RLock()
_CSV_STREAM_COPY_CHUNK_SIZE = 1024 * 1024


class MonitoringMixin:
    def _append_headered_csv_row(self, path: Optional[Path], header: Sequence[str], row: Sequence[Any]):
        if not path:
            return
        with _monitoring_global_lock:
            with instance_rlock(self, "_monitoring_lock"):
                self._rotate_csv_if_needed(path, header)
                with path.open("a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(row)

    def _replace_path_with_retry(self, src: Path, dst: Path, attempts: int = 30, delay_sec: float = 0.05):
        replace_path_with_retry(
            src,
            dst,
            attempts=attempts,
            initial_delay_sec=delay_sec,
            max_delay_sec=0.5,
            replace_func=os.replace,
        )

    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"htx_futures_bot.{config.BOT_NAME}")
        logger.setLevel(getattr(logging, config.MONITORING.log_level.upper(), logging.INFO))
        logger.handlers.clear()
        logger.propagate = False

        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
        logger.addHandler(handler)
        return logger

    def _ensure_headered_csv_file(self, path: Path, header: Sequence[str]):
        path.parent.mkdir(parents=True, exist_ok=True)
        header = list(header)
        if not path.exists() or path.stat().st_size == 0:
            with path.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(header)
            return

        try:
            with path.open("r", newline="", encoding="utf-8") as f:
                first_row = next(csv.reader(f), [])
        except Exception as exc:
            self.log.warning("Could not verify CSV header for %s: %s", path, exc)
            return

        normalized_first_row = self._normalize_csv_header_row(first_row)
        if normalized_first_row == header:
            return

        if normalized_first_row and normalized_first_row[0] == header[0]:
            self._rewrite_headered_csv_file(path, header, normalized_first_row)
            return

        self._prepend_csv_header(path, header)

    def _csv_tmp_path(self, path: Path) -> Path:
        return path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")

    def _normalize_csv_header_row(self, row: Sequence[str]) -> list:
        normalized = list(row or [])
        if normalized:
            normalized[0] = normalized[0].lstrip("\ufeff")
        return normalized

    def _legacy_csv_row_dict(self, source_header: Sequence[str], values: Sequence[Any]) -> Dict[str, Any]:
        row = {}
        for index, name in enumerate(source_header):
            if not name:
                continue
            row[str(name)] = values[index] if index < len(values) else ""
        return row

    def _rewrite_headered_csv_file(self, path: Path, header: Sequence[str], normalized_first_row: Sequence[str]):
        tmp_path = self._csv_tmp_path(path)
        header = list(header)
        source_header = list(normalized_first_row)
        try:
            with path.open("r", newline="", encoding="utf-8") as src, tmp_path.open("w", newline="", encoding="utf-8") as dst:
                reader = csv.reader(src)
                first_row = next(reader, [])
                if first_row:
                    source_header = self._normalize_csv_header_row(first_row)
                writer = csv.writer(dst)
                writer.writerow(header)
                for values in reader:
                    row = self._legacy_csv_row_dict(source_header, values)
                    self._apply_legacy_csv_aliases(row)
                    writer.writerow([row.get(name, "") for name in header])
            self._replace_path_with_retry(tmp_path, path)
        except Exception as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.log.warning("Could not replace CSV header for %s: %s", path, exc)

    def _apply_legacy_csv_aliases(self, row: Dict[str, Any]):
        legacy_aliases = {"ema50": "ema30", "ema100": "ema60"}
        for current_name, legacy_name in legacy_aliases.items():
            if legacy_name in row and current_name not in row:
                row[current_name] = row[legacy_name]

    def _prepend_csv_header(self, path: Path, header: Sequence[str]):
        tmp_path = self._csv_tmp_path(path)
        try:
            with tmp_path.open("w", newline="", encoding="utf-8") as dst:
                csv.writer(dst).writerow(header)
                with path.open("r", newline="", encoding="utf-8") as src:
                    while True:
                        chunk = src.read(_CSV_STREAM_COPY_CHUNK_SIZE)
                        if not chunk:
                            break
                        dst.write(chunk)
            self._replace_path_with_retry(tmp_path, path)
        except Exception as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.log.warning("Could not add CSV header for %s: %s", path, exc)

    def _ensure_csv_file(self):
        self._ensure_headered_csv_file(self.csv_path, self.CSV_HEADER)

    def _ensure_cycle_stats_file(self):
        self._ensure_headered_csv_file(self.cycle_stats_path, self.CYCLE_STATS_HEADER)

    def _ensure_macro_csv_file(self):
        path = getattr(self, "macro_csv_path", None)
        if path:
            self._ensure_headered_csv_file(path, self.MACRO_CSV_HEADER)

    def _ensure_external_price_csv_file(self):
        path = getattr(self, "external_price_csv_path", None)
        if path:
            self._ensure_headered_csv_file(path, self.EXTERNAL_PRICE_CSV_HEADER)

    def _ensure_account_pnl_csv_file(self):
        path = getattr(self, "account_pnl_csv_path", None)
        if path:
            self._ensure_headered_csv_file(path, self.ACCOUNT_PNL_CSV_HEADER)
    def _ensure_jsonl_file(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)

    def _ensure_signal_analytics_files(self):
        csv_path = getattr(self, "signal_analytics_csv_path", None)
        if csv_path:
            self._ensure_headered_csv_file(csv_path, self.SIGNAL_ANALYTICS_CSV_HEADER)
        jsonl_path = getattr(self, "signal_analytics_jsonl_path", None)
        if jsonl_path:
            self._ensure_jsonl_file(jsonl_path)

    def _ensure_diagnostics_files(self):
        csv_path = getattr(self, "diagnostics_csv_path", None)
        if csv_path:
            self._ensure_headered_csv_file(csv_path, self.DIAGNOSTICS_CSV_HEADER)
        jsonl_path = getattr(self, "diagnostics_jsonl_path", None)
        if jsonl_path:
            self._ensure_jsonl_file(jsonl_path)

    def _csv_archive_path(self, path: Path) -> Path:
        archive_dir = Path(config.MONITORING.csv_archive_dir)
        if not archive_dir.is_absolute():
            archive_dir = path.parent / archive_dir
        archive_dir.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        millis = int((time.time() % 1) * 1000)
        return archive_dir / f"{path.stem}.{timestamp}_{millis:03d}_{os.getpid()}_{time.time_ns()}{path.suffix}"

    def _rotate_csv_if_needed(self, path: Path, header: Sequence[str]):
        max_bytes = max(0, int(config.MONITORING.csv_rotate_max_bytes or 0))
        if max_bytes <= 0 or not path.exists() or path.stat().st_size < max_bytes:
            return

        archive_path = self._csv_archive_path(path)
        try:
            self._replace_path_with_retry(path, archive_path)
            self._ensure_headered_csv_file(path, header)
            self.log.info("CSV log rotated: %s -> %s", path, archive_path)
        except Exception as exc:
            self.log.warning("Could not rotate CSV log %s: %s", path, exc)

    def _rotate_jsonl_if_needed(self, path: Path):
        max_bytes = max(0, int(config.MONITORING.csv_rotate_max_bytes or 0))
        if max_bytes <= 0 or not path.exists() or path.stat().st_size < max_bytes:
            return

        archive_path = self._csv_archive_path(path)
        try:
            self._replace_path_with_retry(path, archive_path)
            self._ensure_jsonl_file(path)
            self.log.info("JSONL log rotated: %s -> %s", path, archive_path)
        except Exception as exc:
            self.log.warning("Could not rotate JSONL log %s: %s", path, exc)

    def _append_jsonl(self, path: Optional[Path], payload: Dict[str, Any]):
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with _monitoring_global_lock:
            with instance_rlock(self, "_monitoring_lock"):
                self._rotate_jsonl_if_needed(path)
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(self._sanitize_for_log(payload), ensure_ascii=False, sort_keys=True) + "\n")

    def _sanitize_for_log(self, value: Any, depth: int = 0) -> Any:
        if depth > 8:
            return "<max_depth>"
        if isinstance(value, dict):
            clean = {}
            for key, item in value.items():
                key_text = str(key)
                lowered = key_text.lower()
                if any(secret in lowered for secret in ("apikey", "api_key", "secret", "password", "token", "signature")):
                    clean[key_text] = "<redacted>"
                else:
                    clean[key_text] = self._sanitize_for_log(item, depth + 1)
            return clean
        if isinstance(value, (list, tuple, set)):
            return [self._sanitize_for_log(item, depth + 1) for item in list(value)[:200]]
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str):
                value = self._redact_sensitive_text(value)
                if len(value) > 4000:
                    return value[:4000] + "...<truncated>"
            return value
        return self._redact_sensitive_text(value)

    def _redact_sensitive_text(self, value: Any) -> str:
        text = str(value)
        if not text:
            return text

        def redact_url(match: re.Match) -> str:
            raw_url = match.group(0)
            try:
                parsed = urlsplit(raw_url)
                if not parsed.query:
                    return raw_url
                pairs = []
                changed = False
                for key, item in parse_qsl(parsed.query, keep_blank_values=True):
                    lowered = key.lower()
                    if any(secret in lowered for secret in ("accesskeyid", "api_key", "apikey", "secret", "password", "token", "signature")):
                        pairs.append((key, "<redacted>"))
                        changed = True
                    else:
                        pairs.append((key, item))
                if not changed:
                    return raw_url
                return urlunsplit(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        parsed.path,
                        urlencode(pairs, doseq=True),
                        parsed.fragment,
                    )
                )
            except Exception:
                return raw_url

        text = re.sub(r"https?://[^\s\"'<>]+", redact_url, text)
        patterns = (
            r"(?i)(AccessKeyId=)[^&\s\"'<>]+",
            r"(?i)(Signature=)[^&\s\"'<>]+",
            r"(?i)(signature[\"'\s:=]+)[^,;&\s\"'<>]+",
            r"(?i)(api[_-]?key[\"'\s:=]+)[^,;&\s\"'<>]+",
            r"(?i)(api[_-]?secret[\"'\s:=]+)[^,;&\s\"'<>]+",
            r"(?i)(secret[\"'\s:=]+)[^,;&\s\"'<>]+",
            r"(?i)(password[\"'\s:=]+)[^,;&\s\"'<>]+",
            r"(?i)(token[\"'\s:=]+)[^,;&\s\"'<>]+",
        )
        for pattern in patterns:
            text = re.sub(pattern, lambda match: f"{match.group(1)}<redacted>", text)
        return text

    def _monitoring_float(self, value: Any, default: float = 0.0) -> float:
        safe_float = getattr(self, "_safe_float", None)
        if safe_float:
            return safe_float(value, default)
        try:
            if value is None:
                return float(default)
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _fmt_monitoring_float(self, value: Any, precision: int = 8) -> str:
        numeric = self._monitoring_float(value, 0.0)
        return f"{numeric:.{precision}f}" if numeric else ""

    def _current_profile_name(self) -> str:
        return str(getattr(self, "profile_name", config.BOT_NAME) or config.BOT_NAME)

    def _signal_id(self, symbol: str = "", signal: Optional[dict] = None) -> str:
        signal = signal or {}
        signal_ts = signal.get("ts") or signal.get("signal_ts") or ""
        strategy = signal.get("strategy_name") or "ema_pullback"
        if not symbol:
            symbol = str(signal.get("symbol") or "")
        return f"{self._current_profile_name()}:{symbol}:{signal_ts}:{strategy}"

    def _operation_id(
        self,
        event: str,
        symbol: str = "",
        order_id: str = "",
        signal: Optional[dict] = None,
        suffix: str = "",
    ) -> str:
        signal_ts = (signal or {}).get("ts") or ""
        millis = int(time.time() * 1000)
        parts = [self._current_profile_name(), event, symbol, order_id or str(signal_ts), str(millis)]
        if suffix:
            parts.append(str(suffix))
        return ":".join(str(part).replace(":", "_") for part in parts if part not in (None, ""))

    def _new_cycle_id(self, symbol: str, signal: Optional[dict] = None) -> str:
        return self._operation_id("cycle", symbol=symbol, signal=signal)

    def _selected_config_snapshot(self) -> dict:
        return {
            "position_side": config.POSITION_SIDE,
            "entry_side": config.ENTRY_SIDE,
            "exit_side": config.EXIT_SIDE,
            "leverage": config.RISK.leverage,
            "margin_mode": config.RISK.margin_mode,
            "max_active_positions": config.RISK.max_active_positions,
            "position_budget_fraction": config.BUYING.position_budget_fraction,
            "ladder_fractions": tuple(config.BUYING.ladder_fractions),
            "ladder_offsets": tuple(config.BUYING.ladder_offsets),
        }

    def _signal_block_reason(self, signal: Optional[dict]) -> str:
        if not signal:
            return "signal_missing"
        if signal.get("entry_valid"):
            return ""
        detailed_reason = getattr(self, "_entry_raw_signal_block_reason", None)
        if callable(detailed_reason):
            return detailed_reason(signal)
        if not signal.get("valid"):
            return "signal_invalid"
        for key in (
            "macro_valid",
            "pullback_valid",
            "trigger_valid",
            "rs_confirm_valid",
            "btc_entry_valid",
            "volume_valid",
            "chop_valid",
            "market_structure_valid",
        ):
            if key in signal and not signal.get(key):
                return key
        return "entry_valid_false"

    def _external_context_from_cache(self, symbol: str) -> dict:
        cache = getattr(self, "_external_price_context_cache", None)
        if isinstance(cache, dict) and symbol in cache:
            cached = cache.get(symbol)
            return dict(cached) if isinstance(cached, dict) else {}
        return {}

    def _record_signal_analytics(
        self,
        decision: str,
        symbol: str = "",
        signal: Optional[dict] = None,
        block_reason: str = "",
        external_context: Optional[dict] = None,
        planned_budget: float = 0.0,
        planned_orders: int = 0,
        planned_notional: float = 0.0,
        placed_orders: int = 0,
        filled_notional: float = 0.0,
        realized_pnl_quote: float = 0.0,
        operation_id: str = "",
        order_id: str = "",
        cycle_id: str = "",
        context: Optional[dict] = None,
    ):
        signal = signal or {}
        symbol = symbol or str(signal.get("symbol") or "")
        external_context = external_context if isinstance(external_context, dict) else self._external_context_from_cache(symbol)
        signal_id = self._signal_id(symbol, signal)
        signal_ts = signal.get("ts") or ""
        strategy_name = signal.get("strategy_name") or "ema_pullback"
        side = config.POSITION_SIDE
        block_reason = block_reason or self._signal_block_reason(signal)
        state = None
        if symbol and hasattr(self, "states") and symbol in self.states:
            state = self.states[symbol]
        if not cycle_id and state is not None:
            cycle_id = str(getattr(state, "cycle_id", "") or "")

        row = [
            int(time.time()),
            self._current_profile_name(),
            symbol,
            side,
            signal_id,
            signal_ts,
            strategy_name,
            int(bool(signal.get("valid", False))),
            int(bool(signal.get("entry_valid", False))),
            int(bool(signal.get("add_valid", False))),
            decision,
            block_reason,
            self._fmt_monitoring_float(signal.get("score"), 8),
            self._fmt_monitoring_float(signal.get("rs30"), 8),
            self._fmt_monitoring_float(signal.get("rs60"), 8),
            self._fmt_monitoring_float(signal.get("ema50", signal.get("ema_trigger_fast")), 12),
            self._fmt_monitoring_float(signal.get("ema100", signal.get("ema_trigger_slow")), 12),
            self._fmt_monitoring_float(signal.get("ema1d", signal.get("ema_pullback_fast")), 12),
            self._fmt_monitoring_float(signal.get("ema2d", signal.get("ema_pullback_slow")), 12),
            self._fmt_monitoring_float(signal.get("ema25d", signal.get("ema_macro_fast")), 12),
            self._fmt_monitoring_float(signal.get("ema50d", signal.get("ema_macro_slow")), 12),
            self._fmt_monitoring_float(signal.get("macro_gap"), 8),
            self._fmt_monitoring_float(signal.get("trigger_gap"), 8),
            self._fmt_monitoring_float(signal.get("pullback_depth"), 8),
            self._fmt_monitoring_float(signal.get("btc_return_30m"), 8),
            self._fmt_monitoring_float(signal.get("volatility"), 8),
            self._fmt_monitoring_float(signal.get("budget_multiplier"), 8),
            self._fmt_monitoring_float(signal.get("ladder_multiplier"), 8),
            int(bool(signal.get("volume_valid", False))) if "volume_valid" in signal else "",
            self._fmt_monitoring_float(signal.get("volume_ratio"), 8),
            self._fmt_monitoring_float(signal.get("volume_spike_ratio"), 8),
            signal.get("volume_spike_direction", ""),
            int(bool(signal.get("volume_profile_valid", False))) if "volume_profile_valid" in signal else "",
            int(bool(signal.get("volume_profile_break", False))) if "volume_profile_break" in signal else "",
            self._fmt_monitoring_float(signal.get("volume_profile_poc"), 12),
            self._fmt_monitoring_float(signal.get("volume_profile_value_area_low"), 12),
            self._fmt_monitoring_float(signal.get("volume_profile_value_area_high"), 12),
            signal.get("volume_reason", ""),
            signal.get("macro_regime", ""),
            int(bool(external_context.get("valid", False))) if external_context else "",
            int(bool(external_context.get("stale", False))) if external_context else "",
            self._fmt_monitoring_float(external_context.get("spread_bps"), 8) if external_context else "",
            self._fmt_monitoring_float(planned_budget, 8),
            int(planned_orders or 0),
            self._fmt_monitoring_float(planned_notional, 8),
            int(placed_orders or 0),
            self._fmt_monitoring_float(filled_notional, 8),
            self._fmt_monitoring_float(realized_pnl_quote, 8),
        ]

        csv_path = getattr(self, "signal_analytics_csv_path", None)
        if csv_path:
            self._append_headered_csv_row(csv_path, self.SIGNAL_ANALYTICS_CSV_HEADER, row)

        payload = {
            "ts": int(time.time()),
            "profile": self._current_profile_name(),
            "symbol": symbol,
            "side": side,
            "decision": decision,
            "block_reason": block_reason,
            "signal_id": signal_id,
            "operation_id": operation_id,
            "order_id": order_id,
            "cycle_id": cycle_id,
            "signal": signal,
            "external_context": external_context,
            "macro_context": (getattr(self, "signal_cache", {}) or {}).get("macro", {}),
            "config": self._selected_config_snapshot(),
            "metrics": {
                "planned_budget": planned_budget,
                "planned_orders": planned_orders,
                "planned_notional": planned_notional,
                "placed_orders": placed_orders,
                "filled_notional": filled_notional,
                "realized_pnl_quote": realized_pnl_quote,
            },
            "context": context or {},
        }
        self._append_jsonl(getattr(self, "signal_analytics_jsonl_path", None), payload)

    def _append_csv(
        self,
        level: str,
        event: str,
        message: str = "",
        symbol: str = "",
        side: str = "",
        order_id: str = "",
        price: float = 0.0,
        amount: float = 0.0,
        filled: float = 0.0,
        remaining: float = 0.0,
        position_size: float = 0.0,
        entry_price: float = 0.0,
        notional: float = 0.0,
        fee_quote: float = 0.0,
        fee_currency: str = "",
        fill_source: str = "",
        rs30: float = 0.0,
        rs60: float = 0.0,
        ema30: float = 0.0,
        ema60: float = 0.0,
        reason: str = "",
        exception_type: str = "",
        error_code: str = "",
        retryable: Any = "",
        **_ignored,
    ):
        if symbol and hasattr(self, "states") and symbol in self.states:
            state = self.states[symbol]
            if not position_size:
                position_size = state.position_size
            if not entry_price:
                entry_price = state.entry_price
            if not rs30:
                rs30 = state.last_rs30
            if not rs60:
                rs60 = state.last_rs60
            if not ema30:
                ema30 = state.last_ema30
            if not ema60:
                ema60 = state.last_ema60

        self._append_headered_csv_row(
            self.csv_path,
            self.CSV_HEADER,
            [
                int(time.time()),
                level,
                event,
                symbol,
                side,
                order_id,
                f"{price:.12f}" if price else "",
                f"{amount:.12f}" if amount else "",
                f"{filled:.12f}" if filled else "",
                f"{remaining:.12f}" if remaining else "",
                f"{position_size:.12f}" if position_size else "",
                f"{entry_price:.12f}" if entry_price else "",
                f"{notional:.12f}" if notional else "",
                f"{fee_quote:.12f}" if fee_quote else "",
                fee_currency,
                fill_source,
                f"{rs30:.8f}" if rs30 else "",
                f"{rs60:.8f}" if rs60 else "",
                f"{ema30:.12f}" if ema30 else "",
                f"{ema60:.12f}" if ema60 else "",
                reason,
                message,
                exception_type,
                error_code,
                retryable,
            ],
        )

    def _append_cycle_stats_row(self, row: dict):
        self._append_headered_csv_row(
            self.cycle_stats_path,
            self.CYCLE_STATS_HEADER,
            [
                row.get("symbol", ""),
                int(row.get("opened_at", 0)) if row.get("opened_at") else "",
                int(row.get("closed_at", 0)) if row.get("closed_at") else "",
                row.get("leverage", ""),
                row.get("margin_mode", ""),
                f"{row.get('planned_budget', 0):.8f}",
                f"{row.get('total_entry_notional', 0):.8f}",
                f"{row.get('total_exit_notional', 0):.8f}",
                f"{row.get('average_entry_price', 0):.12f}",
                f"{row.get('average_exit_price', 0):.12f}",
                f"{row.get('buy_fees', 0):.8f}",
                f"{row.get('sell_fees', 0):.8f}",
                f"{row.get('realized_pnl_quote', 0):.8f}",
                f"{row.get('realized_pnl_percent_on_notional', 0):.8f}",
                f"{row.get('realized_pnl_percent_on_margin', 0):.8f}",
                f"{row.get('holding_minutes', 0):.2f}",
                row.get("max_buy_stage", 0),
                int(bool(row.get("frozen_no_more_buys", False))),
                row.get("close_reason", ""),
                f"{row.get('entry_rs30', 0):.8f}",
                f"{row.get('entry_rs60', 0):.8f}",
                f"{row.get('entry_ema30', 0):.12f}",
                f"{row.get('entry_ema60', 0):.12f}",
                row.get("strategy_name", ""),
                f"{row.get('entry_ema25d', 0):.12f}",
                f"{row.get('entry_ema50d', 0):.12f}",
                f"{row.get('entry_ema1d', 0):.12f}",
                f"{row.get('entry_ema2d', 0):.12f}",
                f"{row.get('entry_ema50', 0):.12f}",
                f"{row.get('entry_ema100', 0):.12f}",
                f"{row.get('entry_btc_return_30m', 0):.8f}",
                row.get("max_averaging_stage", 0),
                int(bool(row.get("breakeven_activated", False))),
            ],
        )

    def _append_macro_csv(self, context: dict):
        path = getattr(self, "macro_csv_path", None)
        if not path:
            return

        def fmt(value: object) -> str:
            try:
                return f"{float(value):.8f}"
            except (TypeError, ValueError):
                return ""

        self._append_headered_csv_row(
            path,
            self.MACRO_CSV_HEADER,
            [
                int(context.get("ts") or time.time()),
                getattr(self, "profile_name", config.BOT_NAME),
                context.get("regime", ""),
                context.get("gold_symbol", ""),
                context.get("btc_symbol", ""),
                fmt(context.get("gold_rsi", 0.0)),
                fmt(context.get("btc_rsi", 0.0)),
                fmt(context.get("rsi_spread", 0.0)),
                fmt(context.get("gold_btc_ratio_return", 0.0)),
                fmt(context.get("gold_return", 0.0)),
                fmt(context.get("btc_return", 0.0)),
                fmt(context.get("macro_direction_score", 0.0)),
                fmt(context.get("long_budget_multiplier", 1.0)),
                fmt(context.get("short_budget_multiplier", 1.0)),
                fmt(context.get("ladder_multiplier", 1.0)),
                int(bool(context.get("disable_new_entries", False))),
                int(bool(context.get("disable_averaging", False))),
                context.get("reason", ""),
            ],
        )

    def _append_external_price_csv(self, context: dict):
        path = getattr(self, "external_price_csv_path", None)
        if not path:
            return

        def fmt(value: object) -> str:
            try:
                return f"{float(value):.8f}"
            except (TypeError, ValueError):
                return ""

        self._append_headered_csv_row(
            path,
            self.EXTERNAL_PRICE_CSV_HEADER,
            [
                int(context.get("ts") or time.time()),
                getattr(self, "profile_name", config.BOT_NAME),
                context.get("symbol", ""),
                context.get("mexc_symbol", ""),
                int(bool(context.get("valid", False))),
                int(bool(context.get("stale", False))),
                fmt(context.get("htx_bid", 0.0)),
                fmt(context.get("htx_ask", 0.0)),
                fmt(context.get("htx_mid", 0.0)),
                fmt(context.get("mexc_bid", 0.0)),
                fmt(context.get("mexc_ask", 0.0)),
                fmt(context.get("mexc_mid", 0.0)),
                fmt(context.get("mexc_bid_qty", 0.0)),
                fmt(context.get("mexc_ask_qty", 0.0)),
                fmt(context.get("mexc_bid_notional", 0.0)),
                fmt(context.get("mexc_ask_notional", 0.0)),
                fmt(context.get("spread_bps", 0.0)),
                fmt(context.get("spread_bps_30s_avg", 0.0)),
                fmt(context.get("spread_bps_2m_avg", 0.0)),
                fmt(context.get("spread_bps_10m_avg", 0.0)),
                fmt(context.get("spread_bps_zscore", 0.0)),
                fmt(context.get("htx_change_30s_bps", 0.0)),
                fmt(context.get("mexc_change_30s_bps", 0.0)),
                fmt(context.get("htx_change_1m_bps", 0.0)),
                fmt(context.get("mexc_change_1m_bps", 0.0)),
                int(float(context.get("age_ms", 0.0) or 0.0)),
                context.get("reason", ""),
            ],
        )

    def _append_account_pnl_csv(self, context: dict):
        path = getattr(self, "account_pnl_csv_path", None)
        if not path:
            return

        def fmt(value: object) -> str:
            try:
                return f"{float(value):.8f}"
            except (TypeError, ValueError):
                return ""

        self._append_headered_csv_row(
            path,
            self.ACCOUNT_PNL_CSV_HEADER,
            [
                int(context.get("ts") or time.time()),
                getattr(self, "profile_name", config.BOT_NAME),
                fmt(context.get("open_pnl", 0.0)),
                fmt(context.get("unrealized_pnl", 0.0)),
                fmt(context.get("realized_open_pnl", 0.0)),
                fmt(context.get("open_notional", 0.0)),
                fmt(context.get("open_pnl_rate", 0.0)),
                int(context.get("position_count") or 0),
                int(context.get("history_samples") or 0),
                fmt(context.get("min_open_pnl", 0.0)),
                fmt(context.get("p25_open_pnl", 0.0)),
                fmt(context.get("median_open_pnl", 0.0)),
                fmt(context.get("p75_open_pnl", 0.0)),
                fmt(context.get("max_open_pnl", 0.0)),
                fmt(context.get("previous_open_pnl", 0.0)),
                fmt(context.get("delta_open_pnl", 0.0)),
                context.get("reason", ""),
            ],
        )
    def _diagnostic_error_code(self, exc: Optional[Exception], message: str = "") -> str:
        text = f"{message} {exc or ''}"
        for pattern in (
            r'"(?:err[_-]?code|error[_-]?code|code)"\s*:\s*"?([0-9A-Za-z_-]+)"?',
            r"\berr(?:or)?[_ -]?code[\"'\s:=]+\"?([0-9A-Za-z_-]+)\"?",
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1)
        return ""

    def _diagnostic_category(
        self,
        event: str,
        reason: str = "",
        message: str = "",
        exception: Optional[Exception] = None,
        category: str = "",
    ) -> str:
        if category:
            return category
        is_transient = bool(getattr(self, "_is_transient_exchange_error", lambda _exc: False)(exception)) if exception else False
        text = f"{event} {reason} {message} {exception or ''}".lower()
        if is_transient or any(item in text for item in ("timeout", "network", "connection", "rate limit")):
            return "network"
        if "config" in text or "credential" in text:
            return "config"
        if any(item in text for item in ("csv", "file", "cache", "lock", "read", "write", "json")):
            return "io"
        if "state" in text or "position" in text or "sync" in text:
            return "state"
        if any(item in text for item in ("htx", "exchange", "api", "fetch", "order", "leverage", "balance", "ticker")):
            return "api"
        if any(item in text for item in ("signal", "ema", "strategy", "macro", "external_price")):
            return "strategy"
        return "environment"

    def _diagnostic_from_exception(self, exc: Optional[Exception], message: str = "", retryable: Optional[bool] = None) -> dict:
        if exc is None:
            return {
                "exception_type": "",
                "error_code": self._diagnostic_error_code(None, message),
                "retryable": bool(retryable) if retryable is not None else False,
            }
        if retryable is None:
            retryable = bool(getattr(self, "_is_transient_exchange_error", lambda _exc: False)(exc))
        return {
            "exception_type": type(exc).__name__,
            "error_code": self._diagnostic_error_code(exc, message),
            "retryable": bool(retryable),
        }

    def _record_diagnostic(
        self,
        severity: str,
        category: str,
        event: str,
        message: str,
        symbol: str = "",
        operation_id: str = "",
        signal_id: str = "",
        order_id: str = "",
        reason: str = "",
        exception: Optional[Exception] = None,
        retryable: Optional[bool] = None,
        attempt: Any = "",
        hostname: str = "",
        context: Optional[dict] = None,
    ):
        message = self._redact_sensitive_text(message)
        severity = str(severity or "").lower()
        if severity == "critical":
            severity = "fault"
        category = self._diagnostic_category(event, reason=reason, message=message, exception=exception, category=category)
        exception_info = self._diagnostic_from_exception(exception, message=message, retryable=retryable)
        retryable_value = bool(exception_info.get("retryable", False))
        row = [
            int(time.time()),
            self._current_profile_name(),
            severity,
            category,
            event,
            symbol,
            operation_id,
            signal_id,
            order_id,
            exception_info.get("exception_type", ""),
            exception_info.get("error_code", ""),
            message,
            reason,
            int(retryable_value),
            attempt,
            hostname,
        ]

        csv_path = getattr(self, "diagnostics_csv_path", None)
        if csv_path:
            self._append_headered_csv_row(csv_path, self.DIAGNOSTICS_CSV_HEADER, row)

        payload = {
            "ts": int(time.time()),
            "profile": self._current_profile_name(),
            "severity": severity,
            "category": category,
            "event": event,
            "symbol": symbol,
            "operation_id": operation_id,
            "signal_id": signal_id,
            "order_id": order_id,
            "message": message,
            "reason": reason,
            "attempt": attempt,
            "hostname": hostname,
            "exception": {
                **exception_info,
                "message": self._redact_sensitive_text(exception) if exception else "",
            },
            "context": context or {},
        }
        self._append_jsonl(getattr(self, "diagnostics_jsonl_path", None), payload)

    def _record_config_warnings(self):
        seen = getattr(self, "_recorded_config_warnings", set())
        for message in getattr(config, "CONFIG_WARNINGS", []):
            if message in seen:
                continue
            seen.add(message)
            self._record_diagnostic(
                "warning",
                "config",
                "config_warning",
                str(message),
                reason="config_warning",
            )
        self._recorded_config_warnings = seen

    def _compact_log_message(self, message: str) -> str:
        text = self._redact_sensitive_text(message)
        html_markers = ("<!DOCTYPE html", "<html", "<head", "<body")
        marker_positions = [text.find(marker) for marker in html_markers if marker in text]
        if marker_positions:
            head = text[: min(marker_positions)].strip()
            if len(head) > 500:
                head = f"{head[:500]}..."
            return f"{head} [html response omitted]" if head else "[html response omitted]"
        if len(text) > 2000:
            return f"{text[:2000]}... [truncated]"
        return text

    def _log_event(self, level: str, message: str, event: str, **kwargs):
        diagnostic_exception = kwargs.pop("exception", None)
        diagnostic_category = kwargs.pop("category", "")
        diagnostic_retryable = kwargs.pop("retryable", None)
        diagnostic_attempt = kwargs.pop("attempt", "")
        diagnostic_hostname = kwargs.pop("hostname", "")
        operation_id = kwargs.pop("operation_id", "")
        signal_id = kwargs.pop("signal_id", "")
        diagnostic_context = kwargs.pop("diagnostic_context", None)

        message = self._compact_log_message(message)
        exception_info = self._diagnostic_from_exception(
            diagnostic_exception,
            message=message,
            retryable=diagnostic_retryable,
        )
        level_upper = str(level or "INFO").upper()
        if level_upper in {"FAULT", "CRITICAL"}:
            log_method = self.log.critical
        else:
            log_method = getattr(self.log, str(level).lower(), self.log.info)
        log_method(message)
        try:
            self._append_csv(
                level=level_upper,
                event=event,
                message=message,
                exception_type=str(exception_info.get("exception_type", "")),
                error_code=str(exception_info.get("error_code", "")),
                retryable=int(bool(exception_info.get("retryable", False))) if diagnostic_exception or diagnostic_retryable is not None else "",
                **kwargs,
            )
        except Exception as exc:
            if not getattr(self, "_csv_log_failed_once", False):
                self._csv_log_failed_once = True
                self.log.warning("Could not append CSV event log: %s", exc)
        if level_upper in {"WARNING", "ERROR", "FAULT", "CRITICAL"}:
            severity = "fault" if level_upper in {"FAULT", "CRITICAL"} else level_upper.lower()
            self._record_diagnostic(
                severity,
                diagnostic_category,
                event,
                message,
                symbol=str(kwargs.get("symbol") or ""),
                operation_id=operation_id,
                signal_id=signal_id,
                order_id=str(kwargs.get("order_id") or ""),
                reason=str(kwargs.get("reason") or ""),
                exception=diagnostic_exception,
                retryable=diagnostic_retryable,
                attempt=diagnostic_attempt,
                hostname=diagnostic_hostname,
                context=diagnostic_context,
            )
