"""Select tests deterministically from a change set.

The selector answers one question: which checks can this change plausibly affect?
It reads ``tools/test_impact.toml``, diffs the working tree against a base ref
(including committed, staged, unstaged and untracked paths), and maps the change
to pytest targets, browser drivers and non-pytest gates.

Selection is rule based, explainable and fail-closed:

* a changed test file selects itself;
* source changes select the tests registered for their impact area, plus tests
  that import the changed module directly or transitively;
* shared contracts (schemas, route aggregation, auth/storage/task entry points,
  dependency manifests, CI/release and public entry points) escalate to the full
  core suite;
* documentation only selects the public-source/doc gates;
* UI changes select the browser drivers that mount the changed component;
* anything no rule explains escalates instead of being skipped, and says which
  mapping to add.

The tool is generic: roots, refs, suite list and mappings live in the config and
the command line, not in this module.  Exit code 0 means a usable selection was
produced (it may be the full core suite); exit code 2 means the config, the base
ref or the repository itself prevented a trustworthy answer.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path(__file__).with_name('test_impact.toml')
CONFIG_SCHEMA_VERSION = 1
# Source trees whose Python imports are analysed for reverse dependencies.  A path is
# generic configuration in spirit, but the analysis only reads modules that can import
# each other; the roots are named here so a test can point the selector at a fixture
# checkout without guessing.
IMPORT_ROOTS = ('backend', 'mcp_server', 'tools', 'production', 'tests')
EXIT_PATH_CHANGED = 2


class GitError(RuntimeError):
    """The repository could not answer the change-set question."""


def compile_glob(pattern: str) -> re.Pattern[str]:
    """Translate a repository-relative glob into an anchored regular expression.

    ``*`` and ``?`` stay inside one path segment, ``**`` crosses segments, and a
    ``**/`` prefix also matches zero directories.  This keeps ``src/*.tsx`` and
    ``docs/**`` behaving the way a reader of the config expects.
    """
    pattern = pattern.strip().replace('\\', '/')
    while pattern.startswith('./'):
        pattern = pattern[2:]
    output = ['^']
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == '*':
            if pattern[index:index + 2] == '**':
                index += 2
                if index < len(pattern) and pattern[index] == '/':
                    output.append('(?:.*/)?')
                    index += 1
                else:
                    output.append('.*')
            else:
                output.append('[^/]*')
                index += 1
        elif character == '?':
            output.append('[^/]')
            index += 1
        else:
            output.append(re.escape(character))
            index += 1
    output.append('$')
    return re.compile(''.join(output))


@dataclass(frozen=True)
class Pattern:
    raw: str
    regex: re.Pattern[str]

    @classmethod
    def of(cls, raw: str) -> 'Pattern':
        return cls(raw=raw, regex=compile_glob(raw))

    def matches(self, path: str) -> bool:
        return self.regex.match(path) is not None


@dataclass(frozen=True)
class Mapping:
    """One impact area: source globs mapped to pytest targets and browser drivers."""

    name: str
    paths: tuple[Pattern, ...]
    tests: tuple[str, ...] = ()
    browser: tuple[str, ...] = ()

    def matches(self, path: str) -> bool:
        return any(pattern.matches(path) for pattern in self.paths)


