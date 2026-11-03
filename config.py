"""Settings with defaults, min/max validation, aliases and change_log."""
import time

DEFAULTS = {
 "scanner.min_liquidity_usd": 40000, "scanner.max_top10_pct": 25, "scanner.max_h1_change_pct": 80,
 "scanner.poll_s": 60, "scanner.min_pool_age_min": 10, "scanner.max_wait_min": 60, "scanner.runner_h24_pct": 300,
 "auditor.enabled": True, "auditor.max_dev_pct": 5, "auditor.max_top10_pct": 25, "auditor.min_lp_locked_pct": 99,
 "auditor.min_pool_age_min": 10, "auditor.max_bundled": 5, "auditor.max_delegated_accounts": 10, "auditor.retries": 3,
 "auditor.bundles_no_data": "warn", "auditor.bundles_no_data_penalty": 10,
 "analyst.mode": "rules", "analyst.min_alive_6h": 0.70, "analyst.min_organic": 0.65, "analyst.max_chase": 0.30,
 "analyst.min_score": 70, "analyst.timeout_s": 10, "analyst.alive_volume_h1": 25000, "analyst.organic_buys_h1": 150,
 "analyst.bundled_penalty": 0.5, "analyst.bundled_penalty_from_wallets": 3, "analyst.flush_drop_m5_pct": 5,
 "analyst.chase_h1_pct": 80,
 "executor.stack_pct": 8, "executor.max_pool_pct": 2, "executor.min_ticket_usd": 47.50, "executor.below_min": "bump",
 "executor.recheck_liq_drop_pct": 20, "executor.recheck_price_up_pct": 15, "executor.confirm_window_min": 5,
 "risk.poll_s": 15, "risk.stop_pct": 6, "risk.trail_start_pct": 20, "risk.trail_pct": 12, "risk.timeout_h": 3,
 "risk.timeout_min_gain_pct": 5, "risk.confirm_window_min": 2,
 "desk.mode": "PAPER", "desk.paper_start_stack": 200, "desk.report_time": "08:00", "desk.notifier": "telegram",
 "desk.rpc_url": "https://api.mainnet-beta.solana.com", "desk.push_rejects": False, "desk.fees_ref_discount": 0.10,
 "desk.fomo_handle": "fulfilling", "desk.analyst_mode": "rules",
 # Phase 2 additions (bracketed numbers in the spec that had no setting yet)
 "auditor.max_liq_drop_pct": 50, "risk.backoff_s": 30, "scanner.backoff_429_s": 30, "desk.live_stack": 200,
}
# (min, max) for numerics; tuple of allowed values for enums. Ranges are judgement calls (spec gave none).
RANGES = {
 "scanner.min_liquidity_usd": (1000, 10_000_000), "scanner.max_top10_pct": (1, 100), "scanner.max_h1_change_pct": (1, 1000),
 "scanner.poll_s": (10, 3600), "scanner.min_pool_age_min": (0, 1440), "scanner.max_wait_min": (1, 1440),
 "scanner.runner_h24_pct": (10, 100000),
 "auditor.max_dev_pct": (0, 100), "auditor.max_top10_pct": (1, 100), "auditor.min_lp_locked_pct": (0, 100),
 "auditor.min_pool_age_min": (0, 1440), "auditor.max_bundled": (0, 100), "auditor.max_delegated_accounts": (0, 1000),
 "auditor.retries": (0, 10), "auditor.bundles_no_data_penalty": (0, 100),
 "analyst.min_alive_6h": (0, 1), "analyst.min_organic": (0, 1), "analyst.max_chase": (0, 1), "analyst.min_score": (0, 100),
 "analyst.timeout_s": (1, 120), "analyst.alive_volume_h1": (0, 10_000_000), "analyst.organic_buys_h1": (0, 100000),
 "analyst.bundled_penalty": (0, 1), "analyst.bundled_penalty_from_wallets": (1, 100), "analyst.flush_drop_m5_pct": (0, 100),
 "analyst.chase_h1_pct": (0, 1000),
 "executor.stack_pct": (0.5, 100), "executor.max_pool_pct": (0.1, 100), "executor.min_ticket_usd": (5, 10000),
 "executor.recheck_liq_drop_pct": (0, 100), "executor.recheck_price_up_pct": (0, 1000), "executor.confirm_window_min": (1, 60),
 "risk.poll_s": (5, 600), "risk.stop_pct": (1, 50), "risk.trail_start_pct": (1, 1000), "risk.trail_pct": (1, 90),
 "risk.timeout_h": (0.25, 168), "risk.timeout_min_gain_pct": (0, 1000), "risk.confirm_window_min": (1, 60),
 "desk.paper_start_stack": (10, 1_000_000), "auditor.max_liq_drop_pct": (0, 100), "risk.backoff_s": (1, 600),
 "scanner.backoff_429_s": (1, 600), "desk.live_stack": (0, 10_000_000), "desk.fees_ref_discount": (0, 1),
 "analyst.mode": ("rules", "llm"), "desk.analyst_mode": ("rules", "llm"), "executor.below_min": ("bump", "skip"),
 "desk.mode": ("PAPER", "LIVE"), "auditor.bundles_no_data": ("warn", "red"), "desk.notifier": ("telegram", "push", "none"),
}
ALIASES = {"stop": "risk.stop_pct", "trail": "risk.trail_pct", "trail_start": "risk.trail_start_pct",
           "stack": "executor.stack_pct", "ticket": "executor.min_ticket_usd", "score": "analyst.min_score",
           "liquidity": "scanner.min_liquidity_usd", "mode": "desk.mode", "timeout": "risk.timeout_h"}


