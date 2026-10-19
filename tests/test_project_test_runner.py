import json
from pathlib import Path
import textwrap
from types import SimpleNamespace

from tools import run_project_tests as runner


def host_roots(tmp_path):
    """Keep the run inside tmp_path: the ComfyUI root and runtime root are machine
    roots, so they are injected here rather than picked up from the real install."""
    return {'desktop': tmp_path, 'runtime': tmp_path / 'runtime'}


def setup_project(tmp_path):
    project = tmp_path / 'ComfyUI-Workspace/projects/director-workbench'
    project.mkdir(parents=True)
    specs = runner.suite_specs(project, tmp_path, tmp_path)
    # Browser drivers and the media producers those drivers call live in the checkout.
    scripts = [script for spec in specs.values() for script in spec.get('scripts', ())]
    for script in (*scripts, *runner.BROWSER_MEDIA_TESTS.values()):
        (project / script).parent.mkdir(parents=True, exist_ok=True)
        (project / script).touch()
    # The browser suite's environment probe checks that the driver dependencies are
    # installed before a run can claim to have passed; the checkout's manifests stand in.
    for module in runner.BROWSER_NODE_MODULES:
        manifest = project / 'node_modules' / module / 'package.json'
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.touch()
    for spec in specs.values():
        # The browser suite drives node and carries no interpreter path.
        interpreter = spec.get('python')
        if interpreter is None:
            continue
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
    return project


def fake_probe(command, missing=None, base_prefix=None):
    return SimpleNamespace(returncode=0, stdout=json.dumps({
        'executable': command[0], 'version': 'test', 'base_prefix': str(base_prefix or Path(command[0]).parents[3]),
        'enable_user_site': False, 'stdlib_path': 'base/Lib/os.py',
        'modules': {name: {'available': name != missing, 'version': 'test'} for name in command[3:]},
    }), stderr='')


def fake_output(returncode=0, stdout='', stderr=''):
    """A child result shaped like CompletedProcess: the harness reads the streams it asked
    for, so a fake that omits them would pass here and fail against a real subprocess."""
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_failure_keeps_running_other_suite_with_explicit_paths(tmp_path, monkeypatch):
    project = setup_project(tmp_path)
    calls = []

    def execute(command, **kwargs):
        calls.append((command, kwargs))
        if '-c' in command:
            return fake_probe(command)
        if command[1] in runner.BROWSER_MEDIA_TESTS.values():
            Path(command[2]).write_bytes(b'')  # the producer writes the media fixture
            return fake_output()
        return fake_output(2 if 'pytest' in command else 0)

    monkeypatch.setattr(runner.subprocess, 'run', execute)
    monkeypatch.setenv('PYTHONHOME', 'external-home')
    monkeypatch.setenv('PYTHONPATH', 'external-packages')
    monkeypatch.setenv('PYTHONNOUSERSITE', '0')
    monkeypatch.chdir(tmp_path)
    report = tmp_path / 'result.json'
    assert runner.run(report_path=report, project=project, **host_roots(tmp_path)) == 1
    receipt = json.loads(report.read_text(encoding='utf-8'))
    assert receipt['suites']['core']['status'] == 'failed'
    assert receipt['suites']['auk']['status'] == 'passed'
    specs = runner.suite_specs(project, Path(receipt['run_root']), tmp_path)
    # Two probes and two interpreter suites, plus one command per browser driver. This fake
    # fails every pytest command, so the three drivers in BROWSER_PYTEST_FIXTURES contribute
    # their failed producer instead of a node command and the total is unchanged; each
    # driver in BROWSER_MEDIA_TESTS contributes its producer and then its node command.
    assert len(calls) == 4 + len(specs['browser']['scripts']) + len(runner.BROWSER_MEDIA_TESTS)
    for index, name in ((0, 'core'), (2, 'auk')):
        command, kwargs = calls[index + 1]
        assert command == [str(specs[name]['python']), *specs[name]['args']]
        assert kwargs['cwd'] == project
        temporary = str(Path(receipt['run_root']) / name)
        assert all(kwargs['env'][key] == temporary for key in ('TMP', 'TEMP', 'TMPDIR'))
        assert kwargs['env']['PYTHONNOUSERSITE'] == '1'
        assert 'PYTHONHOME' not in kwargs['env'] and 'PYTHONPATH' not in kwargs['env']
    assert runner.os.environ['PYTHONHOME'] == 'external-home'
    assert runner.os.environ['PYTHONPATH'] == 'external-packages'
    assert runner.os.environ['PYTHONNOUSERSITE'] == '0'
    assert Path(receipt['run_root']).parent == project.parents[1].parent / 'runtime'


