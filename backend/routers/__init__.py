"""路由子包：按领域拆分 backend/app.py 的 HTTP 接口。

每个模块自带 APIRouter，只通过 backend.<名称> 读取 app.py 的工作台级对象；
由 app.py 在拆分前的原位置 include_router，因此路由注册顺序与既有的
monkeypatch（setattr(backend, "X", ...)）语义都保持不变。
"""
