
import msgspec
from typing import Any

class HealthReport(msgspec.Struct):
    """ 健康状态汇报 """
    
    functional: str
    """ 来自哪个功能 """
    
    report_detail: dict[str, Any]
    """ 报告详细 """