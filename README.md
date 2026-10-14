# 网格参数研究

一个用于 Codex 的网格交易研究 skill。输入沪深股票或场内 ETF 代码，查询行情、比较固定参数，并生成配置参考。它不执行证券交易。

## 研究口径

- 最近一个自然年，按不重叠的 60 个交易日观察值分轮，默认至少四轮。
- 同一组参数用于全部轮次，统计账户总盈亏的均值、样本方差、标准差、最差轮次及回撤。
- 明确区分 A 股 T+1、ETF T+0/T+1、佣金、印花税、过户费及滑点。
- 输出有限候选中的样本内最优，不将日线假设路径称为实盘收益或未来保证。
- 默认研究预算和费率可由用户覆盖；实际持仓及券商功能另行核验。

## 安装

```bash
git clone https://github.com/1678092075/grid-trading-research.git
agentbuddy skill add ./grid-trading-research -g --copy -a codex -y
```

行情查询依赖已配置认证的 `ifind-finance-data` skill。不要将其密钥或认证文件提交到本仓库。回测只依赖 Python 标准库，取数脚本使用 Node.js。

## 使用

在 Codex 中输入：

> 用 $grid-trading-research 研究 000792 的网格参数，预算 2 万元。

完整工作流及输入证据要求见 [SKILL.md](SKILL.md)，交易模型边界见 [数据与执行说明](references/data-and-execution.md)。

脚本会生成 `results.json`、`configuration.json` 和 `report.html`。行情、结果与账户资料不随本仓库发布。

## 验证

```bash
python3 -B -m unittest discover -s scripts -p 'test_*.py' -q
node scripts/test_fetch.cjs
```

本仓库包含 26 项 Python 测试和 12 项 Node 测试，覆盖资金记账、结算、报价、止损、数据身份和截断检查。
