"""Config persistence (Phase 2.5): load/save RuntimeState ↔ JSON.

Precedence: class defaults < env vars < config file < API mutations.
API mutations call `save_config_file`, which writes the full state. Startup
calls `load_config_file` after _state_from_env to overlay any persisted
overrides.
"""
import dataclasses
import json
import os
import tempfile
from pathlib import Path

from .state import RuntimeState


def _default_path() -> str:
    return os.environ.get("HFT_CONFIG_PATH", "qpulse_config.json")


def load_config_file(state: RuntimeState, path: str | None = None) -> bool:
    """Apply persisted overrides to `state` in-place. Returns True if loaded."""
    p = Path(path or _default_path())
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return False
    for field in dataclasses.fields(state):
        if field.name in data:
            try:
                setattr(state, field.name, data[field.name])
            except Exception:
                pass
    return True


def save_config_file(state: RuntimeState, path: str | None = None) -> None:
    """Atomic write of RuntimeState to JSON (via temp file + replace)."""
    p = Path(path or _default_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        f.name: getattr(state, f.name)
        for f in dataclasses.fields(state)
        # skip non-primitive fields (overrides dict is fine as dict)
    }
    tmp = tempfile.NamedTemporaryFile(
        mode="w", delete=False, dir=str(p.parent), prefix=p.name + ".",
        suffix=".tmp",
    )
    try:
        json.dump(data, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, p)
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
