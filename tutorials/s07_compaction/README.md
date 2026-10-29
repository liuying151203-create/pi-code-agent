# s07：上下文压缩（开发中）

本章目前只完成切点计算：从近期消息向前估算长度，不从工具结果开始保留，并识别切开的回合。会话压缩、摘要、保存恢复及自动触发尚未接入，暂不能使用 `/compact`。

本章以 s06 会话组件为起点，参考 Pi `e4c75a732` 的 `packages/coding-agent/src/core/compaction/compaction.ts`。完整设计和后续边界见 [上下文压缩设计](../../docs/context-compaction-design.md)。

验证切点：

```powershell
python -m pytest tests/test_s07_compaction.py --basetemp .sessions/s07-choose-a-new-id -p no:cacheprovider
```
