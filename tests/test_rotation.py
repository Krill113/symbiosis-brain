"""Unit tests for symbiosis_brain.rotation."""
from datetime import date as Date

import pytest

from symbiosis_brain.rotation import parse_handoff_sections


def test_module_imports():
    assert parse_handoff_sections is not None
