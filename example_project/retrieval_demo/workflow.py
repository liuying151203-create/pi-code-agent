"""检索示例入口：直接调用、同名方法调用和局部别名调用。"""

from math_ops.calc import Calculator
from storage import Store


def checkout() -> int:
    """演示计算后交给保存接口的流程。

    输入：无，使用固定整数；输出：计算结果。
    流程：计算两个数的和 → 构造保存结果 → 返回计算值；不访问磁盘或网络。
    """
    total = Calculator.add(2, 3)
    Store().add(total)
    return total


def preview() -> int:
    """演示通过局部别名调用同一个运算方法。

    输入：无，使用另一组固定整数；输出：计算结果。
    流程：取得函数对象 → 赋给局部变量 → 通过变量调用，用于观察文本检索的边界。
    """
    operation = Calculator.add
    return operation(4, 5)


if __name__ == "__main__":
    print(f"checkout: {checkout()}")
    print(f"preview: {preview()}")
