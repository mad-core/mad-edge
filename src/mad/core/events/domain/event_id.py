"""UUIDv7 minting for events (RFC 9562).

Pure function; no module-level state. Lex-sortable across processes
because the first 48 bits encode Unix-millisecond mint time. Replaceable
with stdlib ``uuid.uuid7()`` once Python ships it (3.14+).

See ADR-0005 for the rationale and the in-millisecond ordering caveat.
"""

from __future__ import annotations

import secrets
import time
from uuid import UUID


def new_event_id() -> UUID:
    """Mint a fresh UUIDv7.

    Layout (RFC 9562 §5.7):
        48 bits  — Unix milliseconds
         4 bits  — version (always 0b0111 = 7)
        12 bits  — random
         2 bits  — variant (always 0b10 = RFC 4122)
        62 bits  — random
    """
    ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF  # truncate to 48 bits
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)

    value = ts_ms << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b

    return UUID(int=value)
