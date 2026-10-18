"""Shared pytest fixtures.

The suite historically reused fixtures by importing them from a sibling test module
(``from test_private_workbench import private_workbench``), which makes test files
depend on each other's module layout.  The two base fixtures are exposed here so a test
can use them without importing another test module; the remaining module-to-module
imports (helpers such as ``create``/``add_shot``) are scheduled to move in follow-ups.
"""
from test_private_workbench import private_workbench  # noqa: F401
from test_production_presets import preset_client  # noqa: F401
