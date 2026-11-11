"""Settings, all overridable with environment variables."""

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
SERVICE_DIR = PACKAGE_DIR.parent


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # Where incident folders are written.
    incidents_dir: Path = field(
        default_factory=lambda: Path(os.getenv("RESPONDER_INCIDENTS_DIR", SERVICE_DIR / "incidents"))
    )
    # Repository the coding assistant works in (the order tracker app).
    repo_dir: Path = field(
        default_factory=lambda: Path(os.getenv("RESPONDER_REPO_DIR", SERVICE_DIR.parent)).resolve()
    )

    # Observability backends (as reachable from the host).
    prometheus_url: str = field(default_factory=lambda: os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090"))
    loki_url: str = field(default_factory=lambda: os.getenv("LOKI_URL", "http://127.0.0.1:3100"))
    tempo_url: str = field(default_factory=lambda: os.getenv("TEMPO_URL", "http://127.0.0.1:3200"))
    service_name: str = field(default_factory=lambda: os.getenv("RESPONDER_SERVICE_NAME", "order-tracker"))
    # How far before the alert's startsAt to look for evidence.
    lookback_minutes: int = field(default_factory=lambda: int(os.getenv("RESPONDER_LOOKBACK_MINUTES", "15")))
    max_logs: int = field(default_factory=lambda: int(os.getenv("RESPONDER_MAX_LOGS", "50")))
    max_traces: int = field(default_factory=lambda: int(os.getenv("RESPONDER_MAX_TRACES", "5")))

    # Coding assistant.
    agent_enabled: bool = field(default_factory=lambda: _bool("RESPONDER_AGENT_ENABLED", True))
    # Command to run; the prompt is passed on stdin.
    agent_command: list[str] = field(
        default_factory=lambda: shlex.split(os.getenv("RESPONDER_AGENT_CMD", "")) or [
            "claude", "-p",
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits",
            # Allowlist: read/edit code and run the tests. Anything else is
            # denied because nobody is there to approve it in headless mode.
            "--allowedTools",
            "Read,Grep,Glob,Edit,Write,"
            "Bash(uv run --frozen pytest:*),Bash(git status:*),Bash(git diff:*),Bash(git log:*)",
            "--disallowedTools",
            "Bash(git commit:*),Bash(git push:*),Bash(git reset:*),Bash(git checkout:*),Bash(docker:*)",
        ]
    )
    agent_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("RESPONDER_AGENT_TIMEOUT_SECONDS", "1800"))
    )
    # Grafana re-sends firing alerts; don't open a new incident for the same
    # alert fingerprint within this period.
    dedup_seconds: int = field(default_factory=lambda: int(os.getenv("RESPONDER_DEDUP_SECONDS", "3600")))
