# 数贸会线索与合规交易

本项目保存数贸会线索与合规交易所需的领域上下文和校验契约，便于服务端功能围绕真实业务参与方展开。当前版本只提供资料读取、结构校验和命令行摘要，数据均为演示用虚构内容。

## 参与方

采购方、参展企业、会务交易团队、材料审核人员

## 事实资料

- 第五届全球数字贸易博览会在杭州举行并设置主题馆和产业展区
- 展会强调可触达、可互动和可交易
- 报道提出智能体下单、电子签名、跨境支付和争议解决的规则问题

## 跨境数字订单争议归档与分流服务

`src/dispute_archive/` 实现一套面向跨境小额数字订单争议的归档、分流与溯源服务，仅依赖标准库。

### 覆盖的业务规则

| 需求 | 实现位置与做法 |
| --- | --- |
| 接收订单事件、代理授权、签名证据、数据处理同意、支付流水、管辖约定、当事人陈述 | `events.py` 定义七类（另含运营事实、补充协议）入站事件并逐类校验 |
| 按案件发生时有效的规则版本归档 | `rules.py` 版本化注册表，按订单 `occurred_at` 选版本并固定在 `case_opened` 记录中，之后不漂移 |
| 生成可复核的争议包 | `package.build_package` 装订案件日志片段、规则全文、证据、裁定、审计，并对整包取 SHA-256 摘要 |
| 相同事件重复提交返回原案件 | `DisputeService.submit` 以 `event_id` 幂等，重复提交不写日志，返回原 `case_id` |
| 签名或支付材料互相矛盾则冻结自动分流 | `state.evaluate_routing` 检测同文件签名互斥/签署人冲突、支付已付-失败两极/多机构金额冲突、管辖约定冲突；冻结有黏性，`require_routing` 抛 `RoutingBlocked` |
| 后续补充协议只能改变尚未裁定事项，不能覆盖已使用证据 | `add_supplemental` 拒绝触碰 `decided_matters` 与裁定锁定的 `locked_evidence`；证据只追加 |
| 调解员只能查看获授权的原件 | `access_original` 要求显式授权（`grant_access`，仅当事人可授予）或目的含“调解”、范围匹配的数据处理同意 |
| 平台运营者可补充交易事实但无权改写陈述 | `add_operator_fact`（operator）与 `add_statement`（party）分离；陈述只追加，运营者无权查看原件 |
| 跨境协作记录每次数据访问和转交原因 | 每次访问写 `data_access`/`data_transfer`，原因必填；跨境转交校验同意是否覆盖接收方与最小必要范围 |
| 进入调解、诉讼或撤回后期限/通知/费用继续推进 | `transition`、`update_deadline`、`send_notice`、`update_fee` 在终态后仍可调用并全部入链 |
| 系统恢复后从裁定追溯代理行为、规则依据和完整证据链 | 哈希链日志重放恢复（`recover`）；`trace_decision` 给出 订单（代理）事件→代理授权→规则条文→每份证据哈希→裁定前完整日志轨迹 |

### 不可变事实来源

所有状态变更先写 `Journal`（SHA-256 哈希链、仅追加），内存案件只是投影。
导出为 JSONL 后，`DisputeService.recover` 重放即可完整复原；任意条目被改、被删或被调序都会让链校验抛出 `ChainIntegrityError`。

### 角色

- `party` 当事人：提交陈述、授予原件访问权
- `mediator` 调解员：仅查看获授权原件，访问必留痕
- `operator` 平台运营者：补充交易事实，不能看原件、不能改陈述
- `coordinator` 协调员：推进期限/通知/费用，发起跨境转交
- `arbitrator` 裁定人：作出裁定、可切换案件状态

### 命令行演示

```bash
python3 -m src.dispute_archive.demo_cli fixtures/dispute_scenario.json \
    --package out/package.json --log out/journal.jsonl
```

夹具含两个虚构案件：材料一致自动进入调解、签名与两家结算机构流水冲突触发冻结。
输出为端到端走查摘要（幂等、授权拒绝、跨境转交、补充协议边界、结案后费用推进、恢复后摘要一致）。

### 契约

- `contracts/context.schema.json` — 领域上下文
- `contracts/event.schema.json` — 争议入站事件

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 编译

```bash
python3 -m compileall -q src tests
```

## 命令行检查

```bash
python3 -m src.trade_expo_collaboration.context fixtures/context.json
```
