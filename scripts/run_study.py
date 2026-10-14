#!/usr/bin/env python3
"""Fixed-parameter grid comparison over non-overlapping 60-observation cycles."""
import argparse
from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import html
import json
import math
from pathlib import Path
import re
import statistics
from grid_engine import default_config, limitations, money, run, tick_down, tick_up, validate_bars


def date_key(value):
    return datetime.strptime(str(value).replace('-',''),'%Y%m%d').strftime('%Y%m%d')


def validate_symbol(symbol,kind):
    if not re.fullmatch(r'\d{6}\.(SH|SZ)',symbol):
        raise ValueError('Symbol must explicitly include .SH or .SZ')
    if kind=='stock':
        ok=(symbol.endswith('.SH') and symbol[:3] in ('600','601','603','605')) or (
            symbol.endswith('.SZ') and symbol[:3] in ('000','001','002','003','300','301'))
        if not ok:raise ValueError('Stock model only supports standard 100-share SH/SZ main-board/ChiNext A shares')
    elif kind not in ('etf-t0','etf-t1'):
        raise ValueError('ETF settlement must be explicitly confirmed')
    elif not ((symbol.endswith('.SH') and symbol[:2] in ('51','56','58')) or
              (symbol.endswith('.SZ') and symbol[:2]=='15')):
        raise ValueError('Symbol is not within supported SH/SZ ETF code families; verify the instrument')


def validate_evidence(bars,actions,metadata,symbol,as_of):
    if bars[-1]['date']>date_key(as_of):raise ValueError('Future bars after as-of are forbidden')
    if not isinstance(metadata,dict):raise ValueError('Verified symbol / unadjusted-price metadata is required')
    if metadata.get('complete') is False or metadata.get('errors') or metadata.get('unresolved_errors'):
        raise ValueError('Metadata contains incomplete data or unresolved errors')
    if metadata.get('symbol')!=symbol:raise ValueError('Metadata symbol does not match requested symbol')
    if metadata.get('price_type') not in ('unadjusted','不复权','exchange traded unadjusted daily OHLC, not NAV'):
        raise ValueError('Use unadjusted prices with explicit corporate actions')
    coverage=metadata if metadata.get('start') else metadata.get('actual',{})
    if not coverage.get('start') or not coverage.get('end'):
        raise ValueError('Metadata must state start/end coverage')
    if date_key(coverage['start'])>bars[0]['date'] or date_key(coverage['end'])<bars[-1]['date']:
        raise ValueError('Metadata date coverage does not cover input data')
    if metadata.get('data_complete_through') and date_key(metadata['data_complete_through'])<bars[-1]['date']:
        raise ValueError('Input includes an unfinished daily bar')
    if not isinstance(actions,dict) or actions.get('verified') is not True:
        raise ValueError('Corporate actions must be explicitly verified; omission is not no dividends')
    if not actions.get('checked_through') or date_key(actions['checked_through'])<bars[-1]['date']:
        raise ValueError('Corporate-action verification does not cover the data endpoint')
    sources=actions.get('source_urls')
    if not isinstance(sources,list) or not sources or not all(isinstance(u,str) and u.startswith(('http://','https://')) for u in sources):
        raise ValueError('Corporate actions require source URLs')
    if not isinstance(actions.get('events'),list) or actions.get('other_actions')!=[]:
        raise ValueError('Explicit cash events and empty other_actions are required; splits are unsupported')
    dates=set()
    for event in actions['events']:
        if not isinstance(event,dict) or set(event)!={'date','cash_per_share'}:
            raise ValueError('Only date / cash_per_share events are supported')
        d=date_key(event['date'])
        if d!=event['date'] or d in dates:raise ValueError('Use unique YYYYMMDD dividend dates')
        dates.add(d)
        amount=event['cash_per_share']
        if not isinstance(amount,(int,float)) or not math.isfinite(amount) or amount<0:
            raise ValueError('Cash dividend must be nonnegative and finite')


def cycle_stats(pnls):
    if len(pnls)<2:raise ValueError('Sample variance requires at least two cycles')
    mean=statistics.mean(pnls);variance=statistics.variance(pnls)
    return dict(cycles=len(pnls),mean_pnl=mean,variance_pnl=variance,std_pnl=math.sqrt(variance),
                worst_pnl=min(pnls),best_pnl=max(pnls),positive_cycles=sum(p>0 for p in pnls))


