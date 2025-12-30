# tests/test_time_utils.py
"""Tests for time_utils normalization functions."""

import pytest
import sys
import os
from datetime import datetime, timedelta
from unittest.mock import patch

# Add src to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from time_utils import normalize_deadline_to_utc, compute_remind_at_from_offset, now_utc, UTC


class TestNormalizeDeadlineToUtc:
    """Test normalize_deadline_to_utc function."""
    
    def test_user_timezone_conversion_asia_almaty(self):
        """
        User Timezone Logic Test:
        - User timezone: Asia/Almaty (UTC+5 since March 2024 reform)
        - User inputs: "2025-06-15T15:00:00" (naive, no Z)
        - Expected: "2025-06-15T10:00:00Z" (15:00 Almaty = 10:00 UTC)
        
        If result is 15:00Z - conversion didn't happen (BUG!)
        If result is 20:00Z - double conversion happened (BUG!)
        """
        # Use 2025 date (after Kazakhstan's March 2024 timezone reform to UTC+5)
        user_input = "2025-06-15T15:00:00"  # No timezone suffix
        user_timezone = "Asia/Almaty"
        
        result = normalize_deadline_to_utc(user_input, user_timezone)
        
        assert result is not None
        # Almaty is UTC+5 since March 2024, so 15:00 Almaty = 10:00 UTC
        assert result == "2025-06-15T10:00:00Z", f"Expected 10:00Z, got {result}"
    
    def test_utc_input_stays_utc(self):
        """Input already in UTC (Z suffix) should stay the same."""
        utc_input = "2025-06-15T10:00:00Z"
        result = normalize_deadline_to_utc(utc_input, "Asia/Almaty")
        
        assert result == "2025-06-15T10:00:00Z"
    
    def test_date_only_defaults_to_2359(self):
        """Date without time should default to 23:59 local, then convert to UTC."""
        # Use 2025 date (after Kazakhstan's March 2024 timezone reform)
        date_input = "2025-06-15"
        user_timezone = "Asia/Almaty"  # UTC+5 since March 2024
        
        result = normalize_deadline_to_utc(date_input, user_timezone)
        
        # 23:59 Almaty (UTC+5) = 18:59 UTC
        assert result is not None
        assert result == "2025-06-15T18:59:00Z", f"Expected 18:59Z, got {result}"
    
    def test_none_input_returns_none(self):
        """None input should return None."""
        assert normalize_deadline_to_utc(None, "Asia/Almaty") is None
    
    def test_empty_string_returns_none(self):
        """Empty string should return None."""
        assert normalize_deadline_to_utc("", "Asia/Almaty") is None
        assert normalize_deadline_to_utc("   ", "Asia/Almaty") is None


class TestComputeRemindAtFromOffset:
    """Test compute_remind_at_from_offset function."""
    
    def test_offset_subtraction_future_date(self):
        """Reminder should be due_at minus offset for future dates."""
        # Use a date far in the future
        future_due = "2099-12-31T10:00:00Z"
        offset_min = 30
        
        result = compute_remind_at_from_offset(future_due, offset_min)
        
        assert result == "2099-12-31T09:30:00Z"
    
    def test_zero_offset_future_date(self):
        """Zero offset should return same time for future dates."""
        future_due = "2099-12-31T10:00:00Z"
        
        result = compute_remind_at_from_offset(future_due, 0)
        
        assert result == "2099-12-31T10:00:00Z"
    
    def test_past_date_returns_near_future(self):
        """Past due dates should return a time in the near future (now + ~10 sec)."""
        past_due = "2020-01-01T10:00:00Z"
        
        result = compute_remind_at_from_offset(past_due, 30)
        
        # Should be within a few seconds of now
        assert result is not None
        result_dt = datetime.fromisoformat(result.replace("Z", "+00:00"))
        now = now_utc()
        diff = abs((result_dt - now).total_seconds())
        assert diff < 60, f"Expected result to be near now, but diff was {diff} seconds"
    
    def test_none_due_returns_none(self):
        """None due_iso should return None."""
        assert compute_remind_at_from_offset(None, 30) is None
        assert compute_remind_at_from_offset("", 30) is None
