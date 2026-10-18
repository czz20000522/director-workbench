"""拆分出去的 backend/routers 模块，其模块层定义必须仍能从 backend.app 按同一对象取到。

测试与 app.py 其余代码都用 `backend.<名称>` 取用这些名字（含 monkeypatch），
所以 routers 里的每个模块级定义都要在 app.py 里按原样再导出；漏一个只会在
运行时表现为属性缺失，容易被宽 except 吞掉。
"""
import ast
import importlib
from pathlib import Path

from backend import app as backend


ROUTERS = Path(backend.__file__).parent / 'routers'


def router_modules() -> list[tuple[str, Path]]:
    return [(path.stem, path) for path in sorted(ROUTERS.glob('*.py')) if path.stem != '__init__']


def test_routers_package_has_at_least_one_module():
    assert router_modules()


def test_every_router_definition_is_reexported_from_app():
    for stem, path in router_modules():
        module = importlib.import_module(f'backend.routers.{stem}')
        tree = ast.parse(path.read_text(encoding='utf-8'))
        definitions = [node.name for node in tree.body
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        assert definitions, stem
        for name in definitions:
            assert getattr(backend, name, None) is getattr(module, name), f'{stem}.{name}'
