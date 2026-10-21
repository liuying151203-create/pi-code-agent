"""示例项目的公开业务测试；不是检索评测隐藏标签。"""

import unittest

from service import total


class ServiceTests(unittest.TestCase):
    """覆盖普通输入、零和负数，供容器内回归验证。"""

    def test_positive(self):
        self.assertEqual(total(2, 3), 5)

    def test_zero(self):
        self.assertEqual(total(0, 0), 0)

    def test_negative(self):
        self.assertEqual(total(-2, 3), 1)
