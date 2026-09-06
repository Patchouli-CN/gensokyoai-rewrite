""" 全局扩展注册表 + importlib 自动扫描（架构文档 §5.3）"""

import importlib
import pkgutil
from typing import Any, Callable

class Registry:
    """ 全局注册表：name -> {class, ext_type} """

    _modules: dict[str, dict[str, Any]] = {}

    @classmethod
    def register(cls, name: str, ext_type: str) -> Callable[[type], type]:
        """ 装饰器：注册扩展类，返回原类 """
        def decorator(module_class: type) -> type:
            if name in cls._modules:
                raise ValueError(f"扩展重复注册: {name} ({ext_type})")
            cls._modules[name] = {"class": module_class, "type": ext_type}
            return module_class
        return decorator

    @classmethod
    def get(cls, name: str) -> type:
        """ 按名字取扩展类 """
        return cls._modules[name]["class"]

    @classmethod
    def get_by_type(cls, ext_type: str) -> list[type]:
        """ 按类型取一批扩展类 """
        return [m["class"] for m in cls._modules.values() if m["type"] == ext_type]

def auto_discover(package_path: str) -> None:
    """ 扫描包下所有模块，触发装饰器注册（类似 Spring ComponentScan）"""
    package = importlib.import_module(package_path)
    for _, module_name, _ in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        importlib.import_module(module_name)
