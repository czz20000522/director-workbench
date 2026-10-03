"""Export an allowlisted source snapshot; never copy local Git history or data."""
import argparse
from pathlib import Path
import re
import shutil
import stat
import subprocess

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parents[2]
SUFFIXES = {'.py', '.ts', '.tsx', '.json', '.md', '.ps1', '.html', '.css', '.svg', '.txt', '.yml', '.mjs'}
PRIVATE_PARTS = {'.git', '.config', 'config', '.venv', 'node_modules', 'runtime', 'input', 'output',
                 'workspaces', 'private-workspaces', 'reviews', 'logs', 'dist', '__pycache__'}
SENSITIVE = re.compile(r'sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|'
                       r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY|'
                       r'100\.\d+\.\d+\.\d+|' + 'Tom' + 'czz|' + 'tomos' + '-mac|' +
                       r'[\w.+-]+@(?:gmail|qq|outlook)\.com', re.I)


def no_links(path: Path) -> None:
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('Linked source or destination paths are not allowed')


def public_sources(root: Path = ROOT) -> list[Path]:
    no_links(root)
    result = subprocess.run(['git', 'ls-files', '--cached', '-z'], cwd=root,
                            capture_output=True, check=True)
    names = sorted(set(filter(None, result.stdout.decode('utf-8').split('\0'))))
    if not names:
        raise ValueError('No tracked public sources')
    ignored = subprocess.run(['git', 'check-ignore', '--no-index', '-z', '--stdin'],
                             cwd=root, input=('\0'.join(names) + '\0').encode(), capture_output=True)
    if ignored.returncode not in (0, 1):
        raise ValueError('Could not verify source allowlist')
    denied = set(filter(None, ignored.stdout.decode('utf-8').split('\0')))
    selected = []
    failures = []
    for name in names:
        relative = Path(name)
        path = root / relative
        if (name in denied or relative.is_absolute() or '..' in relative.parts or
                PRIVATE_PARTS.intersection(relative.parts) or
                relative.is_relative_to(Path('ComfyUI-Workspace/projects/director-workbench/projects')) or
                relative.is_relative_to(Path('ComfyUI-Workspace/projects/director-workbench/workflows/snapshots')) or
                (path.suffix not in SUFFIXES and path.name != '.gitignore')):
            failures.append(name)
            continue
        try:
            no_links(path)
            if not path.is_file() or path.stat().st_size > 2_000_000:
                raise ValueError('Missing or oversized source')
            content = path.read_text(encoding='utf-8-sig')
            if SENSITIVE.search(content):
                raise ValueError('Sensitive pattern')
        except (OSError, ValueError):
            failures.append(name)
            continue
        selected.append(path)
    if failures:
        raise ValueError('Review required (paths only):\n' + '\n'.join(failures))
    return selected


def export(destination: Path, root: Path = ROOT) -> list[Path]:
    destination = destination.absolute()
    no_links(destination)
    if destination.exists():
        raise ValueError('Destination must be new; no overwrite or deletion is allowed.')
    sources = public_sources(root)
    for path in sources:
        target = destination / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--destination', type=Path)
    mode.add_argument('--check', action='store_true', help='Audit tracked working files without writing an export.')
    args = parser.parse_args()
    try:
        sources = public_sources() if args.check else export(args.destination)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f'Checked {len(sources)} files; {sum(p.stat().st_size for p in sources)} bytes' +
          (f'; exported to {args.destination}' if not args.check else ''))


if __name__ == '__main__':
    main()
