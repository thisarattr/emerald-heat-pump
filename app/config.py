import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path("data")
    timezone: str = "Australia/Melbourne"
    driver: str = "demo"
    device_id: str = ""
    secure_cookies: bool = True
    credential_key_file: Path | None = None

    def __post_init__(self):
        ZoneInfo(self.timezone)
        if self.driver not in {"demo", "emerald"}:
            raise ValueError("HEATER_DRIVER must be demo or emerald")

    @classmethod
    def from_env(cls):
        return cls(
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            timezone=os.getenv("TZ", "Australia/Melbourne"),
            driver=os.getenv("HEATER_DRIVER", "demo"),
            device_id=os.getenv("EMERALD_DEVICE_ID", ""),
            secure_cookies=os.getenv("COOKIE_SECURE", "true").lower() == "true",
            credential_key_file=(
                Path(value) if (value := os.getenv("CREDENTIAL_KEY_FILE", "")) else None
            ),
        )