@dataclass(frozen=True)
class Config:
    path: Path
    root: Path
    default_base: str
    shared: tuple[Pattern, ...]
    docs: tuple[Pattern, ...]
    docs_tests: tuple[str, ...]
    docs_gates: tuple[dict, ...]
    frontend: tuple[Pattern, ...]
    frontend_shared: tuple[Pattern, ...]
    mappings: tuple[Mapping, ...]
    fast_tests: tuple[str, ...]
    fast_budget_seconds: int
    browsers: tuple[str, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChangedFile:
    """One path in the change set.  ``status`` follows ``git diff --name-status``."""

    path: str
    status: str = 'M'
    old_path: str | None = None


@dataclass
class FileDecision:
    path: str
    status: str
    rules: list[str] = field(default_factory=list)
    pytest: list[str] = field(default_factory=list)
    browser: list[str] = field(default_factory=list)
    gates: list[str] = field(default_factory=list)
    full_core: bool = False
    escalation: str | None = None

    def to_dict(self) -> dict:
        return {
            'path': self.path, 'status': self.status, 'rules': list(self.rules),
            'pytest': list(self.pytest), 'browser': list(self.browser),
            'gates': list(self.gates), 'full_core': self.full_core,
            'escalation': self.escalation,
        }


@dataclass
class Selection:
    base: str
    base_commit: str
    config_path: str
    changed: list[ChangedFile]
    pytest: list[str]
    browser: list[str]
    gates: list[dict]
    full_core: bool
    decisions: list[FileDecision]
    warnings: list[str]
    errors: list[str]
    docs_only: bool = False

    @property
    def status(self) -> str:
        if self.errors:
            return 'error'
        if self.full_core:
            return 'full-core'
        if not self.pytest and not self.browser and not self.gates:
            return 'empty'
        if self.docs_only and not self.browser:
            return 'docs-only'
        return 'selected'

    def to_dict(self) -> dict:
        return {
            'base': self.base,
            'base_commit': self.base_commit,
            'config': self.config_path,
            'status': self.status,
            'full_core': self.full_core,
            'docs_only': self.docs_only,
            'pytest': list(self.pytest),
            'browser': list(self.browser),
            'gates': [dict(gate) for gate in self.gates],
            'changed': [{'path': item.path, 'status': item.status, 'old_path': item.old_path}
                        for item in self.changed],
            'decisions': [decision.to_dict() for decision in self.decisions],
            'warnings': list(self.warnings),
            'errors': list(self.errors),
        }


def _require_str(data: dict, key: str, context: str, errors: list[str]) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f'{context}: "{key}" must be a non-empty string')
        return ''
    return value.strip()


def _pattern_list(data: dict, key: str, context: str, errors: list[str]) -> tuple[Pattern, ...]:
    raw = data.get(key, [])
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        errors.append(f'{context}: "{key}" must be a list of non-empty glob strings')
        return ()
    return tuple(Pattern.of(item) for item in raw)


def _target_list(data: dict, key: str, context: str, errors: list[str]) -> tuple[str, ...]:
    raw = data.get(key, [])
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        errors.append(f'{context}: "{key}" must be a list of non-empty target strings')
        return ()
    return tuple(item.strip() for item in raw)


def _gate_list(data: dict, key: str, context: str, errors: list[str]) -> tuple[dict, ...]:
    raw = data.get(key, [])
    if not isinstance(raw, list):
        errors.append(f'{context}: "{key}" must be a list of inline tables')
        return ()
    gates: list[dict] = []
    for index, entry in enumerate(raw):
        gate_context = f'{context}.{key}[{index}]'
        if not isinstance(entry, dict):
            errors.append(f'{gate_context}: must be an inline table')
            continue
        if set(entry) - {'name', 'argv'}:
            errors.append(f'{gate_context}: only "name" and "argv" are supported')
        name = _require_str(entry, 'name', gate_context, errors)
        argv = entry.get('argv')
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(item, str) or not item for item in argv)):
            errors.append(f'{gate_context}: "argv" must be a non-empty list of strings')
            continue
        gates.append({'name': name, 'argv': list(argv)})
    return tuple(gates)


