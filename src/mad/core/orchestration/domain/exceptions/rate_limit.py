"""Rate-limit exception raised by AgentLauncher implementations.

When the external agent process exits because the API is rate-limited
or overloaded, launchers raise ``RateLimitError`` instead of emitting
``session.error``.  The dispatcher catches this specifically and drives
the exponential-backoff retry loop (issue #62) rather than marking the
task terminal immediately.

``captured_id`` carries the conversation ID extracted from the process
stdout before it exited (from ``system/api_retry`` events in
stream-json mode), allowing the next attempt to resume the conversation.
``None`` means no ID was captured; the retry starts a fresh conversation
and emits ``agent.conversation_resume_skipped``.

``retry_after_floor_s`` carries the minimum number of seconds the
dispatcher should wait before the next attempt, derived from the
``resetsAt`` field of the CLI's ``rate_limit_event`` (a usage/session
limit that resets at a fixed wall-clock time).  The dispatcher uses it
as a *floor* under the exponential-backoff schedule so a five-hour
session limit is not retried into the ground every 30 s — it sleeps
until the limit actually resets, then resumes.  ``None`` means no reset
time was advertised and the plain backoff schedule applies.
"""

from __future__ import annotations


class RateLimitError(Exception):
    """Raised by a launcher when the agent exits due to API rate-limiting.

    Attributes
    ----------
    captured_id:
        Conversation ID captured before the process exited, or ``None``.
    reason:
        The ``error`` field from the CLI's ``system/api_retry`` event
        (e.g. ``"rate_limit"``, ``"overloaded"``), or a synthetic string
        derived from stderr pattern matching for providers that lack
        structured retry events.
    retry_after_floor_s:
        Minimum seconds to wait before retrying, derived from the
        ``resetsAt`` of the CLI's ``rate_limit_event``, or ``None`` when
        no reset time was advertised.
    """

    def __init__(
        self,
        captured_id: str | None,
        reason: str,
        retry_after_floor_s: float | None = None,
    ) -> None:
        super().__init__(f"rate limit: {reason} (conversation_id={captured_id!r})")
        self.captured_id = captured_id
        self.reason = reason
        self.retry_after_floor_s = retry_after_floor_s
