import secrets
import time


# 8 hex chars = 32 bits of randomness on top of a 1-second timestamp.
# At 100 inserts/sec the per-second collision probability is ~1.2e-6 (vs.
# ~3e-3 with the prior 4-char base36 ids — birthday-paradox math: n²/2N
# where n=100 collisions in flight, N=alphabet**chars). Failure mode is a
# loud IntegrityError, but ids are minted on the hot purchase / order /
# reminder paths, so the bound matters under festival-spike load.
_RAND_HEX_CHARS = 8


def generate_id(prefix: str = "") -> str:
    """Generate a short, sortable, unique ID. e.g. 'ord_1712500000_a3x91f7c'"""
    ts = int(time.time())
    rand = secrets.token_hex(_RAND_HEX_CHARS // 2)
    if prefix:
        return f"{prefix}_{ts}_{rand}"
    return f"{ts}_{rand}"