def loads(text: str, path: Path, root: Path) -> Config:
    """Load and validate one impact config without touching the filesystem beyond ``root``."""
    errors: list[str] = []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return Config(path=path, root=root, default_base='origin/main', shared=(), docs=(),
                      docs_tests=(), docs_gates=(), frontend=(), frontend_shared=(),
                      mappings=(), fast_tests=(), fast_budget_seconds=60, browsers=(),
                      errors=(f'{path}: {exc}',))
    version = data.get('schema_version')
    if version != CONFIG_SCHEMA_VERSION:
        errors.append(f'{path}: schema_version must be {CONFIG_SCHEMA_VERSION}, found {version!r}')
    default_base = data.get('default_base', 'origin/main')
    if not isinstance(default_base, str) or not default_base:
        errors.append(f'{path}: default_base must be a non-empty git ref')
        default_base = 'origin/main'
    shared = _pattern_list(data.get('shared', {}), 'paths', f'{path}: [shared]', errors)
    docs = _pattern_list(data.get('docs', {}), 'paths', f'{path}: [docs]', errors)
    docs_tests = _target_list(data.get('docs', {}), 'tests', f'{path}: [docs]', errors)
    docs_gates = _gate_list(data.get('docs', {}), 'gates', f'{path}: [docs]', errors)
    if not docs_tests and not docs_gates:
        errors.append(f'{path}: [docs] must select "tests" or "gates" for documentation changes')
    frontend = _pattern_list(data.get('frontend', {}), 'paths', f'{path}: [frontend]', errors)
    frontend_shared = _pattern_list(data.get('frontend', {}), 'shared_paths',
                                    f'{path}: [frontend]', errors)
    fast = data.get('fast', {})
    fast_tests = _target_list(fast, 'tests', f'{path}: [fast]', errors)
    budget = fast.get('budget_seconds', 60)
    if not isinstance(budget, int) or budget <= 0:
        errors.append(f'{path}: [fast].budget_seconds must be a positive integer')
        budget = 60
    browsers = tuple(sorted(
        path.relative_to(root).as_posix()
        for path in (root / 'tests').glob('*.browser.mjs') if path.is_file()))
    mappings: list[Mapping] = []
    seen_names: set[str] = set()
    raw_mappings = data.get('mapping', [])
    if not isinstance(raw_mappings, list):
        errors.append(f'{path}: [[mapping]] entries must be an array of tables')
        raw_mappings = []
    for index, entry in enumerate(raw_mappings):
        context = f'{path}: [[mapping]] #{index + 1}'
        if not isinstance(entry, dict):
            errors.append(f'{context}: must be a table')
            continue
        unknown = set(entry) - {'name', 'paths', 'tests', 'browser'}
        if unknown:
            errors.append(f'{context}: unknown keys: {", ".join(sorted(unknown))}')
        name = _require_str(entry, 'name', context, errors)
        if name and name in seen_names:
            errors.append(f'{context}: duplicate mapping name "{name}"')
        seen_names.add(name)
        paths = _pattern_list(entry, 'paths', context, errors)
        tests = _target_list(entry, 'tests', context, errors)
        browser = _target_list(entry, 'browser', context, errors)
        if not paths:
            errors.append(f'{context}: "paths" must list at least one glob')
        if not tests and not browser:
            errors.append(f'{context}: mapping "{name}" selects neither tests nor browser drivers')
        mappings.append(Mapping(name=name, paths=paths, tests=tests, browser=browser))
    config = Config(path=path, root=root, default_base=default_base, shared=shared, docs=docs,
                    docs_tests=docs_tests, docs_gates=docs_gates,
                    frontend=frontend, frontend_shared=frontend_shared,
                    mappings=tuple(mappings), fast_tests=fast_tests,
                    fast_budget_seconds=budget, browsers=browsers, errors=tuple(errors))
    return config


