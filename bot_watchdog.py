# -*- coding: utf-8 -*-
"""Watchdog: keeps bot.py running and restarts it when the heartbeat goes stale.

The bot writes a timestamp to the heartbeat file every HEARTBEAT_INTERVAL_SEC
(default 10s) from inside the trading loop. If the file stops updating (frozen
loop, e.g. the 2026-06-12 DNS hang) or the process exits, the watchdog kills
and restarts it.

Usage (instead of `python bot.py`):
    python bot_watchdog.py
    python bot_watchdog.py --stale-sec 600 --startup-grace-sec 900
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime


def log(message: str, logfile: str):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} | watchdog | {message}"
    print(line, flush=True)
    try:
        with open(logfile, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def heartbeat_is_stale(
    now: float,
    process_started_at: float,
    heartbeat_mtime: float,
    stale_sec: float,
    startup_grace_sec: float,
) -> bool:
    """Pure decision: restart when the freshest liveness signal is too old.

    Heartbeats written before the current process started are ignored (they
    belong to the previous run); until the first own heartbeat appears the
    startup grace window applies.
    """
    own_heartbeat = heartbeat_mtime if heartbeat_mtime > process_started_at else 0.0
    if own_heartbeat <= 0:
        return now - process_started_at > max(stale_sec, startup_grace_sec)
    return now - own_heartbeat > stale_sec


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heartbeat", default="bot_heartbeat.txt",
                        help="Heartbeat file written by the bot (HEARTBEAT_FILE).")
    parser.add_argument("--stale-sec", type=float, default=600.0,
                        help="Restart when the heartbeat is older than this (default 600).")
    parser.add_argument("--startup-grace-sec", type=float, default=900.0,
                        help="Allowance for setup/markets load before the first heartbeat (default 900).")
    parser.add_argument("--restart-delay-sec", type=float, default=15.0,
                        help="Pause between restarts (default 15).")
    parser.add_argument("--check-interval-sec", type=float, default=10.0,
                        help="How often to poll the heartbeat (default 10).")
    parser.add_argument("--logfile", default="watchdog.log")
    parser.add_argument("command", nargs="*",
                        help="Bot command (default: <python> bot.py)")
    return parser.parse_args()


def main():
    args = parse_args()
    command = args.command or [sys.executable, "bot.py"]
    restarts = 0
    while True:
        process = subprocess.Popen(command)
        started_at = time.time()
        log(f"started: pid={process.pid} cmd={' '.join(command)} (restart #{restarts})", args.logfile)
        reason = ""
        try:
            while True:
                time.sleep(max(1.0, args.check_interval_sec))
                exit_code = process.poll()
                if exit_code is not None:
                    reason = f"process exited with code {exit_code}"
                    break
                try:
                    heartbeat_mtime = os.path.getmtime(args.heartbeat)
                except OSError:
                    heartbeat_mtime = 0.0
                if heartbeat_is_stale(
                    time.time(), started_at, heartbeat_mtime,
                    args.stale_sec, args.startup_grace_sec,
                ):
                    age = time.time() - max(heartbeat_mtime, started_at)
                    reason = f"heartbeat stale for {age:.0f}s; killing pid={process.pid}"
                    process.kill()
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        log("process did not die within 30s after kill", args.logfile)
                    break
        except KeyboardInterrupt:
            log("interrupted; stopping bot and exiting", args.logfile)
            process.kill()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            return
        restarts += 1
        log(f"restarting: {reason}", args.logfile)
        time.sleep(max(0.0, args.restart_delay_sec))


if __name__ == "__main__":
    main()