def evaluate(cycles,cfg,policy,events):
    paths={}
    for path in ('OHLC','OLHC'):
        runs=[run(b,cfg,path,policy,events) for b in cycles]
        s=cycle_stats([r['metrics']['pnl'] for r in runs])
        s.update(worst_max_drawdown_pct=max(r['metrics']['max_drawdown_pct'] for r in runs),
                 stopped_cycles=sum(r['metrics']['stopped'] for r in runs),
                 unliquidated_cycles=sum(not r['metrics']['fully_liquidated'] for r in runs))
        paths[path]=dict(summary=s,runs=runs)
    return paths


def configuration(cfg,price,holdings,selected):
    tick=.01 if cfg.kind=='stock' else .001
    if not math.isfinite(price) or price<=0:raise ValueError('Reference price must be positive')
    if abs(price/tick-round(price/tick))>1e-6:raise ValueError('Reference price violates asset price tick')
    if holdings is not None and (holdings<0 or not isinstance(holdings,int)):
        raise ValueError('Holdings must be a nonnegative integer')
    def quantity(f):return math.floor(cfg.budget*f/price/100)*100
    buy_price=tick_up(price+cfg.slippage,tick)
    notional=money(100*buy_price)
    minimum=money(notional+money(max(cfg.min_fee,notional*cfg.fee_rate))+
                  (money(notional*cfg.transfer_rate) if cfg.kind=='stock' else 0))
    def buy_cost(n):
        value=money(n*buy_price)
        return money(value+money(max(cfg.min_fee,value*cfg.fee_rate))+
                     (money(value*cfg.transfer_rate) if cfg.kind=='stock' else 0)) if n else 0
    initial=quantity(cfg.initial_fraction);floor=quantity(cfg.min_fraction);cap=quantity(cfg.max_fraction)
    while initial and buy_cost(initial)>cfg.budget:initial-=100
    executable=cfg.budget>=minimum and initial>=100 and cap>floor
    issues=[]
    if cfg.budget<minimum:issues.append('当前预算买不起含费用的一手')
    if initial<100:issues.append('当前初始仓位比例不足一手；不能把全零交易排成最优')
    if cap<=floor:issues.append('当前固定持仓上下限没有可交易空间')
    outside_range=holdings is not None and (holdings<floor or holdings>cap)
    if outside_range:issues.append('已有持仓超出模拟仓位范围，不能自动照搬')
    lot=selected['lot'] if selected else None
    position=initial if holdings is None else holdings
    cash_estimate=cfg.budget-buy_cost(initial) if holdings is None else max(0,cfg.budget-holdings*price)
    can_initial_sell=lot is not None and position-floor>=lot
    budget_can_buy=lot is not None and cap-position>=lot and cash_estimate>=buy_cost(lot)
    model_sell=lot is not None and initial-floor>=lot
    model_buy=lot is not None and cap-initial>=lot and cfg.budget-buy_cost(initial)>=buy_cost(lot)
    if lot and not (model_sell or model_buy):
        executable=False;issues.append('模拟初仓与预算无法执行该候选的第一笔网格买卖')
    return dict(reference_price=price,price_tick=tick,budget=cfg.budget,
        lower_price=tick_up(price*cfg.lower_fraction,tick),upper_price=tick_down(price*cfg.upper_fraction,tick),
        simulated_initial_quantity=initial,min_quantity=floor,max_quantity=cap,
        minimum_cash_for_one_lot=minimum,existing_holdings=holdings,
        buy_to_target_quantity=None,model_executable=executable,
        live_start_ready=False if outside_range else None,enable_recommended=False,issues=issues,
        grid_pct=selected['grid_pct'] if selected else None,quantity_per_trade=lot,
        can_initial_grid_sell=can_initial_sell if holdings is None or not can_initial_sell else None,
        can_initial_grid_buy=budget_can_buy if holdings is None else None,
        position_allows_grid_sell=can_initial_sell,actual_available_quantity=None,
        budget_estimate_can_grid_buy=budget_can_buy,budget_cash_estimate=money(cash_estimate),
        position_used_for_configuration=position,actual_available_cash=None,
        cash_estimate_note='仅核对模拟资金可行性；按预算减持仓市值粗算不是账户余额，总持仓也不等于可卖数量。',
        holdings_instruction='模拟初仓不是加仓指令；已有持仓与可用资金需要启动前核对。',
        t1_start_boundary='新买股票/ETF T+1份额次交易日才可卖；当日可卖旧仓。' if cfg.kind!='etf-t0' else '该产品已明确按T+0模拟。',
        platform_capabilities=dict(max_position='unknown',equity_stop='unknown',cloud_monitoring='unknown'),
        platform_note='APP缺少权益止损或最大持仓时不能完整复现；未核实平台功能前不建议直接启用。')


