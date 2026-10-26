"""新主线的离线模型替身，不导入归档实现。"""

from copy import deepcopy


class ScriptedModel:
    """按顺序返回真实内部消息，并保存每次请求的独立快照。"""

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return next(self.responses)
