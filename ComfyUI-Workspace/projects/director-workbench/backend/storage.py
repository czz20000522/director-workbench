"""Load the shared stdlib-only storage policy without a package dependency."""
import importlib.util
from pathlib import Path

_source = Path(__file__).resolve().parents[3] / 'production/storage_layout.py'
_spec = importlib.util.spec_from_file_location('director_storage_layout', _source)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
storage_roots = _module.storage_roots
relocate_asset = _module.relocate_asset
legacy_mappings = _module.legacy_mappings
