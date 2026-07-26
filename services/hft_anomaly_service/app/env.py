"""Auto-load KEY=VALUE pairs from .env if present.

Searches (in order): cwd / <cwd>/../.env / <cwd>/../../.env - so the same
.env works whether you run from the project root or from
services/hft_anomaly_service/. Existing shell env vars ALWAYS WIN over .env.

Every CLI entrypoint calls `load_dotenv()` at startup so Alpaca keys and
config live in one place.
"""
import os
from pathlib import Path


def load_dotenv(verbose: bool = True) -> bool:
    cwd = Path.cwd()
    candidates = [cwd / ".env", cwd.parent / ".env", cwd.parent.parent / ".env"]
    for p in candidates:
        if not p.is_file():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            # Strip inline comments (unless value is explicitly quoted)
            if v and v[0] not in ("'", '"'):
                hash_ix = v.find(" #")
                if hash_ix != -1:
                    v = v[:hash_ix].rstrip()
            # Strip matched surrounding quotes
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            if k and k not in os.environ:
                os.environ[k] = v
        if verbose:
            print(f"[env] loaded {p}")
        return True
    return False
