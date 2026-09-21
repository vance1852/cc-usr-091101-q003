# 分镜生成资产中枢

分镜生产的资产血缘中枢：镜头拆解、提示词包、参考图、生成任务、候选资产、
人工批注与最终采用关系全部落库可查，生成供应商通过可替换适配器接入。

## 血缘模型

```
镜头清单 ──► 生成任务（冻结输入快照 + 幂等任务号）──► 尝试（每次提交/重试一次，各计一次费）
                                                        │  回调收件箱（先落库、去重、按到达序处理）
                                                        ▼
                                              候选资产（摘要+格式核验通过）／隔离区（不符）
                                                        │  人工批注（批准/否决，必留人和理由）
                                                        ▼
                                              采用关系（镜头当前用哪张图，历史全保留）
                                                        │
                                                        ▼
                                              合成产物（成片镜头，记录成分资产快照）
```

变更（替换参考图 / 废弃提示词 / 否决资产）只打返工标记，从不自动改写已批准资产；
返工标记在人工重跑并重新采用（留人和理由）后自动清除。

## 核心不变量

1. **幂等任务号**：任务号 = 冻结输入（镜头 + 提示词包版本 + 参考图版本 + 供应商 + 参数）的
   SHA-256 摘要。同一组输入重复提交返回同一任务，不产生新尝试、不重复计费。
   已废弃的提示词版本、已被替换的参考图版本禁止用于新任务。
2. **唯一有效结果**：每个任务至多一份有效结果（`winning_attempt_id`）。超时重试在同一任务号下
   开出新尝试；重复回调按 `event_id` 去重；先到的失败通知只终结它自己的那次尝试；
   任务闭环后迟到的成功降级为 `SUPERSEDED`，不再取件。
3. **核验入库**：供应商文件只有 `sha256(字节) == 声明摘要` 且 `嗅探格式 == 声明格式`
   才进入候选区，否则连同事由进入隔离区，任务保持 OPEN 可重试。
4. **治理留痕**：批准/否决/采用/变更都必须记录操作人和理由；采用历史只增不改。
5. **崩溃可恢复**：回调先落库后处理；提交与计费标记同事务。`recover()` 重放未处理回调、
   按幂等键重提未落库的提交（供应商去重，不重复计费）、向供应商回收在途结果。

## 目录

| 路径 | 说明 |
| --- | --- |
| `src/asset_hub/hub.py` | 中枢服务：提交、回调、核验、批注、采用、变更影响、血缘、恢复 |
| `src/asset_hub/store.py` | SQLite 持久化（WAL），可重入事务 |
| `src/asset_hub/adapters.py` | 供应商适配器协议 + 确定性 `FakeProvider` |
| `src/asset_hub/contracts.py` | 供应商回调信封契约校验 |
| `src/asset_hub/verify.py` | 摘要与格式核验（魔数嗅探） |
| `src/asset_hub/models.py` / `ids.py` / `errors.py` | 状态机、幂等标识、异常 |
| `fixtures/` | 镜头清单、回调信封样例、资产摘要示例（`scripts/make_fixtures.py` 可重新生成） |
| `tests/` | 33 个单元测试，按验收点分文件 |
| `examples/demo_acceptance.py` | 端到端验收演示 |

## 运行

```bash
python -m unittest discover -s tests -v   # 全部测试
python examples/demo_acceptance.py        # 验收演示（含崩溃恢复）
```

项目使用 Python 3.11 以上版本，仅依赖标准库。时间必须带 UTC 偏移，摘要采用小写
十六进制 SHA-256，真实媒体文件和访问密钥不应提交。

## 接入真实供应商

实现 `ProviderAdapter` 协议即可接入真实模型或计费网关：

```python
class ProviderAdapter(Protocol):
    name: str
    def submit(self, request: SubmitRequest) -> ProviderJob: ...  # 按 idempotency_key 去重
    def poll(self, provider_job_id: str) -> dict | None: ...      # 回收失联任务结果
    def fetch(self, file_handle: str) -> bytes: ...               # 取回文件字节
```

供应商侧需满足：同一 `idempotency_key` 重复提交返回同一任务句柄且不重复计费；
成功回调声明 `asset_sha256` 与 `format`；回调允许重复、乱序、迟到。

## 验收点对应

| 验收点 | 入口 | 演示/测试 |
| --- | --- | --- |
| 冻结输入 + 幂等任务号 | `submit_generation` | `tests/test_idempotency.py` |
| 重试/重复回调/乱序不产生多份有效结果 | `ingest_callback` / `sweep_timeouts` | `tests/test_callbacks.py` |
| 摘要与声明格式吻合才进候选区 | `_process_event` → `verify_payload` | `tests/test_verification.py` |
| 变更影响镜头与合成产物 | `replace_reference` / `retire_prompt_pack` / `annotate(REJECT)` | `tests/test_impact.py` |
| 已批准资产不被覆盖，重新采用留人和理由 | `adopt` / 返工标记只增不清 | `tests/test_impact.py` |
| 从成片镜头反查输入、尝试、审批 | `composite_lineage` / `shot_lineage` | `tests/test_lineage.py` |
| 从变更列出未重跑的返工范围 | `rework_scope(change_id=...)` | `tests/test_impact.py` |
| 中断恢复不重复计费、不丢回调 | `recover()` | `tests/test_recovery.py`、演示第 9 节 |
