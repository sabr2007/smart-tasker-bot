# tests/test_agent.py
"""
Tests for the AI Agent architecture.

Tests cover:
- Tool definitions structure
- Tool executor functions
- Agent loop behavior (mocked)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

import sys
import os

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestAgentTools:
    """Test agent tool definitions."""
    
    def test_tools_list_not_empty(self):
        """Tools list should not be empty."""
        from agent_tools import AGENT_TOOLS
        assert len(AGENT_TOOLS) > 0
    
    def test_all_tools_have_required_fields(self):
        """Each tool should have type and function fields."""
        from agent_tools import AGENT_TOOLS
        for tool in AGENT_TOOLS:
            assert "type" in tool
            assert tool["type"] == "function"
            assert "function" in tool
            assert "name" in tool["function"]
            assert "description" in tool["function"]
            assert "parameters" in tool["function"]
    
    def test_get_tool_names(self):
        """get_tool_names should return list of tool names."""
        from agent_tools import get_tool_names
        names = get_tool_names()
        assert isinstance(names, list)
        assert "get_tasks" in names
        assert "add_task" in names
        assert "delete_task" in names
        assert "complete_task" in names
        assert "update_deadline" in names
        assert "rename_task" in names
        assert "show_tasks" in names
    
    def test_get_tool_by_name_existing(self):
        """get_tool_by_name should return tool definition."""
        from agent_tools import get_tool_by_name
        tool = get_tool_by_name("get_tasks")
        assert tool is not None
        assert tool["function"]["name"] == "get_tasks"
    
    def test_get_tool_by_name_nonexistent(self):
        """get_tool_by_name should return None for unknown tool."""
        from agent_tools import get_tool_by_name
        tool = get_tool_by_name("nonexistent_tool")
        assert tool is None


class TestToolExecutors:
    """Test individual tool executor functions."""
    
    @pytest.fixture
    def mock_db(self):
        """Mock the db module."""
        with patch('llm_client.db') as mock:
            yield mock
    
    @pytest.mark.asyncio
    async def test_execute_get_tasks_empty(self, mock_db):
        """get_tasks should return message when no tasks."""
        from llm_client import _execute_get_tasks
        
        mock_db.get_tasks = AsyncMock(return_value=[])
        
        result = await _execute_get_tasks(user_id=123, user_timezone="Asia/Almaty")
        
        assert "нет активных задач" in result.lower()
        mock_db.get_tasks.assert_called_once_with(123)
    
    @pytest.mark.asyncio
    async def test_execute_get_tasks_with_tasks(self, mock_db):
        """get_tasks should return formatted task list."""
        from llm_client import _execute_get_tasks
        
        mock_db.get_tasks = AsyncMock(return_value=[
            (1, "Buy milk", "2025-01-15T10:00:00Z"),
            (2, "Call John", None),
        ])
        
        result = await _execute_get_tasks(user_id=123, user_timezone="Asia/Almaty")
        
        assert "ID 1" in result
        assert "Buy milk" in result
        assert "ID 2" in result
        assert "Call John" in result
    
    @pytest.mark.asyncio
    async def test_execute_add_task_success(self, mock_db):
        """add_task should create task and return confirmation."""
        from llm_client import _execute_add_task
        
        mock_db.add_task = AsyncMock(return_value=42)
        
        result = await _execute_add_task(
            user_id=123,
            text="Test task",
            deadline=None,
            user_timezone="Asia/Almaty"
        )
        
        assert "ID 42" in result
        assert "Test task" in result
        mock_db.add_task.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_execute_add_task_empty_text(self, mock_db):
        """add_task should reject empty text."""
        from llm_client import _execute_add_task
        
        result = await _execute_add_task(
            user_id=123,
            text="",
            deadline=None,
            user_timezone="Asia/Almaty"
        )
        
        assert "ошибка" in result.lower()
        mock_db.add_task.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_execute_delete_task_success(self, mock_db):
        """delete_task should delete and return confirmation."""
        from llm_client import _execute_delete_task
        
        mock_db.get_task = AsyncMock(return_value=(5, "Buy milk", None))
        mock_db.delete_task = AsyncMock()
        
        result = await _execute_delete_task(user_id=123, task_id=5)
        
        assert "Buy milk" in result
        assert "удален" in result.lower()
        mock_db.delete_task.assert_called_once_with(123, 5)
    
    @pytest.mark.asyncio
    async def test_execute_delete_task_not_found(self, mock_db):
        """delete_task should return error if task not found."""
        from llm_client import _execute_delete_task
        
        mock_db.get_task = AsyncMock(return_value=None)
        
        result = await _execute_delete_task(user_id=123, task_id=999)
        
        assert "не найден" in result.lower()
        mock_db.delete_task.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_execute_complete_task_success(self, mock_db):
        """complete_task should mark task as done."""
        from llm_client import _execute_complete_task
        
        mock_db.get_task = AsyncMock(return_value=(5, "Buy milk", None))
        mock_db.set_task_done = AsyncMock(return_value=(True, None))  # Returns tuple now
        
        result = await _execute_complete_task(user_id=123, task_id=5)
        
        assert "Buy milk" in result
        assert "выполнен" in result.lower()
        mock_db.set_task_done.assert_called_once_with(123, 5)
    
    @pytest.mark.asyncio
    async def test_execute_rename_task_success(self, mock_db):
        """rename_task should update task text."""
        from llm_client import _execute_rename_task
        
        mock_db.get_task = AsyncMock(return_value=(5, "Old text", None))
        mock_db.update_task_text = AsyncMock()
        
        result = await _execute_rename_task(
            user_id=123,
            task_id=5,
            new_text="New text"
        )
        
        assert "Old text" in result
        assert "New text" in result
        mock_db.update_task_text.assert_called_once_with(123, 5, "New text")
    
    @pytest.mark.asyncio
    async def test_execute_update_deadline_remove(self, mock_db):
        """update_deadline with action=remove should clear deadline."""
        from llm_client import _execute_update_deadline
        
        mock_db.get_task = AsyncMock(return_value=(5, "Task", "2025-01-15T10:00:00Z"))
        mock_db.update_task_due = AsyncMock()
        mock_db.update_task_reminder_settings = AsyncMock()  # Added missing mock
        
        result = await _execute_update_deadline(
            user_id=123,
            task_id=5,
            action="remove",
            deadline=None,
            user_timezone="Asia/Almaty"
        )
        
        assert "убран" in result.lower()
        mock_db.update_task_due.assert_called_once_with(123, 5, None)


class TestExecuteTool:
    """Test the main execute_tool dispatcher."""
    
    @pytest.fixture
    def mock_db(self):
        """Mock the db module."""
        with patch('llm_client.db') as mock:
            yield mock
    
    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self, mock_db):
        """execute_tool should handle unknown tool names."""
        from llm_client import execute_tool
        
        result = await execute_tool(
            tool_name="unknown_tool",
            arguments={},
            user_id=123,
            user_timezone="Asia/Almaty"
        )
        
        assert "ошибка" in result.lower() or "неизвестн" in result.lower()
    
    @pytest.mark.asyncio
    async def test_execute_tool_exception_handling(self, mock_db):
        """execute_tool should catch exceptions and return error message."""
        from llm_client import execute_tool
        
        mock_db.get_tasks = AsyncMock(side_effect=Exception("DB connection failed"))
        
        result = await execute_tool(
            tool_name="get_tasks",
            arguments={},
            user_id=123,
            user_timezone="Asia/Almaty"
        )
        
        assert "ошибка" in result.lower()


class TestAgentSystemPrompt:
    """Test agent system prompt generation."""
    
    def test_system_prompt_contains_tools(self):
        """System prompt should mention available tools."""
        from llm_client import build_agent_system_prompt
        
        prompt = build_agent_system_prompt("2025-01-01 12:00", "Asia/Almaty")
        
        assert "get_tasks" in prompt
        assert "add_task" in prompt
        assert "delete_task" in prompt
    
    def test_system_prompt_contains_timezone(self):
        """System prompt should include user's timezone."""
        from llm_client import build_agent_system_prompt
        
        prompt = build_agent_system_prompt("2025-01-01 12:00", "Europe/Moscow")
        
        assert "Europe/Moscow" in prompt
    
    def test_system_prompt_contains_current_time(self):
        """System prompt should include current time."""
        from llm_client import build_agent_system_prompt
        
        prompt = build_agent_system_prompt("2025-01-01 12:00", "Asia/Almaty")
        
        assert "2025-01-01 12:00" in prompt


