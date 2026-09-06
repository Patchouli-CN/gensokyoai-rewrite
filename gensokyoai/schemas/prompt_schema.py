
import msgspec
from string import Template

class Prompt(msgspec.Struct):
    """ 提示词 """
    template: str = ""
    
    name: str = "默认文本"
    
    def render(self, **params) -> str:
        """ 渲染文本 """
        return Template(self.template).safe_substitute(**params)
    
    def raw(self) -> str:
        """ 未渲染的原始模板 """
        return self.template