"""Export an allowlisted source snapshot; never copy local Git history or data."""
import argparse
from pathlib import Path
import re
import shutil

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parents[2]
PREFIX = PROJECT.relative_to(ROOT)
DIRECTORIES = ('backend', 'src', 'tests', 'contracts', 'mcp_server', '.agents')
FILES = ('AGENTS.md', '.gitignore', 'package.json', 'package-lock.json', 'index.html',
         'tsconfig.json', 'vite.config.ts', 'requirements.txt', 'requirements-mcp.txt',
         'requirements-audio-tests.txt', 'run_tests.ps1', 'start_backend.ps1',
         'start_workbench.ps1', 'start_private_workbench.ps1', 'enable_lan_access.ps1',
         'docs/github-collaboration.md')
SUFFIXES = {'.py', '.ts', '.tsx', '.json', '.md', '.ps1', '.html', '.css', '.svg', '.txt', '.yml'}
SENSITIVE = re.compile(r'sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|'
                       r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY|'
                       r'100\.\d+\.\d+\.\d+|Tomczz|tomos-mac|'
                       r'[\w.+-]+@(?:gmail|qq|outlook)\.com', re.I)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args()
    destination = args.destination.resolve()
    if destination.exists():
        raise SystemExit('Destination must be new; no overwrite or deletion is allowed.')
    sources = [PROJECT / name for name in FILES]
    sources.extend(ROOT / name for name in ('README.md', 'AGENTS.md', '.gitignore', 'CONTRIBUTING.md', 'SECURITY.md'))
    sources.extend(path for path in (ROOT / '.github').rglob('*') if path.is_file())
    for name in DIRECTORIES:
        sources.extend(path for path in (PROJECT / name).rglob('*') if path.is_file())
    sources.extend((PROJECT / 'tools').glob('*.py'))
    sources.append(PROJECT / 'public/favicon.svg')
    sources.append(ROOT / 'ComfyUI-Workspace/production/storage_layout.py')
    shared = ROOT / '.agents/skills/aigc-video-production'
    sources.extend((shared / 'scripts').glob('*.py'))
    sources.extend((shared / 'assets/workflows').glob('*.json'))
    selected = []
    failures = []
    for path in sorted(set(sources)):
        if path.name == 'create_galianshou_plan.py':
            continue
        if (path.suffix not in SUFFIXES and path.name != '.gitignore') or '__pycache__' in path.parts:
            continue
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            failures.append(str(path.relative_to(ROOT)))
            continue
        content = path.read_text(encoding='utf-8-sig')
        # The scanner source contains its own deny patterns, not credentials.
        if path != Path(__file__).resolve() and SENSITIVE.search(content):
            failures.append(str(path.relative_to(ROOT)))
        selected.append(path)
    if failures:
        raise SystemExit('Review required (paths only):\n' + '\n'.join(failures))
    for path in selected:
        target = destination / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    print(f'Exported {len(selected)} files; {sum(p.stat().st_size for p in selected)} bytes to {destination}')


if __name__ == '__main__':
    main()