def study(bars,cfg,as_of,actions,*,metadata=None,symbol='000792.SZ',name='',
          grids=None,lots=None,min_cycles=4,holdings=None,reference_price=None):
    validate_symbol(symbol,cfg.kind)
    validate_bars(bars,.01 if cfg.kind=='stock' else .001)
    validate_evidence(bars,actions,metadata,symbol,as_of)
    end=datetime.strptime(date_key(as_of),'%Y%m%d').date()
    try:cutoff=end.replace(year=end.year-1)
    except ValueError:cutoff=end.replace(year=end.year-1,day=28)
    annual=[b for b in bars if cutoff.strftime('%Y%m%d')<b['date']<=date_key(as_of)]
    if min_cycles<2:raise ValueError('min-cycles cannot be below 2 for sample variance')
    if len(annual)//60<min_cycles:raise ValueError(f'Need at least {min_cycles} complete 60-observation cycles, got {len(annual)//60}')
    remainder=len(annual)%60
    cycles=[annual[i:i+60] for i in range(remainder,len(annual),60)]
    price=reference_price if reference_price is not None else annual[-1]['close']
    config=configuration(cfg,price,holdings,None)
    config.update(reference_source='user_input' if reference_price is not None else 'latest_historical_close',
                  reference_date=None if reference_price is not None else annual[-1]['date'],
                  reference_time_status='unknown-time; not verified live' if reference_price is not None else 'historical close; not live')
    scope=dict(start=annual[0]['date'],end=annual[-1]['date'],as_of=end.isoformat(),
               cutoff_exclusive=cutoff.isoformat(),annual_trading_days=len(annual),used_days=60*len(cycles),
               discarded_earliest_days=remainder,cycle_observations=60)
    result=dict(symbol=symbol,name=name,kind=cfg.kind,config=asdict(cfg),scope=scope,
        cycles=[dict(start=b[0]['date'],end=b[-1]['date'],observations=len(b)) for b in cycles],
        actions=actions,metadata=metadata,ranking=[],selected=None,configuration=config,
        status='not_executable',limitations=limitations(cfg)+[
            '每套参数固定用于自然年内所有60观察值轮次，首日收盘到末日收盘约59个收益间隔。',
            '统计每轮结束总盈亏的均值及样本方差（n-1）；方差单位元²，标准差单位元。',
            '各轮独立预算，不拼成复利曲线。四轮既用于比较又用于筛选，属于样本内局部候选，不证明未来最优。',
            '先筛Pareto候选，再以两路径较低均值/较高标准差排序；这一偏好并非唯一正确。',
            '报告截止日由调用者指定，应为上一完整交易日；数据日期不能超过as-of。',
        ])
    if not config['model_executable']:return result
    grids=grids if grids is not None else ([.005,.008,.01,.012,.015,.02,.025,.03,.04,.05] if cfg.kind=='stock' else [.004,.005,.006,.008,.01,.012,.015])
    lots=lots if lots is not None else [100,200]
    if not grids or not lots or any(not math.isfinite(g) or not 0<g<1 for g in grids) or any(q<=0 or q%100 for q in lots):
        raise ValueError('Invalid nonempty grid/lot candidate set')
    rows=[]
    for g in sorted(set(grids)):
        for q in sorted(set(lots)):
            candidate=replace(cfg,grid=g,lot=q)
            paths=evaluate(cycles,candidate,'grid',actions['events'])
            mean=min(p['summary']['mean_pnl'] for p in paths.values())
            var=max(p['summary']['variance_pnl'] for p in paths.values())
            capable=configuration(candidate,price,holdings,dict(grid_pct=g*100,lot=q))['model_executable']
            history_executable=all(r['initial_quantity']>0 and r['max_quantity']-r['min_quantity']>=q
                                   and not r['metrics']['entry_blocked'] for p in paths.values() for r in p['runs'])
            runnable=capable and history_executable
            rows.append(dict(grid_pct=g*100,lot=q,paths=paths,robust_mean_pnl=mean,
                robust_variance_pnl=var,robust_std_pnl=math.sqrt(var),
                score=mean/math.sqrt(var) if var else 0,executable=runnable,pareto=False))
    for r in rows:
        r['pareto']=r['executable'] and not any(q['executable'] and
            q['robust_mean_pnl']>=r['robust_mean_pnl'] and q['robust_variance_pnl']<=r['robust_variance_pnl'] and
            (q['robust_mean_pnl']>r['robust_mean_pnl'] or q['robust_variance_pnl']<r['robust_variance_pnl']) for q in rows)
    rows.sort(key=lambda r:(r['pareto'],r['executable'],r['score'],r['robust_mean_pnl']),reverse=True)
    selected=next((r for r in rows if r['pareto']),None)
    positive=selected is not None and selected['robust_mean_pnl']>0
    result.update(ranking=rows,selected=None if selected is None else dict(grid_pct=selected['grid_pct'],lot=selected['lot']),
        status='research_candidate' if positive else 'comparison_only' if selected else 'not_executable',
        all_candidates_nonpositive=not any(r['executable'] and r['robust_mean_pnl']>0 for r in rows),
        baselines={p:evaluate(cycles,cfg,p,actions['events']) for p in ('hold','hold_stop','cash')})
    result['configuration']=configuration(cfg,price,holdings,result['selected'])
    result['configuration'].update(reference_source=config['reference_source'],reference_date=config['reference_date'],
                                   reference_time_status=config['reference_time_status'])
    result['configuration']['historical_candidate_positive']=positive
    if selected is None:
        result['configuration']['model_executable']=False
        result['configuration']['issues'].append('没有同时满足历史轮次和当前份额约束的候选')
    if selected:
        result['selected_stress']=evaluate(cycles,replace(cfg,grid=selected['grid_pct']/100,lot=selected['lot'],
            slippage=cfg.slippage*3),'grid',actions['events'])
    return result