def test_check_probes_metadata_only_and_missing_module_fails(tmp_path, monkeypatch):
    project = setup_project(tmp_path)
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        assert '-c' in command
        return fake_probe(command, missing='soundfile')

    monkeypatch.setattr(runner.subprocess, 'run', execute)
    report = tmp_path / 'check.json'
    assert runner.run(check=True, report_path=report, project=project,
                      **host_roots(tmp_path)) == 1
    result = json.loads(report.read_text(encoding='utf-8'))
    assert len(commands) == 2
    assert result['suites']['auk']['errors'] == ['Missing modules: soundfile']
    assert result['suites']['core']['test_exit_code'] is None


def test_pytest_backed_drivers_receive_produced_fixtures(tmp_path, monkeypatch):
    """media_qc, guide_joins and creative_review read a tree an earlier pytest run wrote and
    refuse to invent one, so the suite produces it first: one --basetemp per producer, handed
    on as the driver's argv[2] (guide_joins recurses the run root rather than its basetemp)."""
    project = setup_project(tmp_path)
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        if '-c' in command:
            return fake_probe(command)
        if '-m' in command:
            return fake_output(stdout='3 passed in 1.2s\n')
        if command[1] in runner.BROWSER_MEDIA_TESTS.values():
            Path(command[2]).write_bytes(b'')  # the producer writes the media fixture
        return fake_output()

    monkeypatch.setattr(runner.subprocess, 'run', execute)
    report = tmp_path / 'produced.json'
    assert runner.run(report_path=report, project=project, **host_roots(tmp_path)) == 0
    receipt = json.loads(report.read_text(encoding='utf-8'))
    browser = receipt['suites']['browser']
    run_root = Path(receipt['run_root'])
    interpreter = str(project / '.venv/Scripts/python.exe')
    assert browser['errors'] == [] and browser['status'] == 'passed'
    assert sorted(browser['fixture_producers']) == sorted(runner.BROWSER_PYTEST_FIXTURES)
    for name, (module, handed) in runner.BROWSER_PYTEST_FIXTURES.items():
        basetemp = run_root / f'pytest-{name}'
        assert browser['fixture_producers'][name] == {
            'module': module, 'basetemp': str(basetemp), 'exit_code': 0,
            'output': '3 passed in 1.2s'}
        expected = basetemp if handed == 'basetemp' else run_root
        assert browser['fixture_roots'][name] == str(expected)
        produced = calls.index([interpreter, '-m', 'pytest', module, '-q', '--basetemp', str(basetemp)])
        driver = next(index for index, command in enumerate(calls)
                      if command[1:] == [f'tests/{name}.browser.mjs', str(expected)])
        assert produced < driver
    # The two class-2 drivers take the project interpreter and a runtime path that does not
    # exist yet, and user_context.path() rejects asset paths past 240 characters on Windows.
    for name in runner.BROWSER_FIXTURE_TESTS:
        fixture = Path(browser['fixture_roots'][name])
        assert fixture.parent == run_root and not fixture.exists()
        assert [interpreter, str(fixture)] in [
            command[2:] for command in calls if f'tests/{name}.browser.mjs' in command]
    # The media-backed driver is handed a file this suite encoded into the run root first,
    # because no fixture root holds an MP4 and the driver refuses to invent one.
    for name, module in runner.BROWSER_MEDIA_TESTS.items():
        target = run_root / f'{name}.mp4'
        assert browser['media_fixtures'][name] == str(target)
        produced = calls.index([interpreter, module, str(target)])
        driver = next(index for index, command in enumerate(calls)
                      if command[1:] == [f'tests/{name}.browser.mjs', str(target)])
        assert produced < driver


def test_check_reports_missing_browser_prerequisites(tmp_path):
    """--check must name a missing dependency install and a missing interpreter instead of
    letting every driver fail on its own import or spawn, one confusing line at a time."""
    project = setup_project(tmp_path)
    report = tmp_path / 'browser-check.json'
    (project / 'node_modules/esbuild/package.json').unlink()
    assert runner.run('browser', check=True, report_path=report, project=project,
                      **host_roots(tmp_path)) == 1
    browser = json.loads(report.read_text(encoding='utf-8'))['suites']['browser']
    assert browser['status'] == 'failed'
    assert browser['test_exit_code'] is None
    assert any('Missing browser dependencies under node_modules: esbuild' in error
               for error in browser['errors'])

    (project / '.venv/Scripts/python.exe').unlink()
    assert runner.run('browser', check=True, report_path=report, project=project,
                      **host_roots(tmp_path)) == 1
    browser = json.loads(report.read_text(encoding='utf-8'))['suites']['browser']
    assert any('require the project interpreter' in error for error in browser['errors'])


