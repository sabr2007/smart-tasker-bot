
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime
import json
import sys
import os

# Add src to sys.path to allow imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from llm_client import (
    _normalize_deadline_to_utc,
    _apply_phrase_validator,
    parse_user_input,
    parse_user_input_multi,
    TaskInterpretation,
    MAX_ITEMS_PER_REQUEST,
    MAX_DELETE_WITHOUT_CONFIRM
)

# Test _normalize_deadline_to_utc
def test_normalize_deadline_to_utc_valid():
    # Input: Local time ISO, User TZ: UTC+5 (Asia/Almaty-ish)
    # 2025-01-01 15:00 in UTC+5 is 10:00 UTC
    raw_iso = "2025-01-01T15:00:00"
    user_tz = "Asia/Almaty"
    
    result = _normalize_deadline_to_utc(raw_iso, user_tz)
    # Code returns ISO with Z for UTC
    assert result == "2025-01-01T10:00:00Z"

def test_normalize_deadline_to_utc_none():
    assert _normalize_deadline_to_utc(None, "UTC") is None

def test_normalize_deadline_to_utc_invalid_type():
    assert _normalize_deadline_to_utc(12345, "UTC") is None

# Test _apply_phrase_validator
def test_apply_phrase_validator_delete_allowed():
    raw_text = "удалить задачу молоко"
    data = {"action": "delete"}
    now = datetime(2025, 1, 1, 12, 0, 0)
    
    new_data, fired = _apply_phrase_validator(raw_text, data, now)
    assert new_data["action"] == "delete"
    assert not fired

def test_apply_phrase_validator_delete_blocked():
    raw_text = "просто текст без удаления"
    data = {"action": "delete"} # LLM hallucinates delete
    now = datetime(2025, 1, 1, 12, 0, 0)
    
    new_data, fired = _apply_phrase_validator(raw_text, data, now)
    assert new_data["action"] == "unknown"
    assert "delete_blocked_by_phrase" in fired


# Test parse_user_input (Single)
@patch('llm_client.client')
def test_parse_user_input_create(mock_client):
    # Setup mock response
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({
        "action": "create",
        "title": "Buy milk",
        "deadline_iso": "2025-01-01T18:00:00", # Local time (assume user in UTC+5)
        "note": " Urgent "
    })
    mock_client.chat.completions.create.return_value = mock_response

    user_text = "Купить молоко в 6 вечера"
    user_tz = "Asia/Almaty"
    
    result = parse_user_input(user_text, user_timezone=user_tz)

    assert isinstance(result, TaskInterpretation)
    assert result.action == "create"
    assert result.title == "Buy milk"
    # 18:00 in +05:00 is 13:00 UTC
    assert result.deadline_iso == "2025-01-01T13:00:00Z"

@patch('llm_client.client')
def test_parse_user_input_reschedule_needs_clarification(mock_client):
    # Action reschedule BUT missing deadline -> needs_clarification
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({
        "action": "reschedule",
        "title": "Buy milk",
        "deadline_iso": None 
    })
    mock_client.chat.completions.create.return_value = mock_response

    user_text = "перенеси задачу молоко"
    result = parse_user_input(user_text)

    assert result.action == "needs_clarification"
    assert "missing_deadline_for_action" in result.note or "needs_deadline" in result.note

# Test parse_user_input_multi
@patch('llm_client.client')
def test_parse_user_input_multi_mixed(mock_client):
    # Mock return list of items
    items = [
        {"action": "create", "title": "Task 1", "raw_input": "новая задача 1"},
        {"action": "complete", "title": "Task 2", "raw_input": "сделал задачу 2"}
    ]
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({"items": items})
    mock_client.chat.completions.create.return_value = mock_response

    user_text = "новая задача 1 и сделал задачу 2"
    results = parse_user_input_multi(user_text)

    assert len(results) == 2
    assert results[0].action == "create"
    assert results[0].title == "Task 1"
    assert results[1].action == "complete"
    assert results[1].title == "Task 2"

@patch('llm_client.client')
def test_parse_user_input_multi_mass_delete_limit(mock_client):
    # More deletes than MAX_DELETE_WITHOUT_CONFIRM (2)
    items = [
        {"action": "delete", "deadline_iso": None, "raw_input": "удали 1"},
        {"action": "delete", "deadline_iso": None, "raw_input": "удали 2"},
        {"action": "delete", "deadline_iso": None, "raw_input": "удали 3"},
    ]
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({"items": items})
    mock_client.chat.completions.create.return_value = mock_response

    # Force "delete" pattern validator to pass for these
    with patch('llm_client._apply_phrase_validator', side_effect=lambda t, d, n: (d, [])):
        results = parse_user_input_multi("удали все", max_items=10)

    # All should be blocked because total deletes > 2
    assert results[0].action == "unknown"
    assert results[1].action == "unknown"
    assert results[2].action == "unknown"
    assert "delete_requires_confirmation" in (results[0].note or "")
