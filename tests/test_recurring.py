# tests/test_recurring.py
"""
Tests for the Recurring Tasks feature.

Tests cover:
- Time utilities (calculate_next_occurrence)
- Tool definitions (set_task_recurring, remove_task_recurrence)
- Tool executors (mocked DB)
"""

import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone

import sys
import os

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestCalculateNextOccurrence:
    """Test calculate_next_occurrence function."""

    def test_daily_recurrence(self):
        """Daily should add 1 day."""
        from time_utils import calculate_next_occurrence
        
        current = "2026-01-01T10:00:00Z"
        result = calculate_next_occurrence(current, "daily")
        
        assert result == "2026-01-02T10:00:00Z"

    def test_weekly_recurrence(self):
        """Weekly should add 7 days."""
        from time_utils import calculate_next_occurrence
        
        current = "2026-01-01T10:00:00Z"
        result = calculate_next_occurrence(current, "weekly")
        
        assert result == "2026-01-08T10:00:00Z"

    def test_monthly_recurrence(self):
        """Monthly should add 1 month."""
        from time_utils import calculate_next_occurrence
        
        current = "2026-01-15T10:00:00Z"
        result = calculate_next_occurrence(current, "monthly")
        
        assert result == "2026-02-15T10:00:00Z"

    def test_monthly_end_of_month(self):
        """Monthly from Jan 31 should go to Feb 28 (or 29)."""
        from time_utils import calculate_next_occurrence
        
        # 2026 is not a leap year, so Feb has 28 days
        current = "2026-01-31T10:00:00Z"
        result = calculate_next_occurrence(current, "monthly")
        
        # dateutil should handle this correctly
        assert result == "2026-02-28T10:00:00Z"

    def test_custom_recurrence(self):
        """Custom should add specified interval days."""
        from time_utils import calculate_next_occurrence
        
        current = "2026-01-01T10:00:00Z"
        result = calculate_next_occurrence(current, "custom", interval=3)
        
        assert result == "2026-01-04T10:00:00Z"

    def test_custom_default_interval(self):
        """Custom with no interval should default to 1 day."""
        from time_utils import calculate_next_occurrence
        
        current = "2026-01-01T10:00:00Z"
        result = calculate_next_occurrence(current, "custom", interval=0)
        
        # interval < 1 should use 1
        assert result == "2026-01-02T10:00:00Z"

    def test_preserves_time(self):
        """Recurrence should preserve time of day."""
        from time_utils import calculate_next_occurrence
        
        current = "2026-01-01T15:30:45Z"
        result = calculate_next_occurrence(current, "daily")
        
        assert result == "2026-01-02T15:30:45Z"

    def test_handles_none_input(self):
        """Should return None for None input."""
        from time_utils import calculate_next_occurrence
        
        result = calculate_next_occurrence(None, "daily")
        assert result is None

    def test_handles_empty_string(self):
        """Should return None for empty string."""
        from time_utils import calculate_next_occurrence
        
        result = calculate_next_occurrence("", "daily")
        assert result is None

    def test_unknown_type_defaults_to_daily(self):
        """Unknown recurrence type should default to daily."""
        from time_utils import calculate_next_occurrence
        
        current = "2026-01-01T10:00:00Z"
        result = calculate_next_occurrence(current, "unknown_type")
        
        assert result == "2026-01-02T10:00:00Z"


class TestRecurringToolDefinitions:
    """Test recurring tool definitions in agent_tools.py."""

    def test_set_task_recurring_tool_exists(self):
        """set_task_recurring tool should be defined."""
        from agent_tools import AGENT_TOOLS, get_tool_by_name
        
        tool = get_tool_by_name("set_task_recurring")
        assert tool is not None
        assert tool["function"]["name"] == "set_task_recurring"

    def test_remove_task_recurrence_tool_exists(self):
        """remove_task_recurrence tool should be defined."""
        from agent_tools import get_tool_by_name
        
        tool = get_tool_by_name("remove_task_recurrence")
        assert tool is not None
        assert tool["function"]["name"] == "remove_task_recurrence"

    def test_set_task_recurring_has_required_params(self):
        """set_task_recurring should require task_id and recurrence_type."""
        from agent_tools import get_tool_by_name
        
        tool = get_tool_by_name("set_task_recurring")
        required = tool["function"]["parameters"]["required"]
        
        assert "task_id" in required
        assert "recurrence_type" in required

    def test_set_task_recurring_has_recurrence_types(self):
        """set_task_recurring should have enum for recurrence types."""
        from agent_tools import get_tool_by_name
        
        tool = get_tool_by_name("set_task_recurring")
        props = tool["function"]["parameters"]["properties"]
        
        assert "recurrence_type" in props
        assert "enum" in props["recurrence_type"]
        assert set(props["recurrence_type"]["enum"]) == {"daily", "weekly", "monthly", "custom"}


