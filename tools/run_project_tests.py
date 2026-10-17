"""Run CPU tests in their owning environments without activation or installation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths
from tools.remove_tree import remove_tree

PROJECT = paths.PROJECT_ROOT
BROWSER_TESTS = (
    'agent_access', 'agent_page_bridge', 'assembly_edit', 'automatic_creation',
    'enhancement', 'generation_stage', 'guide_queue', 'guide_review',
    'media_tools_panel', 'queued_panels', 'reference_queue', 'speech_panel',
)
# These drivers additionally need the project interpreter and a fresh absolute runtime
# directory (relative or pre-existing roots raise AccessDenied in backend/user_context.py).
BROWSER_FIXTURE_TESTS = ('enhancement',)
# Run roots are named '<prefix><token>' with the current eight hex digits and the
# historical twelve.  The match is anchored: the machine runtime tree also holds live
# roots (accounts, agent pages, the relocation backup) and a prefix test would eat them.
RUN_ROOT_NAME = re.compile(r'^t-[0-9a-f]{8}(?:[0-9a-f]{4})?$')
KEEP_RUNS = 3
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


def suite_specs(project: Path, run_root: Path, desktop: Path | None = None) -> dict:
    # The host installation is a machine root, not a property of the checkout, so it
    # arrives as an explicit argument instead of a depth derivation: a relocated
    # checkout still points at the real host, and a test can hand in a synthetic one.
    host = paths.DESKTOP_ROOT if desktop is None else Path(desktop)
    return {
        'core': {
            'python': project / '.venv/Scripts/python.exe',
            'modules': ['fastapi', 'uvicorn', 'httpx', 'pytest', 'mcp', 'numpy', 'av'],
            'args': ['-m', 'pytest', 'tests', 'tools/test_build_video_finish_workflow.py',
                     '--ignore=tests/test_generate_auk_flash.py', '-q',
                     '--basetemp', str(run_root / 'pytest')],
        },
        'auk': {
            'python': host / 'ComfyUI-Shared/tools/AuK/.venv/Scripts/python.exe',
            'modules': ['numpy', 'soundfile', 'torch'],
            'args': ['-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_generate_auk_flash.py', '-v'],
        },
        # Browser acceptance drivers bundle the front end themselves and intercept their
        # own HTTP. Those listed in BROWSER_FIXTURE_TESTS also take the project Python and
        # a fresh absolute runtime directory. Drivers needing an MP4, ffmpeg or a completed
        # fixture root are not listed yet; see the browser-suite notes in the project docs.
        'browser': {
            'runner': 'node',
            'scripts': [f'tests/{name}.browser.mjs' for name in BROWSER_TESTS],
        },
    }


def report_suite(name: str, result: dict) -> None:
    result['status'] = 'failed' if result['errors'] else 'passed'
    for warning in result['warnings']:
        print(f'[{name}] WARNING: {warning}', flush=True)
    for error in result['errors']:
        print(f'[{name}] ERROR: {error}', flush=True)


def browser_suite(spec: dict, project: Path, run_root: Path, name: str, check: bool) -> dict:
    """Run browser acceptance drivers through node, failing rather than skipping when absent."""
    temporary = run_root / name
    temporary.mkdir()
    result = {'cwd': str(project), 'temporary': str(temporary),
              'warnings': [], 'errors': [], 'test_exit_code': None}
    environment = dict(os.environ)
    environment['PYTHONNOUSERSITE'] = '1'
    environment.update({key: str(temporary) for key in ('TMP', 'TEMP', 'TMPDIR')})
    runner = shutil.which('node')
    if runner is None:
        result['errors'].append('Required node runner is missing; this suite is failed, not skipped.')
        return result
    result['interpreter'] = runner
    missing = [script for script in spec['scripts'] if not (project / script).is_file()]
    if missing:
        result['errors'].append('Missing browser tests: ' + ', '.join(missing))
    if not check:
        commands = []
        fixtures = {}
        interpreter = project / '.venv/Scripts/python.exe'
        for script in spec['scripts']:
            if script in missing:
                continue
            command = [runner, script]
            name = Path(script).name.removesuffix('.browser.mjs')
            if name in BROWSER_FIXTURE_TESTS:
                if not interpreter.is_file():
                    result['errors'].append(f'{script} needs the project interpreter; this suite is failed, not skipped.')
                    continue
                # user_context.path() rejects asset paths over 240 characters on Windows and a
                # fixture root grows ~172 characters of generated account, series and project
                # names, so the fixture root is a short subdirectory of the short run root
                # instead of temporary/<driver name>. The receipt keeps the mapping legible.
                fixture = run_root / f'f{len(fixtures) + 1}'
                fixtures[name] = str(fixture)
                command += [str(interpreter), str(fixture)]
            commands.append(command)
            try:
                completed = subprocess.run(command, cwd=project, env=environment)
            except OSError as exc:
                result['errors'].append(f'{script} could not start: {type(exc).__name__}.')
                continue
            if completed.returncode:
                result['errors'].append(f'{script} failed (exit {completed.returncode}).')
        result['commands'] = commands
        result['fixture_roots'] = fixtures
        result['test_exit_code'] = int(bool(result['errors']))
    return result


def prune_run_roots(runtime_root: Path, *, keep: int = KEEP_RUNS) -> tuple[list[Path], list[str]]:
    """Remove this harness's own run roots beyond the newest ``keep``.

    A run leaves over a hundred megabytes of fixtures behind, so without this the machine
    runtime tree only grows.  The newest runs stay for diagnosis; what was removed is
    reported back so a run can say what it took away.  ``keep`` counts every root left
    behind, so a caller prunes once this run's own root exists rather than before it.
    """
    if keep < 0:
        raise ValueError('keep must not be negative')
    if not runtime_root.is_dir():
        return [], []
    candidates = [entry for entry in runtime_root.iterdir()
                  if RUN_ROOT_NAME.match(entry.name) and entry.is_dir() and not entry.is_symlink()]
    candidates.sort(key=_mtime, reverse=True)
    removed, errors = [], []
    for entry in candidates[keep:]:
        try:
            # remove_tree handles the two things a run root holds that plain rmtree does
            # not: read-only git objects in the pytest fixtures, and junctions.
            remove_tree(entry)
        except OSError as exc:
            errors.append(f'Old run root {entry.name} could not be removed: {type(exc).__name__}.')
            continue
        removed.append(entry)
    return removed, errors


def _mtime(entry: Path) -> float:
    try:
        return entry.stat().st_mtime
    except OSError:
        return 0.0


def run(suite: str = 'all', check: bool = False, report_path: Path | None = None,
        *, project: Path = PROJECT, desktop: Path | None = None,
        runtime: Path | None = None, keep_runs: int = KEEP_RUNS) -> int:
    if keep_runs < 1:
        raise ValueError('keep_runs must be at least 1: the run in flight is one of the kept roots')
    project = project.resolve()
    host = paths.DESKTOP_ROOT if desktop is None else Path(desktop).resolve()
    runtime_root = paths.RUNTIME_ROOT if runtime is None else Path(runtime).resolve()
    # A short run-root token keeps room for the fixture roots below; see browser_suite().
    run_root = runtime_root / ('t-' + uuid.uuid4().hex[:8])
    run_root.mkdir(parents=True, exist_ok=False)
    # Pruned after this run's root exists, so the tree settles at exactly ``keep_runs``
    # entries rather than one more than the operator asked for.
    pruned, prune_errors = prune_run_roots(runtime_root, keep=keep_runs)
    if pruned:
        print(f'Retention: removed {len(pruned)} run root(s) beyond the newest {keep_runs}: '
              + ', '.join(entry.name for entry in pruned), flush=True)
    for error in prune_errors:
        print(f'Retention: WARNING: {error}', flush=True)
    specs = suite_specs(project, run_root, host)
    names = list(specs) if suite == 'all' else [suite]
    receipt = {'mode': 'check' if check else 'tests', 'project': str(project),
               'run_root': str(run_root), 'suites': {},
               'retention': {'keep_runs': keep_runs,
                             'removed_run_roots': [str(entry) for entry in pruned],
                             'errors': prune_errors}}
    for name in names:
        spec = specs[name]
        if spec.get('runner') == 'node':
            print(f'[{name}] Runner: node', flush=True)
            receipt['suites'][name] = browser_suite(spec, project, run_root, name, check)
            report_suite(name, receipt['suites'][name])
            continue
        interpreter = spec['python']
        temporary = run_root / name
        temporary.mkdir()
        environment = dict(os.environ)
        environment.pop('PYTHONHOME', None)
        environment.pop('PYTHONPATH', None)
        environment['PYTHONNOUSERSITE'] = '1'
        environment.update({key: str(temporary) for key in ('TMP', 'TEMP', 'TMPDIR')})
        # Tests that must write inside the machine runtime tree ask tests/runtime_scratch.py
        # where to put their scratch; pointing it at this run root keeps those artifacts
        # inside the tree this run removes, instead of in a fixed directory that grows.
        environment['DIRECTOR_TEST_RUNTIME'] = str(run_root)
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
                    if not Path(details['base_prefix']).resolve().is_relative_to(host):
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
        report_suite(name, result)
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
    parser.add_argument('--suite', choices=('all', 'core', 'auk', 'browser'), default='all')
    parser.add_argument('--check', action='store_true', help='Probe interpreters and package metadata without importing models or running tests.')
    parser.add_argument('--report', type=Path, help='Optional JSON receipt path; default: unique workspace runtime directory.')
    parser.add_argument('--runtime', type=Path,
                        help='Override the workspace runtime root; a shorter root keeps fixture asset paths inside the backend path limit.')
    parser.add_argument('--keep-runs', type=int, default=KEEP_RUNS,
                        help='Run roots left behind for diagnosis, counting the run just '
                             f'started; older ones are removed first, so at least 1 '
                             f'(default: {KEEP_RUNS}).')
    args = parser.parse_args()
    return run(args.suite, args.check, args.report, runtime=args.runtime, keep_runs=args.keep_runs)


if __name__ == '__main__':
    sys.exit(main())