def load_config(path: str | Path = DEFAULT_CONFIG,
                root: str | Path = PROJECT_ROOT) -> Config:
    """Load the impact config and report stale targets as errors.

    Every mapped pytest/browser/fast target must exist in ``root``.  A deleted or
    renamed test therefore leaves a loud error until its mapping is updated; a
    selector that quietly dropped the missing target would stop testing it.
    """
    root = Path(root).resolve()
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = root / config_path
    try:
        text = config_path.read_text(encoding='utf-8-sig')
    except OSError as exc:
        return Config(path=config_path, root=root, default_base='origin/main', shared=(),
                      docs=(), docs_tests=(), docs_gates=(), frontend=(), frontend_shared=(),
                      mappings=(), fast_tests=(), fast_budget_seconds=60, browsers=(),
                      errors=(f'{config_path}: cannot read impact config: {exc}',))
    config = loads(text, config_path, root)
    errors = list(config.errors)
    for mapping in config.mappings:
        for target in mapping.tests:
            file_part = target.split('::', 1)[0]
            if not (root / file_part).is_file():
                errors.append(f'{config.path}: mapping "{mapping.name}" targets missing test '
                              f'{file_part}; update the mapping after the rename/deletion')
        for target in mapping.browser:
            if not (root / target).is_file():
                errors.append(f'{config.path}: mapping "{mapping.name}" targets missing browser '
                              f'driver {target}; update the mapping after the rename/deletion')
    for target in config.fast_tests:
        file_part = target.split('::', 1)[0]
        if not (root / file_part).is_file():
            errors.append(f'{config.path}: [fast] targets missing test {file_part}')
    for target in config.docs_tests:
        file_part = target.split('::', 1)[0]
        if not (root / file_part).is_file():
            errors.append(f'{config.path}: [docs] targets missing test {file_part}')
    for gate in config.docs_gates:
        executable = (root / gate['argv'][1]) if len(gate['argv']) > 1 else None
        if executable is not None and not executable.is_file():
            errors.append(f'{config.path}: [docs] gate "{gate["name"]}" targets missing '
                          f'{gate["argv"][1]}')
    if not config.browsers:
        errors.append(f'{config.path}: no tests/*.browser.mjs drivers found under {root}')
    return Config(path=config.path, root=config.root, default_base=config.default_base,
                  shared=config.shared, docs=config.docs, docs_tests=config.docs_tests,
                  docs_gates=config.docs_gates, frontend=config.frontend,
                  frontend_shared=config.frontend_shared, mappings=config.mappings,
                  fast_tests=config.fast_tests, fast_budget_seconds=config.fast_budget_seconds,
                  browsers=config.browsers, errors=tuple(errors))


def _git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(['git', *args], cwd=root, capture_output=True, text=True,
                                   encoding='utf-8', errors='replace', check=False)
    except OSError as exc:
        raise GitError(f'git is not runnable: {exc}') from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or '').strip()
        raise GitError(f'git {" ".join(args)} failed: {detail}')
    return completed.stdout


def resolve_base(root: Path, base: str) -> str:
    """Return the commit ``base`` resolves to, or fail instead of selecting nothing."""
    output = _git(root, 'rev-parse', '--verify', '--quiet', f'{base}^{{commit}}').strip()
    if not output:
        raise GitError(f'base ref "{base}" does not resolve to a commit; fetch it or pass --base')
    return output


def collect_changes(root: Path, base: str) -> list[ChangedFile]:
    """Committed-vs-base, staged, unstaged and untracked changes as one sorted list."""
    root = Path(root).resolve()
    resolve_base(root, base)
    changes: dict[str, ChangedFile] = {}
    output = _git(root, 'diff', '--name-status', '-M', base, '--')
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split('\t')
        status = parts[0][:1]
        if status in {'R', 'C'} and len(parts) >= 3:
            old_path, path = parts[1], parts[2]
            changes[path] = ChangedFile(path=path, status=status, old_path=old_path)
            # A rename also removes the old path; keep it in the set so a mapping for
            # the old name is still exercised instead of silently going stale.
            changes.setdefault(old_path, ChangedFile(path=old_path, status='D'))
        elif len(parts) >= 2:
            changes[parts[1]] = ChangedFile(path=parts[1], status=status)
    untracked = _git(root, 'ls-files', '--others', '--exclude-standard')
    for line in untracked.splitlines():
        path = line.strip()
        if path and path not in changes:
            changes[path] = ChangedFile(path=path, status='?')
    return [changes[path] for path in sorted(changes)]


def _module_name(path: str) -> str | None:
    """Dotted module name for a repository-relative Python file, or None."""
    candidate = Path(path)
    if candidate.suffix != '.py':
        return None
    parts = list(candidate.with_suffix('').parts)
    if not parts:
        return None
    if parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(parts) if parts else None


