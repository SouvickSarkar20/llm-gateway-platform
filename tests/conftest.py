"""Pytest configuration and global fixtures ensuring test determinism and isolation."""

import os
import pytest

# Force test environment variables before any app modules are initialized
os.environ["LLM_PROVIDER"] = "mock"
os.environ["PRIMARY_MODEL"] = "gpt-4o-mini"
os.environ["FALLBACK_MODEL"] = "gpt-3.5-turbo"
os.environ["APP_ENV"] = "testing"

from app.config import get_settings
from app.services.llm_gateway import gateway


@pytest.fixture(autouse=True)
def configure_test_environment(monkeypatch):
    """Ensure all tests run with mock LLM provider and default test settings."""
    settings = get_settings()
    monkeypatch.setattr(settings, "LLM_PROVIDER", "mock")
    monkeypatch.setattr(settings, "PRIMARY_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(settings, "FALLBACK_MODEL", "gpt-3.5-turbo")
    monkeypatch.setattr(gateway, "primary_provider_name", "mock")
    yield
