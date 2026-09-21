# 分镜生成资产中枢

让镜头拆解、提示词包、参考图、生成任务、候选资产、人工批注与最终采用
之间拥有**可反查的血缘**，并保证：

- 每次提交生成都**冻结输入版本**并取得**幂等任务号**；
- 超时重试、重复回调、先到的失败通知**不会制造多份有效结果**；
- 供应商文件只有在 **SHA-256 与声明格式都吻合**时才进入候选区；
- 已批准资产**不会被自动覆盖**，重新采用必须留下**人和理由**；
- 任一变更（换参考、改提示词、否决图片）都能列出**受影响镜头、合成产物
  与尚未重跑的返工范围**；
- 进程被 kill -9 后重启，**任务不重复计费、回调不丢失**。

只依赖 Python 标准库（SQLite 账本 + 内容寻址对象仓库），真实模型通过
可替换适配器接入。

## 目录

```
src/asset_hub/
  contracts.py   供应商回调信封契约（基线，保持兼容）
  models.py      领域实体：镜头/提示词包版本/参考图版本/冻结输入/尝试/候选/审批/变更
  adapters.py    适配器协议与注册（echo-image、flaky-video 两个内置演示适配器）
  store.py       SQLite 账本（唯一约束即去重防线）+ objects/quarantine 对象仓库
  hub.py         核心服务：冻结、幂等派发、回调状态机、校验隔离、采用留痕
  inbox.py       持久收件箱、单工人消费者、崩溃恢复
  lineage.py     血缘反查 trace_shot 与变更影响面 stale_work
  media.py       摘要、格式嗅探、声明校验、确定性样例 PNG
examples/
  studio_walkthrough.py  业务全流程走查（废弃服装词 → 返工单 → 闭环）
  recovery_demo.py       真实子进程 SIGKILL 恢复演示
fixtures/callbacks.json 回调信封样例（含乱序失败/成功）
```

## 快速开始

```bash
python3 -m unittest discover -s tests -v   # 28 个测试
python3 examples/studio_walkthrough.py     # 业务走查
python3 examples/recovery_demo.py          # kill -9 恢复演示
```

## 数据模型与血缘

```
Shot ──< PromptPackage v1,v2,…（旧版永不改写，废弃变量记入 deprecated）
  │
  ├──< ReferenceImage v1,v2,…（可跨镜头共享，绑定关系显式登记）
  │
  └──< GenerationAttempt
         · FrozenInput = 适配器 + 提示词版本+渲染摘要 + 参考图(版本,摘要) + 参数
         · frozen_digest = SHA-256(规范化 JSON) → 派生幂等任务号
         ├── Dispatch（idempotency_key 唯一：计费凭证，重试/恢复复用一行）
         ├── Callbacks（event_id 唯一；重复/迟到/冲突全部留证不生效）
         └── CandidateAsset
               · objects/（双校验通过）或 quarantine/（摘要/格式/时序不符）
               └── Adoption（adopt/replace/reject/readopt，reviewer+reason 强制）
                     └── CompositeProduct ── composite_inputs ─ 采用候选
```

## 关键语义

### 冻结与幂等

`prepare_generation()` 对当前提示词版本、参考图版本、参数做快照；相同
输入重复提交返回**同一 attempt**。`dispatch()` 的幂等键与尝试 1:1 绑定：
传输超时在同一键下重试，账本里始终只有一行派发、一个供应商任务号。

### 回调状态机

| 情形 | 行为 |
|---|---|
| 相同 `event_id` 重投 | `duplicate_event`，零副作用，物理投递留证 |
| 失败先到 | 立即终态 `failed`；后到成功留存为 `late_succeeded_after_terminal`，其文件隔离 |
| 成功先到 | 登记声明摘要，字节双校验通过后才 `succeeded` |
| 成功证据后又来失败 | `late_failure_after_success_claim`，不翻案 |
| 两封成功声明不同摘要 | 首封认领生效，第二封记 `conflicting_success_claim` |
| 字节摘要/格式不符 | 进 `quarantine/`；成功已认领则尝试落 `dead_letter` |
| 字节先于回调 | 合法字节先入候选区，回调到达时自动终态化 |

失败/死信尝试的救济是显式 `retry_terminal()`：以同一冻结输入开下一代
尝试，旧尝试保持终态不动。

### 人工审批

`adopt` 要求 reviewer+reason；已有生效采用时新候选不会自动顶替，必须
`replace_adoption`（旧候选退回候选区，历史保留）；`reject_candidate`
否决不影响他人、否决生效候选会清空镜头采用并开变更单；
`readopt_rejected` 重新采用被否决的图必须再次给出人和理由。
合成产物只接受**已采用**候选作为输入。

### 影响面与返工范围

变更全部落 `changes` 表。`Lineage.stale_work()` 从每份变更展开：

- 参考替换 → 所有绑定该参考的镜头中，冻结了旧版本参考的尝试；
- 提示词修订 → 该镜头变更前的全部尝试；
- 候选否决 / 采用替换 → 引用该候选的全部下游合成产物。

每个 (镜头, 适配器, 参数) 组合给出 `not_rerun / in_progress / rerun`
状态；合成产物在变更后被同类型新产物取代时标记已闭环。
`Lineage.trace_shot(shot_id)` 反向给出成片镜头的全部输入版本、尝试、
回调留证、候选隔离记录、审批轨迹与合成血缘。

## 接入真实供应商

实现 `GenerationAdapter` 协议并注册；把账本幂等键映射到供应商的幂等
请求头，同键重提必须返回同一任务号：

```python
from asset_hub import register_adapter, GenerationAdapter

@register_adapter("wan-video")
class WanVideoAdapter:
    name = "wan-video"
    def dispatch(self, frozen, idempotency_key: str) -> str:
        resp = http.post("/tasks", json={...}, headers={"Idempotency-Key": idempotency_key})
        return resp.json()["task_id"]
```

供应商异步回调投递到收件箱目录（`inbox/spool/`，信封或 base64 字节），
由 `python -m asset_hub.cli --root <账本根> --recover [--once]` 消费；
spool 文件处理成功后才移入 `processed/`，崩溃时未确认邮件自动重放。

## 约束（沿用基线）

Python 3.11+；时间必须带 UTC 偏移；摘要为小写十六进制 SHA-256；
真实媒体文件与访问密钥不入库不入仓。
