import json
from pathlib import Path
from types import SimpleNamespace

from tools import run_project_tests as runner


def setup_project(tmp_path):
    project = tmp_path / 'ComfyUI-Workspace/projects/director-workbench'
    project.mkdir(parents=True)
    for spec in runner.suite_specs(project, tmp_path).values():
        spec['python'].parent.mkdir(parents=True)
        spec['python'].touch()
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
    assert runner.run(report_path=report, project=project) == 1
    receipt = json.loads(report.read_text(encoding='utf-8'))
    assert receipt['suites']['core']['status'] == 'failed'
    assert receipt['suites']['auk']['status'] == 'passed'
    assert len(calls) == 4
    specs = runner.suite_specs(project, Path(receipt['run_root']))
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
    assert runner.run(check=True, report_path=report, project=project) == 1
    result = json.loads(report.read_text(encoding='utf-8'))
    assert len(commands) == 2
    assert result['suites']['auk']['errors'] == ['Missing modules: soundfile']
    assert result['suites']['core']['test_exit_code'] is None


def test_missing_interpreter_is_failure_not_skip(tmp_path):
    project = tmp_path / 'ComfyUI-Workspace/projects/director-workbench'
    project.mkdir(parents=True)
    report = tmp_path / 'missing.json'
    assert runner.run(check=True, report_path=report, project=project) == 1
    result = json.loads(report.read_text(encoding='utf-8'))
    assert all(suite['status'] == 'failed' for suite in result['suites'].values())


def test_base_installation_boundary_and_audit_fields(tmp_path, monkeypatch):
    project = setup_project(tmp_path)
    local_base = project.parents[1] / 'runtimes/python/cpython-3.11'

    def execute(command, **kwargs):
        return fake_probe(command, base_prefix=local_base)

    monkeypatch.setattr(runner.subprocess, 'run', execute)
    report = tmp_path / 'local.json'
    assert runner.run(check=True, report_path=report, project=project) == 0
    result = json.loads(report.read_text(encoding='utf-8'))
    for suite in result['suites'].values():
        assert suite['warnings'] == []
        assert suite['environment']['enable_user_site'] is False
        assert suite['environment']['stdlib_path'] == 'base/Lib/os.py'
    local_base = tmp_path.parent / 'external-python'
    assert runner.run(check=True, report_path=report, project=project) == 0
    result = json.loads(report.read_text(encoding='utf-8'))
    assert all(len(suite['warnings']) == 1 for suite in result['suites'].values())
