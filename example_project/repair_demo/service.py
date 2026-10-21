"""维护示例：汇总两项费用。"""

from calculator import add


def total(first: int, second: int) -> int:
    """调用基础计算并返回汇总值，不访问外部服务。"""
    return add(first, second)
