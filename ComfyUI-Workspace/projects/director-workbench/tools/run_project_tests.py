"""Run CPU tests in their owning environments without activation or installation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


PROJECT = Path(__file__).resolve().parents[1]
PROBE = """
import importlib.util, importlib.metadata, json, os, site, sys
modules = {}
for name in sys.argv[1:]:
    try:
        found = importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        found = False
    try:
        version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        version = None
    modules[name] = {'available': found, 'version': version}
print(json.dumps({'executable': sys.executable, 'base_prefix': sys.base_prefix,
                  'enable_user_site': site.ENABLE_USER_SITE, 'stdlib_path': os.__file__,
                  'version': sys.version, 'modules': modules}))
"""


def suite_specs(project: Path, run_root: Path) -> dict:
    desktop = project.parents[2]
    return {
        'core': {
            'python': project / '.venv/Scripts/python.exe',
            'modules': ['fastapi', 'uvicorn', 'httpx', 'pytest', 'mcp', 'numpy', 'av'],
            'args': ['-m', 'pytest', 'tests', 'tools/test_build_video_finish_workflow.py',
                     '--ignore=tests/test_generate_auk_flash.py', '-q',
                     '--basetemp', str(run_root / 'pytest')],
        },
        'auk': {
            'python': desktop / 'ComfyUI-Shared/tools/AuK/.venv/Scripts/python.exe',
            'modules': ['numpy', 'soundfile', 'torch'],
            'args': ['-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_generate_auk_flash.py', '-v'],
        },
    }


def run(suite: str = 'all', check: bool = False, report_path: Path | None = None,
        *, project: Path = PROJECT) -> int:
    project = project.resolve()
    run_root = project.parents[1] / 'runtime' / ('t-' + uuid.uuid4().hex[:12])
    run_root.mkdir(parents=True, exist_ok=False)
    specs = suite_specs(project, run_root)
    names = list(specs) if suite == 'all' else [suite]
    receipt = {'mode': 'check' if check else 'tests', 'project': str(project),
               'run_root': str(run_root), 'suites': {}}
    for name in names:
        spec = specs[name]
        interpreter = spec['python']
        temporary = run_root / name
        temporary.mkdir()
        environment = dict(os.environ)
        environment.pop('PYTHONHOME', None)
        environment.pop('PYTHONPATH', None)
        environment['PYTHONNOUSERSITE'] = '1'
        environment.update({key: str(temporary) for key in ('TMP', 'TEMP', 'TMPDIR')})
        result = {'interpreter': str(interpreter), 'cwd': str(project), 'temporary': str(temporary),
                  'warnings': [], 'errors': [], 'test_exit_code': None}
        receipt['suites'][name] = result
        print(f'[{name}] Python: {interpreter}', flush=True)
        if not interpreter.is_file():
            result['errors'].append('Required interpreter is missing; this suite is failed, not skipped.')
        else:
            try:
                probe = subprocess.run([str(interpreter), '-c', PROBE, *spec['modules']],
                                       cwd=project, env=environment, capture_output=True, text=True,
                                       encoding='utf-8', errors='replace', timeout=60)
                if probe.returncode:
                    result['errors'].append(f'Environment probe failed (exit {probe.returncode}).')
                else:
                    details = json.loads(probe.stdout)
                    result['environment'] = details
                    missing = [key for key, value in details['modules'].items() if not value['available']]
                    if missing:
                        result['errors'].append('Missing modules: ' + ', '.join(missing))
                    if not Path(details['base_prefix']).resolve().is_relative_to(project.parents[2]):
                        result['warnings'].append('Python base_prefix is outside the Comfy-Desktop root; the environment depends on that installation.')
            except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
                result['errors'].append(f'Environment probe could not complete: {type(exc).__name__}.')
            if not check:
                command = [str(interpreter), *spec['args']]
                result['command'] = command
                try:
                    completed = subprocess.run(command, cwd=project, env=environment)
                    result['test_exit_code'] = completed.returncode
                    if completed.returncode:
                        result['errors'].append(f'Test suite failed (exit {completed.returncode}).')
                except OSError as exc:
                    result['errors'].append(f'Test suite could not start: {type(exc).__name__}.')
        result['status'] = 'failed' if result['errors'] else 'passed'
        for warning in result['warnings']:
            print(f'[{name}] WARNING: {warning}', flush=True)
        for error in result['errors']:
            print(f'[{name}] ERROR: {error}', flush=True)
    failed = [name for name, result in receipt['suites'].items() if result['status'] == 'failed']
    receipt['exit_code'] = int(bool(failed))
    destination = report_path.resolve() if report_path else run_root / 'report.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Summary: ' + ', '.join(f"{name}={result['status']}" for name, result in receipt['suites'].items()), flush=True)
    print(f'Report: {destination}', flush=True)
    return receipt['exit_code']


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('all', 'core', 'auk'), default='all')
    parser.add_argument('--check', action='store_true', help='Probe interpreters and package metadata without importing models or running tests.')
    parser.add_argument('--report', type=Path, help='Optional JSON receipt path; default: unique workspace runtime directory.')
    args = parser.parse_args()
    return run(args.suite, args.check, args.report)


if __name__ == '__main__':
    sys.exit(main())
