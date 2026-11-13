"""Content-addressed research artifacts, separate from scanner and paper state."""

import os
import re
from pathlib import Path

from app.db.hashing import canonical_json, sha256_hex
from app.strategies.configuration import StrategyConfiguration


class ConfigurationStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid configuration hash")
        return self.root / f"{digest}.json"

    def save(self, configuration: StrategyConfiguration) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = configuration.to_dict()
        if "configuration_version" in payload:
            identity = {key: payload[key] for key in ("strategy_id", "strategy_version", "execution_mode",
                                                       "adapter_version", "engine_version")}
            versions = self.root / "versions"
            versions.mkdir(exist_ok=True)
            index = versions / sha256_hex(canonical_json(identity))
            self._write_once(index, configuration.sha256, "IMMUTABLE_CONFIGURATION_VERSION_CONFLICT")
        path = self._path(configuration.sha256)
        self._write_once(path, configuration.canonical, "CONFIGURATION_ARTIFACT_HASH_MISMATCH")
        return configuration.sha256

    @staticmethod
    def _write_once(path: Path, content: str, conflict: str) -> None:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != content:
                raise ValueError(conflict)
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())

    def load(self, digest: str) -> StrategyConfiguration:
        content = self._path(digest).read_text(encoding="utf-8")
        if sha256_hex(content) != digest:
            raise ValueError("CONFIGURATION_ARTIFACT_HASH_MISMATCH")
        return StrategyConfiguration(content)

    def save_result(self, payload: dict) -> str:
        return ConfigurationStore(self.root / "research").save(StrategyConfiguration(canonical_json(payload)))
