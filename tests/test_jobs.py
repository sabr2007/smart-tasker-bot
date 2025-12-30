
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import sys
import os

# Add src to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from bot.jobs import send_task_reminder

@pytest.mark.asyncio
async def test_send_task_reminder_success():
    """Test that reminder is sent if task is active."""
    context = MagicMock()
    context.job.data = {"task_id": 123, "text": "Test Task"}
    context.job.chat_id = 456
    context.bot.send_message = AsyncMock()

    # Mock DB connection and fetch
    mock_conn = AsyncMock()
    # Mocking async context manager for get_connection
    mock_conn_ctx = AsyncMock()
    mock_conn_ctx.__aenter__.return_value = mock_conn
    mock_conn_ctx.__aexit__.return_value = None

    with patch('db.get_connection', return_value=mock_conn_ctx):
        with patch('db._fetch_task_row', new_callable=AsyncMock) as mock_fetch:
            # Task is active
            mock_fetch.return_value = {"status": "active", "text": "Test Task"}
            
            await send_task_reminder(context)

            # Check logic
            mock_fetch.assert_awaited_once_with(mock_conn, 456, 123)
            context.bot.send_message.assert_awaited_once()
            args, kwargs = context.bot.send_message.call_args
            assert kwargs['chat_id'] == 456
            assert "Test Task" in kwargs['text']

@pytest.mark.asyncio
async def test_send_task_reminder_skipped_not_active():
    """Test that reminder is skipped if task is not active."""
    context = MagicMock()
    context.job.data = {"task_id": 123, "text": "Test Task"}
    context.job.chat_id = 456
    context.bot.send_message = AsyncMock()

    mock_conn = AsyncMock()
    mock_conn_ctx = AsyncMock()
    mock_conn_ctx.__aenter__.return_value = mock_conn
    mock_conn_ctx.__aexit__.return_value = None

    with patch('db.get_connection', return_value=mock_conn_ctx):
        with patch('db._fetch_task_row', new_callable=AsyncMock) as mock_fetch:
            # Task is completed
            mock_fetch.return_value = {"status": "done", "text": "Test Task"}
            
            await send_task_reminder(context)

            # Check logic
            mock_fetch.assert_awaited_once_with(mock_conn, 456, 123)
            # Should NOT send message
            context.bot.send_message.assert_not_called()

@pytest.mark.asyncio
async def test_send_task_reminder_skipped_not_found():
    """Test that reminder is skipped if task is not found (None)."""
    context = MagicMock()
    context.job.data = {"task_id": 999, "text": "Ghost Task"}
    context.job.chat_id = 456
    context.bot.send_message = AsyncMock()

    mock_conn_ctx = AsyncMock()
    mock_conn_ctx.__aenter__.return_value = AsyncMock()
    mock_conn_ctx.__aexit__.return_value = None

    with patch('db.get_connection', return_value=mock_conn_ctx):
        with patch('db._fetch_task_row', new_callable=AsyncMock) as mock_fetch:
            # Task not found
            mock_fetch.return_value = None
            
            await send_task_reminder(context)

            context.bot.send_message.assert_not_called()
