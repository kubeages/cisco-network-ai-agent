"""Runtime configuration with JSON persistence (PVC or ConfigMap).

Allows changing LLM, APIC, ND, Splunk, AI Defense settings without pod restarts.
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Mutable configuration fields
MUTABLE_FIELDS: Dict[str, type] = {
    # LLM Primary
    "llm_provider": str,
    "llm_model": str,
    "llm_base_url": str,
    "llm_api_key": str,
    "llm_temperature": float,
    # LLM Alternative
    "llm_alt_enabled": bool,
    "llm_alt_provider": str,
    "llm_alt_model": str,
    "llm_alt_base_url": str,
    "llm_alt_api_key": str,
    "llm_alt_temperature": float,
    # APIC
    "apic_url": str,
    "apic_user": str,
    "apic_password": str,
    "apic_fabric_name": str,
    # Nexus Dashboard
    "nd_url": str,
    "nd_user": str,
    "nd_password": str,
    # Neo4j
    "neo4j_uri": str,
    "neo4j_user": str,
    "neo4j_password": str,
    # Splunk
    "splunk_enabled": bool,
    "otel_endpoint": str,
    "otel_service_name": str,
    "otel_environment": str,
    "splunk_access_token": str,
    # AI Defense
    "ai_defense_enabled": bool,
    "ai_defense_api_key": str,
    "ai_defense_endpoint": str,
    "ai_defense_inspect_prompts": bool,
    "ai_defense_inspect_responses": bool,
}


class RuntimeConfig:
    """Thread-safe runtime configuration with JSON persistence."""

    def __init__(self, config_path: str = "/data/config.json"):
        self._path = Path(config_path)
        self._lock = threading.Lock()
        self._overrides: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        """Load configuration from disk or environment variables."""
        if self._path.exists():
            try:
                self._overrides = json.loads(self._path.read_text())
                logger.info(f"Runtime config loaded from {self._path}, keys: {list(self._overrides.keys())}")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load runtime config from {self._path}: {e}")
                self._overrides = {}
        else:
            # Initialize from environment variables
            self._overrides = {}
            for key in MUTABLE_FIELDS:
                env_value = os.getenv(key.upper())
                if env_value is not None:
                    self._overrides[key] = self._coerce_type(key, env_value)
            self._save()

    def _save(self) -> None:
        """Persist configuration to disk."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._overrides, indent=2))
            logger.info(f"Runtime config saved to {self._path}")
        except OSError as e:
            logger.error(f"Failed to save runtime config to {self._path}: {e}")

    def _coerce_type(self, key: str, value: Any) -> Any:
        """Coerce value to expected type."""
        expected_type = MUTABLE_FIELDS.get(key, str)
        if expected_type is bool:
            if isinstance(value, str):
                return value.lower() in ("true", "1", "yes", "on")
            return bool(value)
        elif expected_type is float:
            return float(value)
        elif expected_type is int:
            return int(value)
        return str(value)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value."""
        with self._lock:
            return self._overrides.get(key, default)

    def get_all(self) -> Dict[str, Any]:
        """Get all configuration values."""
        with self._lock:
            return dict(self._overrides)

    def update(self, changes: Dict[str, Any]) -> Dict[str, str]:
        """Update configuration values.

        Args:
            changes: Dictionary of field names to new values.

        Returns:
            Dictionary of field -> status ("updated" or error message).
        """
        results: Dict[str, str] = {}
        with self._lock:
            for key, value in changes.items():
                if key not in MUTABLE_FIELDS:
                    results[key] = "unknown or immutable field"
                    continue
                try:
                    coerced_value = self._coerce_type(key, value)
                    self._overrides[key] = coerced_value
                    results[key] = "updated"
                except (ValueError, TypeError) as exc:
                    results[key] = f"invalid value: {exc}"

            self._save()

        logger.info(f"Runtime config updated: {results}")
        return results


# Global runtime config instance
runtime_config: RuntimeConfig | None = None


def init_runtime_config(config_path: str = "/data/config.json") -> RuntimeConfig:
    """Initialize the global runtime config."""
    global runtime_config
    runtime_config = RuntimeConfig(config_path)
    return runtime_config


def get_runtime_config() -> RuntimeConfig:
    """Get the global runtime config instance."""
    if runtime_config is None:
        raise RuntimeError("Runtime config not initialized. Call init_runtime_config() first.")
    return runtime_config
