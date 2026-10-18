"""Shared pytest fixtures.

The suite historically reused fixtures by importing them from a sibling test module
(``from test_private_workbench import private_workbench``), which makes test files depend
on each other's module layout.  The base fixtures are exposed here so a test can use them
without importing another test module; plain helpers that take a client/project live in
``support.py`` for the same reason.  A few aliased wrapper fixtures and two reference
helpers still import their home module and are the only remaining exceptions.
"""
from test_private_workbench import private_workbench  # noqa: F401
from test_production_presets import preset_client  # noqa: F401
from test_assembly_edit import edit_client  # noqa: F401
from test_reverse_workbench import reverse_client  # noqa: F401
from test_scheduler_app import queued_private_app  # noqa: F401
from test_task_reconciliation import reconcile_client  # noqa: F401