def _test_target(path: str) -> bool:
    candidate = Path(path)
    return ((candidate.parent.as_posix() == 'tests' and candidate.name.startswith('test_')
             and candidate.suffix == '.py')
            or (candidate.parent.as_posix() == 'tools'
                and candidate.name.startswith('test_') and candidate.suffix == '.py'))


def _extract_imports(path: Path, module: str) -> set[str]:
    import ast
    try:
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    package = module.rsplit('.', 1)[0] if '.' in module else ''
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            if node.level:
                prefix_parts = package.split('.') if package else []
                if node.level - 1 > len(prefix_parts):
                    continue
                keep = prefix_parts[:len(prefix_parts) - (node.level - 1)] if node.level > 1 else prefix_parts
                base = '.'.join((*keep, base)) if base else '.'.join(keep)
            if base:
                found.add(base)
            for alias in node.names:
                if alias.name != '*':
                    found.add(f'{base}.{alias.name}' if base else alias.name)
    return found


class ImportIndex:
    """Reverse import graph over Python sources, used to widen a change to its tests."""

    def __init__(self, root: Path):
        self.root = root
        self.module_files: dict[str, str] = {}
        for top in IMPORT_ROOTS:
            directory = root / top
            if not directory.is_dir():
                continue
            for path in directory.rglob('*.py'):
                relative = path.relative_to(root).as_posix()
                module = _module_name(relative)
                if module:
                    self.module_files[module] = relative
        for name in ('paths.py',):
            if (root / name).is_file():
                self.module_files[name[:-3]] = name
        self.importers: dict[str, set[str]] = {}
        for module, relative in self.module_files.items():
            for imported in _extract_imports(root / relative, module):
                if imported in self.module_files:
                    self.importers.setdefault(imported, set()).add(module)
        self.stops: set[str] = set()

    def stop_at(self, paths: set[str]) -> None:
        self.stops = set(paths)

    def files_matching(self, patterns: tuple[Pattern, ...]) -> set[str]:
        """Source files matching the shared patterns, without walking the whole checkout."""
        return {relative for relative in self.module_files.values()
                if any(pattern.matches(relative) for pattern in patterns)}

    def affected_tests(self, path: str) -> list[str]:
        module = _module_name(path)
        if module is None or module not in self.module_files:
            return []
        selected: set[str] = set()
        seen = {module}
        queue = [module]
        while queue:
            current = queue.pop(0)
            for importer in sorted(self.importers.get(current, ())):
                if importer in seen:
                    continue
                seen.add(importer)
                relative = self.module_files[importer]
                if _test_target(relative):
                    selected.add(relative)
                    continue
                if relative in self.stops:
                    continue
                queue.append(importer)
        return sorted(selected)


def _dedupe(items: list[str]) -> list[str]:
    return sorted(dict.fromkeys(items))


