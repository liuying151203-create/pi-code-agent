"""检索示例：与计算方法同名但含义不同的存储接口，不连接数据库。"""


class Store:
    """只构造保存结果，不写文件；供同名方法检索与调用区分使用。"""

    def add(self, item: object) -> dict[str, object]:
        """接收任意对象，包装为保存结果字典并返回，不维护持久状态。"""
        return {"saved": item}
