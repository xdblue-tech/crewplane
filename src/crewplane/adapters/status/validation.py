from __future__ import annotations

from rich.console import Console

from crewplane.core.config import Config


def validate_reporter_request(
    config: Config,
    run_id: str,
    console: Console,
) -> None:
    if not isinstance(config, Config):
        raise TypeError("config must be a Config instance")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    if not isinstance(console, Console):
        raise TypeError("console must be a Console instance")