def test_missing_interpreter_is_failure_not_skip(tmp_path):
    project = tmp_path / 'ComfyUI-Workspace/projects/director-workbench'
    project.mkdir(parents=True)
    report = tmp_path / 'missing.json'
    assert runner.run(check=True, report_path=report, project=project,
                      **host_roots(tmp_path)) == 1
    result = json.loads(report.read_text(encoding='utf-8'))
    assert all(suite['status'] == 'failed' for suite in result['suites'].values())


def test_base_installation_boundary_and_audit_fields(tmp_path, monkeypatch):
    project = setup_project(tmp_path)
    local_base = project.parents[1] / 'runtimes/python/cpython-3.11'

    def execute(command, **kwargs):
        return fake_probe(command, base_prefix=local_base)

    monkeypatch.setattr(runner.subprocess, 'run', execute)
    report = tmp_path / 'local.json'
    assert runner.run(check=True, report_path=report, project=project,
                      **host_roots(tmp_path)) == 0
    result = json.loads(report.read_text(encoding='utf-8'))
    # Only interpreter-backed suites are probed and audited; the browser suite is not.
    probed = {name for name, suite in result['suites'].items() if 'environment' in suite}
    assert probed == {'core', 'auk'}
    for name in probed:
        suite = result['suites'][name]
        assert suite['warnings'] == []
        assert suite['environment']['enable_user_site'] is False
        assert suite['environment']['stdlib_path'] == 'base/Lib/os.py'
    local_base = tmp_path.parent / 'external-python'
    assert runner.run(check=True, report_path=report, project=project,
                      **host_roots(tmp_path)) == 0
    result = json.loads(report.read_text(encoding='utf-8'))
    assert all(len(result['suites'][name]['warnings']) == 1 for name in probed)


def test_failing_driver_output_reaches_the_report(tmp_path, monkeypatch):
    """A report outlives the console it was printed to. An exit code alone cannot tell a flake
    apart from a broken driver, so the driver's own output is kept, truncated to its tail --
    and repeated in the error line, which is what a reader sees first."""
    project = setup_project(tmp_path)
    noise = '\n'.join(f'line {index}' for index in range(runner.OUTPUT_TAIL_LINES + 5))

    def execute(command, **kwargs):
        if command[1] in runner.BROWSER_MEDIA_TESTS.values():
            Path(command[2]).write_bytes(b'')  # the producer writes the media fixture
        elif command[1].endswith('.browser.mjs'):
            return fake_output(1, stdout=noise + '\nError: locator timed out')
        return fake_output()

    monkeypatch.setattr(runner.subprocess, 'run', execute)
    report = tmp_path / 'output.json'
    assert runner.run('browser', report_path=report, project=project, **host_roots(tmp_path)) == 1
    browser = json.loads(report.read_text(encoding='utf-8'))['suites']['browser']
    script = 'tests/assembly_edit.browser.mjs'
    kept = browser['driver_output'][script]
    assert browser['status'] == 'failed'
    assert kept.splitlines()[-1] == 'Error: locator timed out'
    assert len(kept.splitlines()) == runner.OUTPUT_TAIL_LINES and 'line 0' not in kept
    failure = next(error for error in browser['errors'] if error.startswith(script))
    assert failure.startswith(f'{script} failed (exit 1).')
    assert 'Error: locator timed out' in failure


def test_failing_suite_output_reaches_the_report(tmp_path, monkeypatch):
    """The same for an interpreter suite: its exit code stays in test_exit_code and what the
    runner said is kept beside it, so the report explains itself without the console."""
    project = setup_project(tmp_path)

    def execute(command, **kwargs):
        if '-c' in command:
            return fake_probe(command)
        return fake_output(2, stdout='1 failed, 3 passed\nAssertionError: frame count differs\n',
                           stderr='short test summary info')

    monkeypatch.setattr(runner.subprocess, 'run', execute)
    report = tmp_path / 'core.json'
    assert runner.run('core', report_path=report, project=project, **host_roots(tmp_path)) == 1
    core = json.loads(report.read_text(encoding='utf-8'))['suites']['core']
    assert core['test_exit_code'] == 2
    assert 'AssertionError: frame count differs' in core['output']
    assert 'short test summary info' in core['output']
    assert core['errors'] == ['Test suite failed (exit 2).\n1 failed, 3 passed\n'
                              'AssertionError: frame count differs\nshort test summary info']