class TestAgentHistory:
    """Test conversation history management."""
    
    @pytest.mark.asyncio
    async def test_get_user_history_empty(self):
        """Should return empty list for new user (with mocked DB)."""
        from bot.handlers.agent_text import _get_user_history, _user_histories_cache
        
        # Clear cache
        _user_histories_cache.clear()
        
        # Mock DB to return empty
        with patch('db.get_conversation_history', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = []
            history = await _get_user_history(999999)
            assert history == []
    
    @pytest.mark.asyncio
    async def test_update_user_history(self):
        """Should store and retrieve history (with mocked DB)."""
        from bot.handlers.agent_text import (
            _get_user_history,
            _update_user_history,
            _user_histories_cache,
        )
        
        _user_histories_cache.clear()
        
        test_history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        
        with patch('db.set_conversation_history', new_callable=AsyncMock) as mock_set:
            await _update_user_history(12345, test_history)
            mock_set.assert_called_once()
        
        # Check cache was updated
        assert len(_user_histories_cache.get(12345, [])) == 2
    
    @pytest.mark.asyncio
    async def test_clear_user_history(self):
        """Should clear history for user (with mocked DB)."""
        from bot.handlers.agent_text import (
            _update_user_history,
            clear_user_history,
            _user_histories_cache,
        )
        
        _user_histories_cache.clear()
        
        # Set some history in cache
        _user_histories_cache[12345] = [{"role": "user", "content": "Test"}]
        
        with patch('db.clear_conversation_history', new_callable=AsyncMock) as mock_clear:
            await clear_user_history(12345)
            mock_clear.assert_called_once_with(12345)
        
        assert 12345 not in _user_histories_cache
