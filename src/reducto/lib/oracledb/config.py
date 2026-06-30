from __future__ import annotations

import os
from typing import Any, Literal, cast
from dataclasses import dataclass

ReductoEnvironment = Literal["production", "eu", "au"]


@dataclass(frozen=True)
class OracleConfig:
    user: str
    password: str
    dsn: str
    config_dir: str | None = None
    wallet_location: str | None = None
    wallet_password: str | None = None

    @classmethod
    def from_env(cls) -> OracleConfig:
        return cls(
            user=_required_env("ORACLE_USER"),
            password=_required_env("ORACLE_PASSWORD"),
            dsn=_required_env("ORACLE_DSN"),
            config_dir=os.getenv("ORACLE_CONFIG_DIR"),
            wallet_location=os.getenv("ORACLE_WALLET_LOCATION"),
            wallet_password=os.getenv("ORACLE_WALLET_PASSWORD"),
        )


def connect_oracle(config: OracleConfig | None = None) -> Any:
    """Create a python-oracledb connection from environment-backed settings."""
    import oracledb

    resolved = config or OracleConfig.from_env()
    kwargs: dict[str, str] = {
        "user": resolved.user,
        "password": resolved.password,
        "dsn": resolved.dsn,
    }
    if resolved.config_dir:
        kwargs["config_dir"] = resolved.config_dir
    if resolved.wallet_location:
        kwargs["wallet_location"] = resolved.wallet_location
    if resolved.wallet_password:
        kwargs["wallet_password"] = resolved.wallet_password
    return oracledb.connect(**kwargs)


def reducto_environment() -> ReductoEnvironment | None:
    value = os.getenv("REDUCTO_ENVIRONMENT")
    if value is None or value == "":
        return None
    if value not in ("production", "eu", "au"):
        raise ValueError("REDUCTO_ENVIRONMENT must be one of: production, eu, au.")
    return cast("ReductoEnvironment", value)


def vector_dimensions_from_env(default: int = 384) -> int:
    raw_value = os.getenv("ORACLE_VECTOR_DIMENSIONS")
    if not raw_value:
        return default
    try:
        dimensions = int(raw_value)
    except ValueError as exc:
        raise ValueError("ORACLE_VECTOR_DIMENSIONS must be an integer.") from exc
    if dimensions < 1:
        raise ValueError("ORACLE_VECTOR_DIMENSIONS must be positive.")
    return dimensions


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value
