"""Contract tests for the deterministic test-impact selector.

The selector is development tooling: these tests pin the decisions that keep a
partial run honest.  A wrong answer here is not a cosmetic bug -- it silently
skips regression coverage -- so each rule (self-selection, ownership mapping,
reverse imports, shared-contract escalation, docs/frontend routing, fail-closed
fallback and stale mappings) is exercised directly.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import textwrap

from tools import select_tests

REAL_ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True,
                          text=True, encoding='utf-8', errors='replace')


def make_repo(root: Path) -> Path:
    """A throwaway checkout with the files the synthetic config refers to."""
    root.mkdir(parents=True, exist_ok=True)
    files = {
        'backend/audio_edit.py': 'VALUE = 1\n',
        'backend/speech.py': 'VALUE = 1\n',
        'backend/contract.py': 'VALUE = 1\n',
        'backend/imported.py': 'VALUE = 1\n',
        'backend/importer.py': 'from backend.imported import VALUE\n',
        'backend/new_thing.py': 'VALUE = 1\n',
        'tests/test_audio_edit.py': 'from backend import audio_edit\n',
        'tests/test_speech.py': 'from backend import speech\n',
        'tests/test_imported_user.py': 'from backend import imported\n',
        'tests/test_smoke.py': '',
        'tests/test_docs_gate.py': '',
        'tests/alpha.browser.mjs': '',
        'tests/beta.browser.mjs': '',
        'src/Widget.tsx': '',
        'src/Unknown.tsx': '',
        'tools/export_public_source.py': '',
        'tools/check_public_docs.py': '',
        'docs/guide.md': '# guide\n',
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    (root / 'tools/test_impact.toml').write_text(textwrap.dedent('''
        schema_version = 1
        default_base = "HEAD"

        [shared]
        paths = ["backend/contract.py"]

        [docs]
        paths = ["docs/**", "*.md"]
        tests = ["tests/test_docs_gate.py"]
        gates = [
          { name = "public-source-export", argv = ["python", "tools/export_public_source.py", "--check"] },
          { name = "public-docs", argv = ["python", "tools/check_public_docs.py"] },
        ]

        [frontend]
        paths = ["src/**"]
        shared_paths = []

        [fast]
        budget_seconds = 60
        tests = ["tests/test_smoke.py"]

        [[mapping]]
        name = "audio"
        paths = ["backend/audio_edit.py"]
        tests = ["tests/test_audio_edit.py"]

        [[mapping]]
        name = "widget"
        paths = ["src/Widget.tsx"]
        browser = ["tests/alpha.browser.mjs"]
    ''').lstrip(), encoding='utf-8')
    return root / 'tools/test_impact.toml'


def synthetic_selection(tmp_path: Path, changes: list[select_tests.ChangedFile],
                        config_text: str | None = None):
    config_path = make_repo(tmp_path)
    if config_text is not None:
        config_path.write_text(textwrap.dedent(config_text).lstrip(), encoding='utf-8')
    config = select_tests.load_config(config_path, tmp_path)
    assert config.errors == (), config.errors
    return select_tests.select(config, changes), config


def test_real_config_is_valid_and_documents_shared_entries():
    config = select_tests.load_config(select_tests.DEFAULT_CONFIG, REAL_ROOT)
    assert config.errors == ()
    assert config.default_base == 'origin/main'
    assert config.fast_tests and config.browsers
    names = {mapping.name for mapping in config.mappings}
    assert {'audio-edit', 'enhancement', 'mcp-server-tools', 'task-controls'} <= names
    shared = {pattern.raw for pattern in config.shared}
    assert {'backend/app.py', 'backend/schemas.py', 'requirements.txt'} <= shared
    assert 'tests/conftest.py' in shared


def test_changed_test_file_selects_only_itself():
    config = select_tests.load_config(select_tests.DEFAULT_CONFIG, REAL_ROOT)
    selection = select_tests.select(
        config, [select_tests.ChangedFile('tests/test_speech_tasks.py', 'M')])
    assert selection.pytest == ['tests/test_speech_tasks.py']
    assert selection.browser == []
    assert selection.full_core is False
    assert selection.errors == []


def test_backend_feature_selects_its_own_tests_not_another_feature():
    config = select_tests.load_config(select_tests.DEFAULT_CONFIG, REAL_ROOT)
    selection = select_tests.select(
        config, [select_tests.ChangedFile('backend/audio_edit.py', 'M')])
    assert 'tests/test_audio_edit.py' in selection.pytest
    assert 'tests/test_audio_edit_api.py' in selection.pytest
    assert 'tests/test_speech_tasks.py' not in selection.pytest
    assert 'tests/test_speech_process.py' not in selection.pytest
    assert selection.full_core is False


def test_shared_contract_changes_escalate_to_full_core():
    config = select_tests.load_config(select_tests.DEFAULT_CONFIG, REAL_ROOT)
    for path in ('backend/schemas.py', 'backend/app.py', 'requirements-dev.txt',
                 'tests/conftest.py', '.github/workflows/public-source.yml'):
        selection = select_tests.select(config, [select_tests.ChangedFile(path, 'M')])
        assert selection.full_core is True, path
        assert selection.status == 'full-core', path


def test_unmapped_source_escalates_to_full_core_and_asks_for_mapping(tmp_path):
    selection, _ = synthetic_selection(
        tmp_path, [select_tests.ChangedFile('backend/new_thing.py', 'A')])
    assert selection.full_core is True
    assert any('Add a [[mapping]] entry' in warning for warning in selection.warnings)
    assert selection.decisions[0].escalation == 'full-core'


def test_unmapped_frontend_selects_every_driver_instead_of_skipping(tmp_path):
    selection, config = synthetic_selection(
        tmp_path, [select_tests.ChangedFile('src/Unknown.tsx', 'M')])
    assert selection.browser == list(config.browsers)
    assert selection.full_core is False
    assert any('no browser-driver mapping' in warning for warning in selection.warnings)


def test_frontend_mapping_selects_only_the_related_driver(tmp_path):
    selection, _ = synthetic_selection(
        tmp_path, [select_tests.ChangedFile('src/Widget.tsx', 'M')])
    assert selection.browser == ['tests/alpha.browser.mjs']
    assert selection.pytest == []
    assert selection.full_core is False


def test_docs_change_selects_docs_gates_only(tmp_path):
    selection, _ = synthetic_selection(
        tmp_path, [select_tests.ChangedFile('docs/guide.md', 'M')])
    assert selection.pytest == ['tests/test_docs_gate.py']
    assert [gate['name'] for gate in selection.gates] == ['public-docs', 'public-source-export']
    assert selection.browser == []
    assert selection.full_core is False
    assert selection.status == 'docs-only'


def test_deleted_test_file_is_reported_instead_of_silently_dropped():
    config = select_tests.load_config(select_tests.DEFAULT_CONFIG, REAL_ROOT)
    selection = select_tests.select(
        config, [select_tests.ChangedFile('tests/test_audio_edit.py', 'D')])
    assert 'tests/test_audio_edit.py' not in selection.pytest
    assert any('no longer exists' in warning for warning in selection.warnings)


def test_missing_mapping_target_is_reported_as_stale(tmp_path):
    config_path = make_repo(tmp_path)
    config_path.write_text(config_path.read_text(encoding='utf-8') + textwrap.dedent('''
        [[mapping]]
        name = "renamed-test"
        paths = ["backend/audio_edit.py"]
        tests = ["tests/test_renamed_away.py"]
    '''), encoding='utf-8')
    config = select_tests.load_config(config_path, tmp_path)
    assert any('test_renamed_away.py' in error and 'update the mapping' in error
               for error in config.errors)


def test_import_reverse_dependency_selects_importing_test(tmp_path):
    selection, _ = synthetic_selection(
        tmp_path, [select_tests.ChangedFile('backend/imported.py', 'M')])
    assert 'tests/test_imported_user.py' in selection.pytest
    assert selection.full_core is False
    assert any('import-reverse-dependency' in rule
               for decision in selection.decisions for rule in decision.rules)


def test_changed_browser_driver_selects_itself():
    config = select_tests.load_config(select_tests.DEFAULT_CONFIG, REAL_ROOT)
    selection = select_tests.select(
        config, [select_tests.ChangedFile('tests/enhancement.browser.mjs', 'M')])
    assert selection.browser == ['tests/enhancement.browser.mjs']
    assert selection.pytest == []
    assert selection.full_core is False


def test_pytest_node_targets_are_preserved():
    config = select_tests.loads(textwrap.dedent('''
        schema_version = 1
        [shared]
        paths = ["backend/contract.py"]
        [docs]
        paths = ["docs/**"]
        tests = ["tests/test_docs_gate.py"]
        gates = []
        [frontend]
        paths = ["src/**"]
        [[mapping]]
        name = "node"
        paths = ["backend/audio_edit.py"]
        tests = ["tests/test_audio_edit.py::test_specific_case"]
    ''').lstrip(), Path('synthetic.toml'), REAL_ROOT)
    assert config.errors == ()
    selection = select_tests.select(config, [select_tests.ChangedFile('backend/audio_edit.py')])
    assert selection.pytest == ['tests/test_audio_edit.py::test_specific_case']


def test_collect_changes_reports_committed_modified_untracked_and_renamed(tmp_path):
    root = tmp_path
    make_repo(root)
    git(root, 'init', '-q')
    git(root, 'config', 'user.email', 'selector@example.invalid')
    git(root, 'config', 'user.name', 'Selector Test')
    git(root, 'add', '.')
    git(root, 'commit', '-q', '-m', 'fixture')
    (root / 'backend/audio_edit.py').write_text('VALUE = 2\n', encoding='utf-8')
    (root / 'backend/untracked.py').write_text('VALUE = 3\n', encoding='utf-8')
    git(root, 'mv', 'backend/speech.py', 'backend/speech_renamed.py')
    changes = {change.path: change for change in select_tests.collect_changes(root, 'HEAD')}
    assert changes['backend/audio_edit.py'].status == 'M'
    assert changes['backend/untracked.py'].status == '?'
    assert changes['backend/speech_renamed.py'].status == 'R'
    assert changes['backend/speech_renamed.py'].old_path == 'backend/speech.py'
    assert changes['backend/speech.py'].status == 'D'


def test_cli_json_reports_the_same_selection_and_rejects_stale_config(tmp_path, capsys):
    root = tmp_path
    config_path = make_repo(root)
    git(root, 'init', '-q')
    git(root, 'config', 'user.email', 'selector@example.invalid')
    git(root, 'config', 'user.name', 'Selector Test')
    git(root, 'add', '.')
    git(root, 'commit', '-q', '-m', 'fixture')
    (root / 'backend/audio_edit.py').write_text('VALUE = 4\n', encoding='utf-8')
    assert select_tests.main(['--root', str(root), '--base', 'HEAD', '--json',
                              '--config', str(config_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['full_core'] is False
    assert payload['pytest'] == ['tests/test_audio_edit.py']
    assert payload['changed'] == [{'path': 'backend/audio_edit.py', 'status': 'M',
                                   'old_path': None}]
    (root / 'tests/test_audio_edit.py').unlink()
    assert select_tests.main(['--root', str(root), '--base', 'HEAD', '--json',
                              '--config', str(config_path)]) == 2
    assert 'update the mapping' in capsys.readouterr().out
