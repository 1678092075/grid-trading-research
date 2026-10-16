# 网格参数研究

一个用于 Codex 的网格交易研究 skill。输入沪深股票或场内 ETF 代码，查询行情、比较固定参数，并生成配置参考。它不执行证券交易。

## 研究口径

- 最近一个自然年的行情用于识别价格背景，先固定震荡阶段，再比较参数；不按回测收益反选起点。
- 同一阶段连续运行：只建仓一次，现金、持仓、可卖库存与基准连续承接，统计窗口边界不清仓、不重置。
- 按阶段持续时间及价格往返频率确定统计窗口，保留尾段，比较总盈亏、窗口均值、样本方差、回撤与持有对照。样本不足时不强给最优。
- 根据预算、股价和初始仓位空间生成每笔数量候选，先粗搜，再局部细搜并核对邻域与边界。显式传入的 `--lots`、`--grids` 不自动扩展。
- 明确区分 A 股 T+1、ETF T+0/T+1、佣金、印花税、过户费及滑点；ETF 交易制度须先核实。
- 排名保留收益与方差的权衡，同时展示每笔金额、预算占比、初始可买卖档数和搜索覆盖，不把总收益最高直接当作最优。
- 结果是候选范围中的样本内研究，不将日线人工路径称为真实成交或未来收益保证。

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

本仓库包含 49 项 Python 测试和 12 项 Node 测试，覆盖资金记账、结算、连续状态、统计分段、候选搜索、显式参数、报价、止损及行情校验。