class TestRecurringToolExecutors:
    """Test recurring tool executor functions."""

    @pytest.fixture
    def mock_db(self):
        """Mock the db module."""
        with patch('llm_client.db') as mock:
            yield mock

    @pytest.mark.asyncio
    async def test_execute_set_task_recurring_daily(self, mock_db):
        """set_task_recurring should set daily recurrence."""
        from llm_client import _execute_set_task_recurring
        
        mock_db.get_task = AsyncMock(return_value=(1, "Test task", "2026-01-01T10:00:00Z"))
        mock_db.set_task_recurrence = AsyncMock(return_value=True)
        
        result = await _execute_set_task_recurring(
            user_id=123,
            task_id=1,
            recurrence_type="daily",
            interval=None,
            end_date=None,
            user_timezone="Asia/Almaty",
        )
        
        assert "повторяется" in result
        assert "каждый день" in result
        mock_db.set_task_recurrence.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_set_task_recurring_custom(self, mock_db):
        """set_task_recurring with custom type should require interval."""
        from llm_client import _execute_set_task_recurring
        
        # Without interval
        result = await _execute_set_task_recurring(
            user_id=123,
            task_id=1,
            recurrence_type="custom",
            interval=None,
            end_date=None,
            user_timezone="Asia/Almaty",
        )
        
        assert "Ошибка" in result
        assert "interval" in result

    @pytest.mark.asyncio
    async def test_execute_set_task_recurring_task_not_found(self, mock_db):
        """set_task_recurring should error if task not found."""
        from llm_client import _execute_set_task_recurring
        
        mock_db.get_task = AsyncMock(return_value=None)
        
        result = await _execute_set_task_recurring(
            user_id=123,
            task_id=999,
            recurrence_type="daily",
            interval=None,
            end_date=None,
            user_timezone="Asia/Almaty",
        )
        
        assert "не найдена" in result

    @pytest.mark.asyncio
    async def test_execute_remove_task_recurrence_success(self, mock_db):
        """remove_task_recurrence should remove recurrence."""
        from llm_client import _execute_remove_task_recurrence
        
        mock_db.get_task = AsyncMock(return_value=(1, "Test task", "2026-01-01T10:00:00Z"))
        mock_db.remove_task_recurrence = AsyncMock(return_value=True)
        
        result = await _execute_remove_task_recurrence(user_id=123, task_id=1)
        
        assert "отключено" in result
        mock_db.remove_task_recurrence.assert_called_once_with(123, 1)

    @pytest.mark.asyncio
    async def test_execute_complete_task_recurring(self, mock_db):
        """complete_task should schedule new occurrence for recurring task."""
        from llm_client import _execute_complete_task, set_schedule_reminder_callback
        
        mock_db.get_task = AsyncMock(side_effect=[
            (1, "Test task", "2026-01-01T10:00:00Z"),  # First call - original task
            (2, "Test task", "2026-01-02T10:00:00Z"),  # Second call - new occurrence
        ])
        mock_db.set_task_done = AsyncMock(return_value=(True, 2))  # Returns new_task_id=2
        
        # Mock schedule callback
        schedule_callback = AsyncMock()
        set_schedule_reminder_callback(schedule_callback)
        
        result = await _execute_complete_task(user_id=123, task_id=1)
        
        assert "выполненная" in result
        # Should have scheduled reminder for new task
        schedule_callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_complete_task_non_recurring(self, mock_db):
        """complete_task should not schedule new occurrence for non-recurring task."""
        from llm_client import _execute_complete_task, set_schedule_reminder_callback
        
        mock_db.get_task = AsyncMock(return_value=(1, "Test task", "2026-01-01T10:00:00Z"))
        mock_db.set_task_done = AsyncMock(return_value=(True, None))  # No new task
        
        schedule_callback = AsyncMock()
        set_schedule_reminder_callback(schedule_callback)
        
        result = await _execute_complete_task(user_id=123, task_id=1)
        
        assert "выполненная" in result
        # Should NOT have scheduled new reminder
        schedule_callback.assert_not_called()
