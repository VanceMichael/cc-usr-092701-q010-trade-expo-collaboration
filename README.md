# 数贸会线索与合规交易

本项目保存数贸会线索与合规交易所需的领域上下文和校验契约，便于服务端功能围绕真实业务参与方展开。当前版本只提供资料读取、结构校验和命令行摘要，数据均为演示用虚构内容。

## 参与方

采购方、参展企业、会务交易团队、材料审核人员

## 事实资料

- 第五届全球数字贸易博览会在杭州举行并设置主题馆和产业展区
- 展会强调可触达、可互动和可交易
- 报道提出智能体下单、电子签名、跨境支付和争议解决的规则问题

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