def impact_project(tmp_path, *, stale_mapping=False):
    """A fake checkout wired to a synthetic impact config for fast/affected suites."""
    project = setup_project(tmp_path)
    files = {
        'backend/widget.py': 'VALUE = 1\n',
        'backend/contract.py': 'VALUE = 1\n',
        'tests/test_smoke.py': '',
        'tests/test_widget.py': 'from backend import widget\n',
        'tests/test_docs_gate.py': '',
        'tests/alpha.browser.mjs': '',
        'tools/check_public_docs.py': '',
        'docs/guide.md': '# guide\n',
    }
    for name, text in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    stale = '''
        [[mapping]]
        name = "stale"
        paths = ["backend/stale.py"]
        tests = ["tests/test_removed_elsewhere.py"]
    ''' if stale_mapping else ''
    (project / 'tools/test_impact.toml').write_text(textwrap.dedent('''
        schema_version = 1
        default_base = "HEAD"

        [shared]
        paths = ["backend/contract.py"]

        [docs]
        paths = ["docs/**"]
        tests = ["tests/test_docs_gate.py"]
        gates = [
          { name = "public-docs", argv = ["python", "tools/check_public_docs.py"] },
        ]

        [frontend]
        paths = ["src/**"]

        [fast]
        budget_seconds = 60
        tests = ["tests/test_smoke.py"]

        [[mapping]]
        name = "widget"
        paths = ["backend/widget.py"]
        tests = ["tests/test_widget.py"]
        browser = ["tests/alpha.browser.mjs"]
    ''').lstrip() + textwrap.dedent(stale), encoding='utf-8')
    return project


def fake_impact_commands(calls):
    """A fake child runner that answers probes, pytest, gates and node drivers."""
    def execute(command, **kwargs):
        calls.append(command)
        if '-c' in command:
            return fake_probe(command)
        label = Path(command[1]).name if len(command) > 1 and not command[1].startswith('-') else 'pytest'
        return fake_output(stdout=f'{label} output')
    return execute


def test_fast_suite_runs_only_the_configured_list(tmp_path, monkeypatch):
    project = impact_project(tmp_path)
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        if '-c' in command:
            return fake_probe(command)
        return fake_output(stdout='2 passed\n')

    monkeypatch.setattr(runner.subprocess, 'run', execute)
    report = tmp_path / 'fast.json'
    assert runner.run('fast', report_path=report, project=project, **host_roots(tmp_path)) == 0
    fast = json.loads(report.read_text(encoding='utf-8'))['suites']['fast']
    assert fast['status'] == 'passed'
    assert fast['tests'] == ['tests/test_smoke.py']
    assert fast['budget_seconds'] == 60
    assert fast['elapsed_seconds'] < 60
    command = next(command for command in calls if '-m' in command)
    interpreter = str(project / '.venv/Scripts/python.exe')
    assert command == [interpreter, '-m', 'pytest', 'tests/test_smoke.py', '-q',
                       '--basetemp', str(Path(fast['temporary']).parent / 'pytest')]


def test_fast_suite_names_the_missing_timeout_plugin_install(tmp_path, monkeypatch):
    project = impact_project(tmp_path)

    def execute(command, **kwargs):
        if '-c' in command:
            return fake_probe(command, missing='pytest_timeout')
        return fake_output()

    monkeypatch.setattr(runner.subprocess, 'run', execute)
    report = tmp_path / 'fast-missing.json'
    assert runner.run('fast', report_path=report, project=project, **host_roots(tmp_path)) == 1
    fast = json.loads(report.read_text(encoding='utf-8'))['suites']['fast']
    assert fast['status'] == 'failed'
    assert any('pip install -r requirements-dev.txt' in error for error in fast['errors'])


def test_affected_suite_runs_the_selector_targets(tmp_path, monkeypatch):
    project = impact_project(tmp_path)
    monkeypatch.setattr(runner.select_tests, 'resolve_base', lambda root, base: 'a' * 40)
    monkeypatch.setattr(runner.select_tests, 'collect_changes', lambda root, base: [
        runner.select_tests.ChangedFile('backend/widget.py', 'M')])
    calls = []
    monkeypatch.setattr(runner.subprocess, 'run', fake_impact_commands(calls))
    report = tmp_path / 'affected.json'
    assert runner.run('affected', report_path=report, project=project,
                      base='HEAD', **host_roots(tmp_path)) == 0
    affected = json.loads(report.read_text(encoding='utf-8'))['suites']['affected']
    assert affected['status'] == 'passed'
    assert affected['mode'] == 'selected'
    assert affected['pytest_targets'] == ['tests/test_widget.py']
    assert affected['selection']['status'] == 'selected'
    interpreter = str(project / '.venv/Scripts/python.exe')
    assert [interpreter, '-m', 'pytest', 'tests/test_widget.py', '-q', '--basetemp',
            str(Path(affected['run_root']) / 'pytest')] in calls


