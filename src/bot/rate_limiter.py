# src/bot/rate_limiter.py
"""In-memory rate limiter to protect against API abuse.

Prevents excessive OpenAI API calls by limiting requests per user.
Uses a sliding window algorithm with automatic cleanup.
"""

import logging
import time
from collections import defaultdict
from threading import Lock
from typing import Tuple

logger = logging.getLogger(__name__)

# ===== Configuration =====
# Maximum requests per window per user
MAX_REQUESTS_PER_WINDOW = 10
# Window size in seconds
WINDOW_SIZE_SECONDS = 60
# Cleanup interval: remove old entries every N seconds
CLEANUP_INTERVAL_SECONDS = 300

# ===== Internal State =====
# user_id -> list of timestamps
_user_requests: dict[int, list[float]] = defaultdict(list)
_lock = Lock()
_last_cleanup = time.time()


def _cleanup_old_entries() -> None:
    """Remove timestamps older than the window from all users."""
    global _last_cleanup
    
    now = time.time()
    if now - _last_cleanup < CLEANUP_INTERVAL_SECONDS:
        return
    
    _last_cleanup = now
    cutoff = now - WINDOW_SIZE_SECONDS
    
    users_to_remove = []
    for user_id, timestamps in _user_requests.items():
        # Filter out old timestamps
        _user_requests[user_id] = [ts for ts in timestamps if ts > cutoff]
        if not _user_requests[user_id]:
            users_to_remove.append(user_id)
    
    # Remove empty user entries
    for user_id in users_to_remove:
        del _user_requests[user_id]
    
    if users_to_remove:
        logger.debug("Rate limiter cleanup: removed %d inactive users", len(users_to_remove))


def check_rate_limit(user_id: int) -> Tuple[bool, int]:
    """
    Check if a user is within rate limits.
    
    Args:
        user_id: Telegram user ID
        
    Returns:
        Tuple of (is_allowed, seconds_until_reset)
        - is_allowed: True if request is allowed, False if rate limited
        - seconds_until_reset: Seconds until oldest request expires (0 if allowed)
    """
    now = time.time()
    cutoff = now - WINDOW_SIZE_SECONDS
    
    with _lock:
        _cleanup_old_entries()
        
        # Filter out old timestamps for this user
        timestamps = _user_requests[user_id]
        valid_timestamps = [ts for ts in timestamps if ts > cutoff]
        _user_requests[user_id] = valid_timestamps
        
        # Check if within limit
        if len(valid_timestamps) < MAX_REQUESTS_PER_WINDOW:
            # Allow request and record timestamp
            _user_requests[user_id].append(now)
            return True, 0
        else:
            # Rate limited - calculate time until oldest expires
            oldest = min(valid_timestamps)
            wait_time = int(oldest + WINDOW_SIZE_SECONDS - now) + 1
            logger.warning(
                "Rate limit exceeded for user %s: %d requests in window, must wait %ds",
                user_id, len(valid_timestamps), wait_time
            )
            return False, max(1, wait_time)


def get_user_request_count(user_id: int) -> int:
    """Get current number of requests for a user in the window."""
    now = time.time()
    cutoff = now - WINDOW_SIZE_SECONDS
    
    with _lock:
        timestamps = _user_requests.get(user_id, [])
        return len([ts for ts in timestamps if ts > cutoff])


def reset_user_limit(user_id: int) -> None:
    """Reset rate limit for a specific user (for admin use)."""
    with _lock:
        if user_id in _user_requests:
            del _user_requests[user_id]
            logger.info("Rate limit reset for user %s", user_id)
