import json
from pathlib import Path
from types import SimpleNamespace

from tools import run_project_tests as runner


def host_roots(tmp_path):
    """Keep the run inside tmp_path: the host and runtime roots are machine roots, so
    they are injected here rather than picked up from the real Comfy-Desktop install."""
    return {'desktop': tmp_path, 'runtime': tmp_path / 'ComfyUI-Workspace/runtime'}


def setup_project(tmp_path):
    project = tmp_path / 'ComfyUI-Workspace/projects/director-workbench'
    project.mkdir(parents=True)
    for spec in runner.suite_specs(project, tmp_path, tmp_path).values():
        for script in spec.get('scripts', ()):  # browser drivers live in the checkout
            (project / script).parent.mkdir(parents=True, exist_ok=True)
            (project / script).touch()
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
    }))


def test_failure_keeps_running_other_suite_with_explicit_paths(tmp_path, monkeypatch):
    project = setup_project(tmp_path)
    calls = []

    def execute(command, **kwargs):
        calls.append((command, kwargs))
        if '-c' in command:
            return fake_probe(command)
        return SimpleNamespace(returncode=2 if 'pytest' in command else 0)

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
    # Two probes and two interpreter suites, plus one node command per browser driver.
    assert len(calls) == 4 + len(specs['browser']['scripts'])
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
    assert Path(receipt['run_root']).parent == project.parents[1] / 'runtime'


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
