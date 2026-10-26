# 新主线问题记录

旧版 B001–B041 完整保留在 [v1 问题记录](../archive/v1/docs/bug-log.md)。新主线使用 V2 前缀，后续定位问题时记录复现、原因、修复和实际验证。

当前首次重写的已验证行为包括：工具错误不依赖正文前缀；参数校验先于执行；截断调用不执行；原生取消传播并补齐未完成工具结果；注入客户端不被 Provider 提前关闭。没有据此宣称验证真实模型或实际网络兼容性。

## V2-B001：Chat 适配不应改写工具错误正文

2026-10-26，在本轮尚未交付的重写草稿中，Chat 适配器曾给 `is_error=True` 的工具结果加上错误前缀。静态对照 Pi `e4c75a732` 的 `packages/ai/src/api/openai-completions.ts` 可见，Pi 直接传递工具正文；这项添加既改变了工具输出，也没有协议依据。

修复仅限 Chat 消息投影：保留原文和调用 ID；内部 `is_error` 继续保留，Anthropic 继续使用其原生 `is_error` 字段。回归 `tests/test_s03_models.py::test_batch_results_keep_ids_and_only_protocol_adapter_wraps_errors` 分别检查两种协议的正文、调用关联与错误字段，新主线 66 项测试通过。