def select(config: Config, changes: list[ChangedFile],
           base_commit: str = '') -> Selection:
    """Apply the rule table to an explicit change set.

    Pure with respect to git: the caller supplies the changes, which keeps the rule
    surface testable without creating repositories.  The returned selection always
    serialises the decision that produced every target.
    """
    warnings: list[str] = list(config.errors)
    errors: list[str] = list(config.errors)
    decisions: list[FileDecision] = []
    pytest_targets: list[str] = []
    browser_targets: list[str] = []
    gate_names: dict[str, dict] = {}
    full_core = False
    browser_available = set(config.browsers)
    import_index: ImportIndex | None = None
    for change in changes:
        path = change.path
        decision = FileDecision(path=path, status=change.status)
        exists = (config.root / path).is_file()
        deleted = change.status == 'D' or not exists

        matched_shared = next((pattern.raw for pattern in config.shared if pattern.matches(path)), None)
        matched_docs = next((pattern.raw for pattern in config.docs if pattern.matches(path)), None)
        matched_frontend_shared = next(
            (pattern.raw for pattern in config.frontend_shared if pattern.matches(path)), None)
        matched_frontend = next((pattern.raw for pattern in config.frontend if pattern.matches(path)), None)
        matched_mappings = [mapping for mapping in config.mappings if mapping.matches(path)]

        if matched_shared:
            decision.full_core = True
            decision.rules.append(f'shared-contract:{matched_shared}')
            full_core = True
        elif _test_target(path):
            decision.rules.append('test-file-self')
            if deleted:
                warnings.append(f'{path}: selected test file no longer exists (deleted or renamed); '
                                f'update tests and tools/test_impact.toml together')
            else:
                decision.pytest.append(path)
                pytest_targets.append(path)
        elif path.startswith('tests/') and path.endswith('.browser.mjs'):
            decision.rules.append('browser-driver-self')
            if deleted:
                warnings.append(f'{path}: selected browser driver no longer exists')
            elif path in browser_available:
                decision.browser.append(path)
                browser_targets.append(path)
            else:
                warnings.append(f'{path}: browser driver is not discoverable under tests/*.browser.mjs')
        elif matched_docs:
            decision.rules.append(f'docs-gate:{matched_docs}')
            for gate in config.docs_gates:
                decision.gates.append(gate['name'])
                gate_names.setdefault(gate['name'], dict(gate))
            for target in config.docs_tests:
                if not (config.root / target.split('::', 1)[0]).is_file():
                    errors.append(f'{path}: [docs] targets missing test '
                                  f'{target.split("::", 1)[0]}')
                    continue
                decision.pytest.append(target)
                pytest_targets.append(target)
        elif matched_mappings:
            for mapping in matched_mappings:
                decision.rules.append(f'mapping:{mapping.name}')
                for target in mapping.tests:
                    if not (config.root / target.split('::', 1)[0]).is_file():
                        errors.append(f'{path}: mapping "{mapping.name}" targets missing test '
                                      f'{target.split("::", 1)[0]}')
                        continue
                    decision.pytest.append(target)
                    pytest_targets.append(target)
                for target in mapping.browser:
                    if target not in browser_available:
                        errors.append(f'{path}: mapping "{mapping.name}" targets unknown browser '
                                      f'driver {target}')
                        continue
                    decision.browser.append(target)
                    browser_targets.append(target)
        elif matched_frontend_shared:
            decision.rules.append(f'frontend-shared:{matched_frontend_shared}')
            decision.browser.extend(config.browsers)
            browser_targets.extend(config.browsers)
        elif matched_frontend or path.startswith('src/') or path in {'index.html', 'prototype.html'}:
            pattern = matched_frontend or 'src/**'
            decision.rules.append(f'frontend-fallback:{pattern}')
            decision.browser.extend(config.browsers)
            browser_targets.extend(config.browsers)
            decision.escalation = 'all-browser'
            warnings.append(f'{path}: no browser-driver mapping for this UI change; selected every '
                            f'frontend driver. Add a [[mapping]] entry naming the component in '
                            f'tools/test_impact.toml.')
        elif path.startswith('tests/'):
            decision.full_core = True
            decision.rules.append('tests-support-fallback')
            full_core = True
            warnings.append(f'{path}: test support file has no impact mapping; escalated to the '
                            f'full core suite. Add a [[mapping]] entry naming its consumers.')
        else:
            if import_index is None:
                import_index = ImportIndex(config.root)
                import_index.stop_at(import_index.files_matching(config.shared))
            reverse_tests = import_index.affected_tests(path)
            if reverse_tests:
                decision.rules.append('import-reverse-dependency')
                decision.pytest.extend(reverse_tests)
                pytest_targets.extend(reverse_tests)
            else:
                decision.full_core = True
                decision.rules.append('unmapped-fallback')
                full_core = True
                decision.escalation = 'full-core'
                warnings.append(f'{path}: no ownership or import mapping explains this change; '
                                f'escalated to the full core suite. Add a [[mapping]] entry in '
                                f'tools/test_impact.toml rather than relying on the fallback.')
        decisions.append(decision)

    pytest_final = _dedupe(pytest_targets)
    browser_final = _dedupe([target for target in browser_targets if target in browser_available])
    gates = [gate_names[name] for name in sorted(gate_names)]
    docs_only = (bool(decisions) and not full_core and not browser_final
                 and all(decision.rules and all(rule.startswith('docs-gate:') for rule in decision.rules)
                         for decision in decisions))
    return Selection(base='', base_commit=base_commit, config_path=str(config.path),
                     changed=list(changes), pytest=pytest_final, browser=browser_final,
                     gates=gates, full_core=full_core, decisions=decisions,
                     warnings=_dedupe(warnings), errors=_dedupe(errors), docs_only=docs_only)