def test_affected_shared_contract_runs_the_full_core_command(tmp_path, monkeypatch):
    project = impact_project(tmp_path)
    monkeypatch.setattr(runner.select_tests, 'resolve_base', lambda root, base: 'a' * 40)
    monkeypatch.setattr(runner.select_tests, 'collect_changes', lambda root, base: [
        runner.select_tests.ChangedFile('backend/contract.py', 'M')])
    calls = []
    monkeypatch.setattr(runner.subprocess, 'run', fake_impact_commands(calls))
    report = tmp_path / 'affected-core.json'
    assert runner.run('affected', report_path=report, project=project,
                      base='HEAD', **host_roots(tmp_path)) == 0
    affected = json.loads(report.read_text(encoding='utf-8'))['suites']['affected']
    assert affected['mode'] == 'full-core'
    core_args = runner.suite_specs(project, Path(affected['run_root']), tmp_path)['core']['args']
    assert [str(project / '.venv/Scripts/python.exe'), *core_args] in calls


def test_affected_stale_mapping_fails_without_running_a_subset(tmp_path, monkeypatch):
    project = impact_project(tmp_path, stale_mapping=True)
    monkeypatch.setattr(runner.select_tests, 'resolve_base', lambda root, base: 'a' * 40)
    monkeypatch.setattr(runner.select_tests, 'collect_changes', lambda root, base: [
        runner.select_tests.ChangedFile('backend/widget.py', 'M')])
    calls = []
    monkeypatch.setattr(runner.subprocess, 'run', fake_impact_commands(calls))
    report = tmp_path / 'affected-stale.json'
    assert runner.run('affected', report_path=report, project=project,
                      base='HEAD', **host_roots(tmp_path)) == 1
    affected = json.loads(report.read_text(encoding='utf-8'))['suites']['affected']
    assert affected['status'] == 'failed'
    assert any('test_removed_elsewhere.py' in error for error in affected['errors'])
    assert not any('-m' in command and 'pytest' in command for command in calls)


def test_affected_docs_change_runs_public_source_gates(tmp_path, monkeypatch):
    project = impact_project(tmp_path)
    monkeypatch.setattr(runner.select_tests, 'resolve_base', lambda root, base: 'a' * 40)
    monkeypatch.setattr(runner.select_tests, 'collect_changes', lambda root, base: [
        runner.select_tests.ChangedFile('docs/guide.md', 'M')])
    calls = []
    monkeypatch.setattr(runner.subprocess, 'run', fake_impact_commands(calls))
    report = tmp_path / 'affected-docs.json'
    assert runner.run('affected', report_path=report, project=project,
                      base='HEAD', **host_roots(tmp_path)) == 0
    affected = json.loads(report.read_text(encoding='utf-8'))['suites']['affected']
    interpreter = str(project / '.venv/Scripts/python.exe')
    assert affected['mode'] == 'selected'
    assert affected['selection']['status'] == 'docs-only'
    assert [interpreter, 'tools/check_public_docs.py'] in calls
    assert affected['gates'] == [{'name': 'public-docs', 'exit_code': 0, 'output': 'check_public_docs.py output'}]


def test_affected_with_browser_runs_the_selected_driver(tmp_path, monkeypatch):
    project = impact_project(tmp_path)
    monkeypatch.setattr(runner.select_tests, 'resolve_base', lambda root, base: 'a' * 40)
    monkeypatch.setattr(runner.select_tests, 'collect_changes', lambda root, base: [
        runner.select_tests.ChangedFile('backend/widget.py', 'M')])
    monkeypatch.setattr(runner.shutil, 'which', lambda name: 'node')
    calls = []
    monkeypatch.setattr(runner.subprocess, 'run', fake_impact_commands(calls))
    report = tmp_path / 'affected-browser.json'
    assert runner.run('affected', report_path=report, project=project, base='HEAD',
                      with_browser=True, **host_roots(tmp_path)) == 0
    affected = json.loads(report.read_text(encoding='utf-8'))['suites']['affected']
    assert affected['status'] == 'passed'
    assert ['node', 'tests/alpha.browser.mjs'] in calls
    assert affected['browser']['status'] == 'passed'

