""" 全局扩展注册表 + importlib 自动扫描（架构文档 §5.3）"""

import importlib
import pkgutil
from typing import Any, Callable
from ..schemas.model_schema import ToolFunc, ToolSpec
from ..utils.logger import LoggerManager

_registry_logger = LoggerManager.get_logger("REGISTRY")

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

class ToolRegistry:
    """ 全局工具注册表：装饰器注册 + 自动包装为 ToolSpec """
    
    _tools: dict[str, ToolSpec] = {}
    
    @classmethod
    def tool(cls, tool_func: ToolFunc, *, desc: str = "", name: str = ""):
        """ 装饰器：注册一个工具函数，自动包装为 ToolSpec
        
        用法：
        @ToolRegistry.tool
        def get_current_time() -> str:
            '''获取当前时间'''
            return "现在是..."
        
        @ToolRegistry.tool(desc="检索记忆")
        def async_retrieve_memory(query: str, limit: int = 5) -> str:
            ...
        """
        def decorator(func: ToolFunc) -> ToolFunc:
            tool_name = name or func.__name__
            _registry_logger.info(f"注册工具：{tool_name}")
            if tool_name in cls._tools:
                raise ValueError(f"工具重复注册: {tool_name}")
            
            # 自动包装为 ToolSpec
            spec = ToolSpec(tool_func=func, desc=desc)
            cls._tools[tool_name] = spec
            return func  # 返回原函数，保持可用
        
        # 支持 @ToolRegistry.tool 和 @ToolRegistry.tool(desc="...") 两种用法
        if tool_func is not None:
            return decorator(tool_func)
        return decorator
    
    @classmethod
    def get(cls, name: str) -> ToolSpec:
        """ 按名字获取工具 """
        if name not in cls._tools:
            raise KeyError(f"未找到工具: {name}")
        return cls._tools[name]
    
    @classmethod
    def all(cls) -> list[ToolSpec]:
        """ 获取全部工具 """
        return list(cls._tools.values())
    
    @classmethod
    def register_external(cls, tool: ToolSpec) -> None:
        """ 注册外部传入的 ToolSpec（不走装饰器） """
        if tool.tool_func.__name__ in cls._tools:
            raise ValueError(f"工具重复注册: {tool.tool_func.__name__}")
        cls._tools[tool.tool_func.__name__] = tool
    
    @classmethod
    def clear(cls) -> None:
        """ 清空全部工具（测试用） """
        cls._tools.clear()