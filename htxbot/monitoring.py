# -*- coding: utf-8 -*-

import csv
import logging
import os
import time
from pathlib import Path
from typing import Sequence

import config


class MonitoringMixin:
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

        normalized_first_row = list(first_row)
        if normalized_first_row:
            normalized_first_row[0] = normalized_first_row[0].lstrip("\ufeff")
        if normalized_first_row == list(header):
            return

        if normalized_first_row and normalized_first_row[0] == header[0]:
            tmp_path = path.with_name(f"{path.name}.tmp")
            try:
                with path.open("r", newline="", encoding="utf-8") as src, tmp_path.open("w", newline="", encoding="utf-8") as dst:
                    reader = csv.reader(src)
                    writer = csv.writer(dst)
                    next(reader, None)
                    writer.writerow(header)
                    writer.writerows(reader)
                os.replace(tmp_path, path)
                return
            except Exception as exc:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                self.log.warning("Could not replace CSV header for %s: %s", path, exc)
                return

        tmp_path = path.with_name(f"{path.name}.tmp")
        try:
            original_content = path.read_text(encoding="utf-8")
            with tmp_path.open("w", newline="", encoding="utf-8") as dst:
                csv.writer(dst).writerow(header)
                dst.write(original_content)
            os.replace(tmp_path, path)
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

    def _csv_archive_path(self, path: Path) -> Path:
        archive_dir = Path(config.MONITORING.csv_archive_dir)
        if not archive_dir.is_absolute():
            archive_dir = path.parent / archive_dir
        archive_dir.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        millis = int((time.time() % 1) * 1000)
        return archive_dir / f"{path.stem}.{timestamp}_{millis:03d}{path.suffix}"

    def _rotate_csv_if_needed(self, path: Path, header: Sequence[str]):
        max_bytes = max(0, int(config.MONITORING.csv_rotate_max_bytes or 0))
        if max_bytes <= 0 or not path.exists() or path.stat().st_size < max_bytes:
            return

        archive_path = self._csv_archive_path(path)
        try:
            os.replace(path, archive_path)
            self._ensure_headered_csv_file(path, header)
            self.log.info("CSV log rotated: %s -> %s", path, archive_path)
        except Exception as exc:
            self.log.warning("Could not rotate CSV log %s: %s", path, exc)

    def _append_csv(
        self,
        level: str,
        event: str,
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
    ):
        if symbol and symbol in self.states:
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

        self._rotate_csv_if_needed(self.csv_path, self.CSV_HEADER)
        with self.csv_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
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
                ]
            )

    def _append_cycle_stats_row(self, row: dict):
        self._rotate_csv_if_needed(self.cycle_stats_path, self.CYCLE_STATS_HEADER)
        with self.cycle_stats_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
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
                ]
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

        self._rotate_csv_if_needed(path, self.MACRO_CSV_HEADER)
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
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
                    fmt(context.get("long_budget_multiplier", 1.0)),
                    fmt(context.get("short_budget_multiplier", 1.0)),
                    fmt(context.get("ladder_multiplier", 1.0)),
                    int(bool(context.get("disable_new_entries", False))),
                    int(bool(context.get("disable_averaging", False))),
                    int(bool(context.get("disable_recovery", False))),
                    context.get("reason", ""),
                ]
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

        self._rotate_csv_if_needed(path, self.EXTERNAL_PRICE_CSV_HEADER)
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
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
                ]
            )

    def _log_event(self, level: str, message: str, event: str, **kwargs):
        reason = str(kwargs.get("reason") or "")
        if config.RUNTIME.dry_run and not message.startswith("[DRY-RUN]"):
            message = f"[DRY-RUN] {message}"
            if not reason:
                kwargs["reason"] = "dry_run"

        log_method = getattr(self.log, level.lower(), self.log.info)
        log_method(message)
        self._append_csv(level=level.upper(), event=event, **kwargs)
