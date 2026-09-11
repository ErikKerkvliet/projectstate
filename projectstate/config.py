"""Environment-driven configuration. Everything runtime-tunable lives in the settings table instead."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def usd_to_micro(s: str | float) -> int:
    """'0.005' -> 5000 micro-dollars."""
    from decimal import Decimal

    return int((Decimal(str(s)) * 1_000_000).to_integral_value())


@dataclass
class Settings:
    base_url: str = field(default_factory=lambda: _env("BASE_URL", "http://127.0.0.1:8000").rstrip("/"))
    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH", "./data/projectstate.db")))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", "dev-secret-change-me"))
    admin_username: str = field(default_factory=lambda: _env("ADMIN_USERNAME", "admin"))
    admin_password_hash: str = field(default_factory=lambda: _env("ADMIN_PASSWORD_HASH", ""))
    # x402 is the only payment rail: off | mock (local mock facilitator) | testnet | live
    x402_mode: str = field(default_factory=lambda: _env("X402_MODE", "testnet"))
    x402_facilitator_url: str = field(default_factory=lambda: _env("X402_FACILITATOR_URL", "https://x402.org/facilitator"))
    x402_network: str = field(default_factory=lambda: _env("X402_NETWORK", "eip155:84532"))
    x402_pay_to: str = field(default_factory=lambda: _env("X402_PAY_TO", ""))
    cdp_api_key_id: str = field(default_factory=lambda: _env("CDP_API_KEY_ID", ""))
    cdp_api_key_secret: str = field(default_factory=lambda: _env("CDP_API_KEY_SECRET", ""))
    # Defaults seeded into the settings table on first start (editable in the dashboard afterwards)
    default_prices_usd: dict[str, str] = field(
        default_factory=lambda: {
            "project_open": _env("PRICE_PROJECT_OPEN", "0.002"),
            "project_status": _env("PRICE_PROJECT_STATUS", "0.002"),
            "remember": _env("PRICE_REMEMBER", "0.002"),
            "recall": _env("PRICE_RECALL", "0.005"),
            "update": _env("PRICE_UPDATE", "0.002"),
        }
    )
    default_daily_call_cap: int = field(default_factory=lambda: int(_env("DEFAULT_DAILY_CALL_CAP", "2000")))
    default_daily_spend_cap_usd: str = field(default_factory=lambda: _env("DEFAULT_DAILY_SPEND_CAP_USD", "5.00"))
    default_per_minute_cap: int = field(default_factory=lambda: int(_env("DEFAULT_PER_MINUTE_CAP", "60")))
    x402_pack_usd: str = field(default_factory=lambda: _env("X402_PACK_USD", "0.10"))
    x402_min_payment_usd: str = field(default_factory=lambda: _env("X402_MIN_PAYMENT_USD", "0.01"))
    monthly_cost_usd: str = field(default_factory=lambda: _env("MONTHLY_COST_USD", "6.00"))
    alarm_calls_per_hour: int = field(default_factory=lambda: int(_env("ALARM_CALLS_PER_HOUR", "300")))
    dedup_window_seconds: int = field(default_factory=lambda: int(_env("DEDUP_WINDOW_SECONDS", "120")))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"


settings = Settings()
