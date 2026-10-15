"""Environment configuration; SQLite must be chosen explicitly for local use."""
import os
import math
from dataclasses import dataclass, field
from pathlib import Path


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes"}


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("LIBRARY_DATABASE_URL", "mysql+pymysql://library:library@127.0.0.1:3306/library?charset=utf8mb4"))
    storage_root: Path = field(default_factory=lambda: Path(os.getenv("LIBRARY_STORAGE_ROOT", "./data/library")).resolve())
    import_root: Path = field(default_factory=lambda: Path(os.getenv("LIBRARY_IMPORT_ROOT", "./data/import")).resolve())
    model_dir: Path = field(default_factory=lambda: Path(os.getenv("LIBRARY_MODEL_DIR", "./data/models")).resolve())
    admin_password: str = field(default_factory=lambda: os.getenv("LIBRARY_ADMIN_PASSWORD", "local-library"))
    secret_key: str = field(default_factory=lambda: os.getenv("LIBRARY_SECRET_KEY", "development-only-secret-change-before-deployment"))
    environment: str = field(default_factory=lambda: os.getenv("LIBRARY_ENV", "development"))
    secure_cookie: bool = field(default_factory=lambda: env_bool("LIBRARY_SECURE_COOKIE"))
    storage_require_marker: bool = field(default_factory=lambda: env_bool("LIBRARY_STORAGE_REQUIRE_MARKER"))
    allowed_origins: list[str] = field(default_factory=lambda: [o.strip().rstrip("/") for o in os.getenv("LIBRARY_ALLOWED_ORIGINS", "").split(",") if o.strip()])
    session_seconds: int = 60 * 60 * 24 * 7
    max_file_bytes: int = 128 * 1024 * 1024
    max_import_bytes: int = 512 * 1024 * 1024
    max_import_files: int = 3000
    request_interval_min_seconds: float = field(default_factory=lambda: float(os.getenv("LIBRARY_REQUEST_INTERVAL_MIN_SECONDS", os.getenv("LIBRARY_REQUEST_INTERVAL_SECONDS", "2"))))
    request_interval_max_seconds: float = field(default_factory=lambda: float(os.getenv("LIBRARY_REQUEST_INTERVAL_MAX_SECONDS", os.getenv("LIBRARY_REQUEST_INTERVAL_SECONDS", "5"))))

    def validate(self) -> None:
        if not all(math.isfinite(v) for v in (self.request_interval_min_seconds, self.request_interval_max_seconds)) or not 0 <= self.request_interval_min_seconds <= self.request_interval_max_seconds <= 60:
            raise RuntimeError("Request intervals must satisfy 0 <= min <= max <= 60")
        if self.environment == "production":
            if len(self.admin_password) < 12 or self.admin_password == "local-library":
                raise RuntimeError("Production requires LIBRARY_ADMIN_PASSWORD of at least 12 characters")
            if len(self.secret_key) < 32 or self.secret_key.startswith("development-only"):
                raise RuntimeError("Production requires a unique LIBRARY_SECRET_KEY of at least 32 characters")
            if self.database_url.startswith("sqlite"):
                raise RuntimeError("Production requires MySQL; SQLite is only for explicit development/test use")


settings = Settings()
