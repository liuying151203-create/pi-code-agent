"""检索示例：带装饰器和嵌套函数的数值运算，不依赖 Harness。"""


class Calculator:
    """提供负数归零后再相加的运算，供搜索定义与范围补读使用。"""

    @staticmethod
    def add(a: int, b: int) -> int:
        """将两个输入分别归零负数后相加，返回整数结果。

        输入：两个整数；输出：处理后的和。
        流程：定义局部处理函数 → 分别处理输入 → 返回相加结果；不产生外部副作用。
        """

        def normalize(value: int) -> int:
            """把负数归零，非负整数保持不变。"""
            return max(0, value)

        return normalize(a) + normalize(b)