class SettingError(ValueError):
    pass


def resolve(key):
    k = ALIASES.get(key, key)
    if k not in DEFAULTS:
        raise SettingError(f"unknown setting {key}")
    return k


def coerce_validate(key, value):
    d = DEFAULTS[key]
    r = RANGES.get(key)
    if isinstance(d, bool):
        if isinstance(value, str):
            v = value.strip().lower()
            if v not in ("true", "false", "on", "off", "1", "0", "yes", "no"):
                raise SettingError(f"{key} must be true or false")
            return v in ("true", "on", "1", "yes")
        return bool(value)
    if isinstance(d, (int, float)):
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise SettingError(f"{key} must be a number")
        lo, hi = r
        if not lo <= v <= hi:
            raise SettingError(f"{key} out of range: must be between {lo} and {hi}")
        return int(v) if isinstance(d, int) and v.is_integer() else v
    v = str(value)
    if key == "desk.report_time":
        try:
            h, m = v.split(":"); assert len(h) == 2 and len(m) == 2 and 0 <= int(h) <= 23 and 0 <= int(m) <= 59
        except Exception:
            raise SettingError(f"{key} must be HH:MM between 00:00 and 23:59")
    if isinstance(r, tuple) and r and isinstance(r[0], str) and v not in r:
        raise SettingError(f"{key} must be one of {', '.join(r)}")
    return v


class Settings:
    """Backed by db settings/change_log tables when conn given, else in-memory."""

    def __init__(self, conn=None):
        self.conn = conn
        self.values = dict(DEFAULTS)
        if conn is not None:
            import json
            for k, v in conn.execute("SELECT key, value FROM settings"):
                if k in DEFAULTS:
                    self.values[k] = json.loads(v)

    def get(self, key):
        return self.values[resolve(key)]

    def set(self, key, value, who="owner"):
        import json
        k = resolve(key)
        new = coerce_validate(k, value)
        old = self.values[k]
        self.values[k] = new
        if self.conn is not None:
            ts = time.time()
            self.conn.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE "
                              "SET value=excluded.value, updated_at=excluded.updated_at", (k, json.dumps(new), ts))
            self.conn.execute("INSERT INTO change_log(ts,key,old,new,who) VALUES(?,?,?,?,?)",
                              (ts, k, json.dumps(old), json.dumps(new), who))
            self.conn.commit()
        return old, new


def fmt_money(x):
    return f"{x:,.2f}"


def ticket_check(stack, stack_pct, min_ticket_usd, below_min):
    """Returns (ticket_usd or None, warning or None). min_stack = min_ticket/(stack_pct/100)."""
    min_stack = min_ticket_usd / (stack_pct / 100)
    ticket = stack * stack_pct / 100
    if stack >= min_stack:
        return ticket, None
    if below_min == "bump":
        p = round(min_ticket_usd / stack * 100, 1)
        return min_ticket_usd, f"tickets bumped to ${min_ticket_usd:.2f} = {p}% of stack"
    return None, f"every coin will be skipped, stack under ${fmt_money(min_stack)}"