def selection_for(root: str | Path = PROJECT_ROOT, base: str | None = None,
                  config_path: str | Path | None = None) -> tuple[Selection, Config]:
    """Collect the change set and select checks for the configured base ref."""
    root = Path(root).resolve()
    config = load_config(config_path or DEFAULT_CONFIG, root)
    base_ref = base or config.default_base
    base_commit = resolve_base(root, base_ref)
    changes = collect_changes(root, base_ref)
    selection = select(config, changes, base_commit=base_commit)
    selection.base = base_ref
    return selection, config


def _human(selection: Selection, explain: bool) -> str:
    lines = [f'Base: {selection.base} @ {selection.base_commit[:12]}',
             f'Changed files: {len(selection.changed)}']
    if explain:
        for decision in selection.decisions:
            lines.append(f'  {decision.status} {decision.path}')
            for rule in decision.rules:
                lines.append(f'    rule: {rule}')
            if decision.pytest:
                lines.append('    pytest: ' + ', '.join(decision.pytest))
            if decision.browser:
                lines.append('    browser: ' + ', '.join(decision.browser))
            if decision.escalation:
                lines.append(f'    escalation: {decision.escalation}')
    else:
        for decision in selection.decisions:
            rules = ', '.join(decision.rules)
            lines.append(f'  {decision.status} {decision.path} -> {rules}')
    lines.append('Selected pytest: ' + (', '.join(selection.pytest) if selection.pytest
                                        else '(none)'))
    lines.append('Selected browser: ' + (', '.join(selection.browser) if selection.browser
                                         else '(none)'))
    lines.append('Gates: ' + (', '.join(gate['name'] for gate in selection.gates)
                              if selection.gates else '(none)'))
    if selection.full_core:
        lines.append('Escalated: full core suite (all backend/core tests)')
    for warning in selection.warnings:
        lines.append(f'[select] WARNING: {warning}')
    for error in selection.errors:
        lines.append(f'[select] ERROR: {error}')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base', help='Git ref to diff the working tree against '
                                       '(default: the config default_base, usually origin/main).')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG,
                        help='Impact config path (default: tools/test_impact.toml).')
    parser.add_argument('--root', type=Path, default=PROJECT_ROOT,
                        help='Repository root (default: the checkout containing this tool).')
    parser.add_argument('--json', action='store_true', help='Print the selection as JSON.')
    parser.add_argument('--explain', action='store_true',
                        help='Show every matched rule and selected target per changed file.')
    parser.add_argument('--check-config', action='store_true',
                        help='Validate mappings and fast targets, then exit without diffing.')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    config = load_config(args.config if args.config.is_absolute() else root / args.config, root)
    if args.check_config:
        if config.errors:
            for error in config.errors:
                print(f'[select] ERROR: {error}', file=sys.stderr)
            return EXIT_PATH_CHANGED
        print(f'Impact config OK: {len(config.mappings)} mapping(s), '
              f'{len(config.fast_tests)} fast test(s), {len(config.browsers)} browser driver(s).')
        return 0
    try:
        base_ref = args.base or config.default_base
        base_commit = resolve_base(root, base_ref)
        changes = collect_changes(root, base_ref)
    except GitError as exc:
        print(f'[select] ERROR: {exc}', file=sys.stderr)
        return EXIT_PATH_CHANGED
    selection = select(config, changes, base_commit=base_commit)
    selection.base = base_ref
    if args.json:
        print(json.dumps(selection.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_human(selection, args.explain))
    return EXIT_PATH_CHANGED if selection.errors else 0


if __name__ == '__main__':
    sys.exit(main())
