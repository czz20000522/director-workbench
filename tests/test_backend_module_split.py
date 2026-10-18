"""拆分出去的 backend/routers 模块，其模块层定义必须仍能从 backend.app 按同一对象取到。

测试与 app.py 其余代码都用 `backend.<名称>` 取用这些名字（含 monkeypatch），
所以 routers 里的每个模块级定义都要在 app.py 里按原样再导出；漏一个只会在
运行时表现为属性缺失，容易被宽 except 吞掉。

同理，路由模块内部互相调用也必须写成 `backend.<名称>`（在调用时读取）。
裸调用会绕过 monkeypatch：原实现里这些函数同属一个模块全局，打补丁是生效的，
拆出后裸调用读到的是本模块自己的定义，补丁就静默失效了。
"""
import ast
import importlib
from pathlib import Path

from backend import app as backend


ROUTERS = Path(backend.__file__).parent / 'routers'


def router_modules() -> list[tuple[str, Path]]:
    return [(path.stem, path) for path in sorted(ROUTERS.glob('*.py')) if path.stem != '__init__']


def module_definitions(tree: ast.Module) -> list[str]:
    return [node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def local_names(function: ast.AST) -> set[str]:
    """函数（含嵌套作用域）里会被当成局部名的标识符，用来排除同名的假引用。"""
    names = {node.arg for node in ast.walk(function) if isinstance(node, ast.arg)}
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not function:
            names.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.Global):
            names.difference_update(node.names)
    return names


def test_routers_package_has_at_least_one_module():
    assert router_modules()


def test_every_router_definition_is_reexported_from_app():
    for stem, path in router_modules():
        module = importlib.import_module(f'backend.routers.{stem}')
        tree = ast.parse(path.read_text(encoding='utf-8'))
        definitions = module_definitions(tree)
        assert definitions, stem
        for name in definitions:
            assert getattr(backend, name, None) is getattr(module, name), f'{stem}.{name}'


def test_router_bodies_call_their_own_definitions_through_backend():
    offenders = []
    for stem, path in router_modules():
        tree = ast.parse(path.read_text(encoding='utf-8'))
        definitions = set(module_definitions(tree))
        for function in tree.body:
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            locals_ = local_names(function)
            for node in ast.walk(function):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                        and node.id in definitions and node.id not in locals_:
                    offenders.append(f'{stem}.py:{node.lineno} {function.name}() 裸调用 {node.id}')
    assert not offenders, offenders
