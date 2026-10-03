"""Check Python syntax and relative Markdown links in the reviewed public files."""
import ast
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from export_public_source import ROOT, public_sources


def main():
    sources = public_sources()
    published = {path.relative_to(ROOT).as_posix() for path in sources}
    errors = []
    links = 0
    for path in sources:
        text = path.read_text(encoding='utf-8-sig')
        if path.suffix == '.py':
            ast.parse(text, filename=str(path.relative_to(ROOT)))
        if path.suffix != '.md':
            continue
        text = re.sub(r'```[^\n]*\n.*?```', '', text, flags=re.S)
        for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', text):
            target = target.strip().strip('<>')
            parts = urlsplit(target)
            if parts.scheme in {'https', 'http', 'mailto'} or target.startswith('#'):
                continue
            links += 1
            if parts.scheme or target.startswith('/'):
                errors.append(path.relative_to(ROOT).as_posix() + ': absolute/private link')
                continue
            candidate = (path.parent / unquote(parts.path)).resolve()
            relative = candidate.relative_to(ROOT).as_posix() if candidate.is_relative_to(ROOT) else None
            visible = relative is not None and (relative in published or any(name.startswith(relative + '/') for name in published))
            if not visible:
                errors.append(path.relative_to(ROOT).as_posix() + ': unpublished link ' + target)
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'Public source syntax and {links} relative links passed ({len(sources)} files).')


if __name__ == '__main__':
    main()