def render_report(result):
    esc=lambda x:html.escape(str(x))
    scope=result['scope'];c=result['configuration']
    heading=f"{result['name']} {result['symbol']} · 固定参数网格研究"
    winner=(f"本次候选：涨跌 {c['grid_pct']:g}%，每笔 {c['quantity_per_trade']} 股/份。"
            if result.get('selected') else '当前约束下没有可用的网格候选。')
    status={'not_executable':'当前约束下不可执行','comparison_only':'候选无正收益优势；现金基准更好或不差','research_candidate':'样本内局部候选；尚未验证未来表现'}[result['status']]
    rows=''.join('<tr>'+''.join(f'<td>{esc(v)}</td>' for v in [f"{r['grid_pct']:g}%",r['lot'],f"{r['robust_mean_pnl']:.2f}",f"{r['robust_variance_pnl']:.2f}",f"{r['robust_std_pnl']:.2f}",'是' if r['pareto'] else '否','是' if r['executable'] else '否'])+'</tr>' for r in result['ranking'])
    detail=''
    for r in result['ranking']:
        if result['selected']!={'grid_pct':r['grid_pct'],'lot':r['lot']}:continue
        detail='<h2>该候选每轮总盈亏</h2><table><tr><th>轮次</th><th>日期</th><th>OHLC</th><th>OLHC</th></tr>'
        for i,w in enumerate(result['cycles']):
            vals=[r['paths'][p]['runs'][i]['metrics'] for p in ('OHLC','OLHC')]
            detail+=f'<tr><td>{i+1}</td><td>{w["start"]}—{w["end"]}</td>'+''.join(f'<td>{m["pnl"]:.2f} 元'+('（未清仓）' if not m['fully_liquidated'] else '')+'</td>' for m in vals)+'</tr>'
        detail+='</table>'
    baseline=''
    if result.get('baselines'):
        baseline='<h2>相同预算与初始仓位的对照</h2><table><tr><th>对照</th><th>OHLC均值</th><th>OLHC均值</th></tr>'
        for key,label in [('hold','持有不动'),('hold_stop','持有＋权益止损'),('cash','现金')]:
            baseline+=f'<tr><td>{label}</td>'+''.join(f'<td>{result["baselines"][key][p]["summary"]["mean_pnl"]:.2f} 元</td>' for p in ('OHLC','OLHC'))+'</tr>'
        baseline+='</table>'
    fees=result['config']
    stress=''
    if result.get('selected_stress'):
        stress='<h2>固定候选的三倍滑点检查</h2><p>'+ '；'.join(
            f'{p} 每轮均值 {v["summary"]["mean_pnl"]:.2f} 元，标准差 {v["summary"]["std_pnl"]:.2f} 元'
            for p,v in result['selected_stress'].items())+'。成交价更新基准会改变路径，收益不保证随滑点单调下降。</p>'
    sources=' '.join(f'<a href="{esc(u)}">公司行动来源 {i+1}</a>' for i,u in enumerate(result['actions']['source_urls']))
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(heading)}</title>
<style>body{{font:16px/1.65 system-ui,sans-serif;color:#17202a;background:#f5f5f2;max-width:1100px;margin:40px auto;padding:0 24px}}table{{border-collapse:collapse;width:100%;background:white;margin:16px 0}}th,td{{padding:10px;border-bottom:1px solid #ddd;text-align:right}}th:first-child,td:first-child{{text-align:left}}.notice{{padding:18px;border-left:4px solid #9b6d27;background:#fff6df}}code{{overflow-wrap:anywhere}}h2{{margin-top:32px}}</style>
<h1>{esc(heading)}</h1><p class="notice"><strong>{esc(winner)}</strong><br>{esc(status)}。这是样本内历史比较，不是实盘回测或下一轮收益承诺。均值是每60日轮次的总盈亏，绝非每格利润。</p>
<p>数据 {scope['start']}—{scope['end']}；研究截至 {scope['as_of']}，自然年截点 {scope['cutoff_exclusive']}（不含）。使用 {scope['used_days']} 日，舍弃最早 {scope['discarded_earliest_days']} 日。预算 {fees['budget']:g} 元。</p>
<h2>候选比较</h2><p>较低路径均值、较高路径方差分别汇总；两条人工路径不构成真实结果上下界。</p><table><tr><th>间距</th><th>每笔份额</th><th>均值/元</th><th>样本方差/元²</th><th>标准差/元</th><th>Pareto</th><th>可模拟</th></tr>{rows}</table>{detail}{baseline}{stress}
<h2>参数填写参考</h2><p>{esc(winner)}基准 {c['reference_price']:g}（{esc(c['reference_source'])}，日期 {esc(c['reference_date'])}，{esc(c['reference_time_status'])}），区间 {c['lower_price']:g}—{c['upper_price']:g}；模拟初仓 {c['simulated_initial_quantity']} 股/份，底仓 {c['min_quantity']} 股/份，上限 {c['max_quantity']} 股/份。含费用一手至少需要 {c['minimum_cash_for_one_lot']:.2f} 元。</p><p>{esc(c['holdings_instruction'])} {esc(c['cash_estimate_note'])} {esc(c['t1_start_boundary'])} {esc(c['platform_note'])}</p><p>{esc('；'.join(c['issues']))}</p>
<p>单边佣金 {fees['fee_rate']:g}，最低 {fees['min_fee']:g} 元，单边滑点每份 {fees['slippage']:g} 元。股票另收卖出印花税 {fees['stamp_rate']:g} 和双边过户费 {fees['transfer_rate']:g}；ETF不收这两项。</p>
<h2>约束与解释</h2><ul>{''.join('<li>'+esc(x)+'</li>' for x in result['limitations'])}</ul><p>{sources}</p><p>行情SHA-256：<code>{esc(result.get('data_sha256','not supplied by API caller'))}</code></p></html>'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('symbol','kind','data','as-of','actions','metadata','out'):parser.add_argument('--'+key,required=True)
    parser.add_argument('--name',default='')
    for key in ('budget','initial-fraction','min-fraction','max-fraction','fee-rate','min-fee','slippage','stop-fraction','reference-price'):
        parser.add_argument('--'+key,type=float)
    parser.add_argument('--holdings',type=int);parser.add_argument('--min-cycles',type=int,default=4)
    parser.add_argument('--grids');parser.add_argument('--lots',default='100,200')
    args=parser.parse_args()
    overrides={k:getattr(args,k) for k in ('budget','initial_fraction','min_fraction','max_fraction','fee_rate','min_fee','slippage','stop_fraction') if getattr(args,k) is not None}
    try:
        raw=Path(args.data).read_bytes();bars=json.loads(raw)
        result=study(bars,default_config(args.kind,**overrides),args.as_of,json.loads(Path(args.actions).read_text()),
            metadata=json.loads(Path(args.metadata).read_text()),symbol=args.symbol.upper(),name=args.name,
            grids=[float(x)/100 for x in args.grids.split(',')] if args.grids else None,
            lots=[int(x) for x in args.lots.split(',')],min_cycles=args.min_cycles,
            holdings=args.holdings,reference_price=args.reference_price)
        result['data_sha256']=hashlib.sha256(raw).hexdigest()
        out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
        (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        (out/'configuration.json').write_text(json.dumps(result['configuration'],ensure_ascii=False,indent=2),encoding='utf-8')
        (out/'report.html').write_text(render_report(result),encoding='utf-8')
        print(json.dumps(dict(status=result['status'],selected=result['selected'],report=str(out/'report.html')),ensure_ascii=False))
    except (ValueError,KeyError,TypeError,OSError) as exc:parser.exit(2,f'Research input rejected: {exc}\n')


if __name__=='__main__':main()
