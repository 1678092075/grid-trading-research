#!/usr/bin/env python3
"""Regime-conditioned continuous grid research with statistics-only windows."""
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
from regime_windows import validate_regime, derive_windows, summarize_equity
from search_candidates import lot_space, grid_neighbors, lot_neighbors, immediate_neighbors
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
    if metadata.get('complete') is False or any(not isinstance(e,dict) or e.get('resolved') is not True for e in metadata.get('errors',[])) or metadata.get('unresolved_errors'):
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


def evaluate(stage,cfg,policy,events,plan):
    paths={}
    for path in ('OHLC','OLHC'):
        trajectory=run(stage,cfg,path,policy,events,liquidate_end=False)
        summary=summarize_equity(trajectory['daily'],cfg.budget,plan)
        summary.update(max_drawdown_pct=trajectory['metrics']['max_drawdown_pct'],
                       stopped=trajectory['metrics']['stopped'])
        paths[path]=dict(run=trajectory,summary=summary)
    return paths


def study(bars,cfg,as_of,actions,*,metadata=None,symbol='000792.SZ',name='',
          regime=None,window_days=None,grids=None,lots=None,holdings=None,reference_price=None):
    validate_symbol(symbol,cfg.kind)
    validate_bars(bars,.01 if cfg.kind=='stock' else .001)
    validate_evidence(bars,actions,metadata,symbol,as_of)
    end=datetime.strptime(date_key(as_of),'%Y%m%d').date()
    try:cutoff=end.replace(year=end.year-1)
    except ValueError:cutoff=end.replace(year=end.year-1,day=28)
    background=[b for b in bars if cutoff.strftime('%Y%m%d')<b['date']<=date_key(as_of)]
    stage,info=validate_regime(background,regime,as_of)
    plan=derive_windows(stage,override=window_days,actions=actions['events'])
    price=reference_price if reference_price is not None else stage[-1]['close']
    config=configuration(cfg,price,holdings,None)
    reference=dict(reference_source='user_input' if reference_price is not None else 'latest_historical_close',
                   reference_date=None if reference_price is not None else stage[-1]['date'],
                   reference_time_status='unknown-time; not verified live' if reference_price is not None else 'historical close; not live')
    config.update(reference)
    scope=dict(start=stage[0]['date'],end=stage[-1]['date'],as_of=end.isoformat(),
               background_start=background[0]['date'],background_observations=len(background),
               used_days=len(stage),discarded_days=0,window_days=plan['window_days'],
               complete_windows=len(plan['complete_windows']),tail_observations=plan['tail_window']['observations'] if plan['tail_window'] else 0)
    result=dict(symbol=symbol,name=name,kind=cfg.kind,config=asdict(cfg),scope=scope,
                evaluation_mode='continuous_mark_to_market',regime=info,window_plan=plan,
                actions=actions,metadata=metadata,ranking=[],selected=None,configuration=config,
                status='not_executable',limitations=limitations(cfg,liquidate_end=False)+[
                    '一年行情只作阶段判断背景；震荡起点由价格证据明确指定，不搜索收益最大的起点。',
                    '一次建仓后持仓、现金、基准与止损状态连续承接；统计边界不交易，末日按市值估值而非强制清仓。',
                    '统计窗口依据回看价格往返频率与阶段长度的启发式选择，不是经过验证的最优统计周期。',
                    '完整等长统计窗的盈亏由连续净值端点作差，样本方差用n-1；尾段单列但计入总盈亏。',
                    '少于两个完整统计窗不计算跨窗方差，也不强给最优参数；窗口不是独立重复实验。',
                    '阶段选择、频率估计和参数比较均使用历史数据，属于回看样本内分析，不能声称当时已识别横盘。',
                    '排名先筛窗口均值/方差Pareto，再按较低均值与较高标准差辅助排序；同时要求整段总盈亏为正。',
                    '初仓、底仓和预算上限是显式研究条件，不是本次优化结果。',
                ])
    if not config['model_executable']:return result
    if not info['can_optimize']:
        result['status']='regime_uncertain';return result
    if len(stage)<2:
        result['status']='insufficient_evidence';return result
    result['baselines']={p:evaluate(stage,cfg,p,actions['events'],plan) for p in ('hold','hold_stop','cash')}
    if not plan['frequency_estimate_sufficient'] or len(plan['complete_windows'])<2:
        result['status']='insufficient_evidence';return result
    auto_grid=grids is None;auto_lot=lots is None
    grids=grids if grids is not None else ([.005,.008,.01,.012,.015,.02,.025,.03,.04,.05] if cfg.kind=='stock' else [.004,.005,.006,.008,.01,.012,.015])
    space=lot_space(cfg,stage[0]['close'])
    lots=space['coarse_lots'] if auto_lot else lots
    if not grids or any(not math.isfinite(g) or not 0<g<1 for g in grids) or (not auto_lot and not lots) or any(not isinstance(q,int) or q<=0 or q%100 for q in lots):
        raise ValueError('Invalid grid/lot candidate set')
    grids=sorted(set(grids));lots=sorted(set(lots))
    search=dict(auto_lots=auto_lot,auto_grids=auto_grid,position_space=space,
                coarse_grids=grids,coarse_lots=lots,grid_step_pct=.1,
                explicit_lots=None if auto_lot else lots,explicit_grids=None if auto_grid else grids,
                lot_refinement_radius=1200,lot_step=100,refinement_seeds=[],
                coverage='bounded coarse and local search; not an exhaustive global optimum')
    result['search']=search
    evaluated={}
    def add(g,q):
        g=round(g,9);key=(g,q)
        if key in evaluated:return
        candidate=replace(cfg,grid=g,lot=q)
        paths=evaluate(stage,candidate,'grid',actions['events'],plan)
        mean=min(p['summary']['mean_pnl'] for p in paths.values())
        var=max(p['summary']['variance_pnl'] for p in paths.values())
        total=min(p['run']['metrics']['pnl'] for p in paths.values())
        capable=configuration(candidate,price,holdings,dict(grid_pct=g*100,lot=q))['model_executable']
        history_ok=all(p['run']['initial_quantity']>0 and p['run']['max_quantity']-p['run']['min_quantity']>=q
                       and not p['run']['metrics']['entry_blocked'] for p in paths.values())
        evaluated[key]=dict(grid_pct=round(g*100,6),lot=q,paths=paths,robust_mean_pnl=mean,
            robust_total_pnl=total,robust_variance_pnl=var,robust_std_pnl=math.sqrt(var),
            score=mean/math.sqrt(var) if var else 0,executable=capable and history_ok,pareto=False,
            reference_trade_value=money(q*price),trade_budget_pct=q*price/cfg.budget*100,
            historical_initial_buy_slots=space['initial_buy_space']//q,
            historical_initial_sell_slots=space['initial_sell_space']//q)
    def ranked():
        rows=list(evaluated.values())
        for r in rows:
            r['pareto']=r['executable'] and not any(q['executable'] and
                q['robust_mean_pnl']>=r['robust_mean_pnl'] and q['robust_variance_pnl']<=r['robust_variance_pnl'] and
                (q['robust_mean_pnl']>r['robust_mean_pnl'] or q['robust_variance_pnl']<r['robust_variance_pnl']) for q in rows)
        return sorted(rows,key=lambda r:(r['pareto'],r['executable'],r['score'],r['robust_total_pnl']),reverse=True)
    for g in grids:
        for q in lots:add(g,q)
    coarse=ranked();search['coarse_evaluations']=len(evaluated)
    for seed in [r for r in coarse if r['pareto']][:3]:
        gs=grid_neighbors(seed['grid_pct']/100,grids) if auto_grid else [seed['grid_pct']/100]
        qs=lot_neighbors(seed['lot'],space) if auto_lot else [seed['lot']]
        search['refinement_seeds'].append(dict(grid_pct=seed['grid_pct'],lot=seed['lot'],
            grid_pct_tested=[round(g*100,6) for g in gs],lots_tested=qs))
        for g in gs:
            for q in qs:add(g,q)
    rows=ranked();rounds=0
    for _ in range(4):
        leader=next((r for r in rows if r['pareto']),None)
        if leader is None:break
        pairs=immediate_neighbors(leader['grid_pct']/100,leader['lot'],grids,space,auto_grid,auto_lot)
        missing=[(g,q) for g,q in pairs if (round(g,9),q) not in evaluated]
        if not missing:break
        for g,q in missing:add(g,q)
        rounds+=1;rows=ranked()
    leader=next((r for r in rows if r['pareto']),None)
    search.update(evaluated_pairs=len(evaluated),neighbor_rounds=rounds,
                  evaluated_lots=sorted({q for g,q in evaluated}),
                  evaluated_grid_pct=sorted({round(g*100,6) for g,q in evaluated}),
                  neighbor_complete=False,boundary=None)
    if leader:
        g=leader['grid_pct']/100;q=leader['lot']
        pairs=immediate_neighbors(g,q,grids,space,auto_grid,auto_lot)
        search['neighbor_complete']=all((round(x,9),y) in evaluated for x,y in pairs)
        search['final_neighbors']=[dict(grid_pct=round(x*100,6),lot=y,
            evaluated=(round(x,9),y) in evaluated) for x,y in pairs]
        search['boundary']=dict(lot_at_position_lower=q==100,lot_at_position_upper=q==space['maximum_lot'],
            lot_at_search_lower=q==min(search['evaluated_lots']),
            lot_at_search_upper=q==max(search['evaluated_lots']),
            grid_at_declared_edge=abs(g-min(grids))<1e-9 or abs(g-max(grids))<1e-9,
            interpretation='Position bounds, explicit search limits and incomplete neighborhoods are distinct; none proves a future or global optimum.')
    selected=next((r for r in rows if r['pareto']),None)
    positive=selected is not None and selected['robust_mean_pnl']>0 and selected['robust_total_pnl']>0
    result.update(ranking=rows,selected=None if selected is None else dict(grid_pct=selected['grid_pct'],lot=selected['lot']),
                  status='research_candidate' if positive else 'comparison_only' if selected else 'not_executable',
                  all_candidates_nonpositive=not any(r['executable'] and r['robust_total_pnl']>0 for r in rows))
    result['configuration']=configuration(cfg,price,holdings,result['selected'])
    result['configuration'].update(reference,historical_candidate_positive=positive)
    if selected is None:
        result['configuration']['model_executable']=False
        result['configuration']['issues'].append('没有同时满足历史与当前模拟约束的候选')
        return result
    selected_cfg=replace(cfg,grid=selected['grid_pct']/100,lot=selected['lot'])
    result['selected_stress']=evaluate(stage,replace(selected_cfg,slippage=cfg.slippage*3),'grid',actions['events'],plan)
    # Reporting sensitivity reuses the identical equity path, not new trades.
    w=plan['window_days'];delta=max(1,round(w*.2))
    result['window_sensitivity']=[]
    for alternative in sorted({max(1,w-delta),w+delta}-{w}):
        other=derive_windows(stage,override=alternative,actions=actions['events'])
        stats={p:summarize_equity(x['run']['daily'],cfg.budget,other) for p,x in selected['paths'].items()}
        result['window_sensitivity'].append(dict(window_days=alternative,same_trades=True,paths=stats))
    # Start perturbations are diagnostics of the already chosen candidate, never
    # a search over entry dates. Do not call them independent/out-of-sample tests.
    start_index=next(i for i,b in enumerate(background) if b['date']==stage[0]['date'])
    shift=max(1,w//2);result['start_sensitivity']=[]
    for offset in (-shift,shift):
        index=start_index+offset
        if index<0 or index>=len(background)-1:continue
        alternative=background[index:]
        runs={p:run(alternative,selected_cfg,p,'grid',actions['events'],liquidate_end=False) for p in ('OHLC','OLHC')}
        result['start_sensitivity'].append(dict(start=alternative[0]['date'],shift_observations=offset,
            interpretation='固定主候选的起点扰动诊断；不证明该日期属于横盘，不用于重新选择主起点。',
            paths={p:r['metrics'] for p,r in runs.items()}))
    return result


def render_report(result):
    esc=lambda x:html.escape(str(x))
    num=lambda x:'样本不足' if x is None else f'{x:,.2f}'
    s=result['scope'];c=result['configuration'];plan=result['window_plan'];stage=result['regime']
    status={'not_executable':'预算/份额约束下不可模拟','regime_uncertain':'震荡阶段尚不明确，不强给最优',
            'insufficient_evidence':'阶段或统计样本不足，不强给最优',
            'comparison_only':'候选未同时形成正的统计窗均值与整段盈亏',
            'research_candidate':'该震荡阶段中的样本内候选，尚未验证未来'}[result['status']]
    winner=(f"涨跌{c['grid_pct']:g}%，每笔{c['quantity_per_trade']}股/份" if result['selected'] else '暂无可用最优候选')
    rows=''.join('<tr>'+''.join(f'<td>{esc(v)}</td>' for v in [f"{r['grid_pct']:g}% / {r['lot']}",
        num(r.get('reference_trade_value')),num(r.get('trade_budget_pct')),
        str(r.get('historical_initial_buy_slots','—'))+'/'+str(r.get('historical_initial_sell_slots','—')),
        num(r['robust_total_pnl']),num(r['robust_mean_pnl']),num(r['robust_variance_pnl']),num(r['robust_std_pnl']),
        num(max(p['run']['metrics']['max_drawdown_pct'] for p in r['paths'].values())),
        '是' if r['pareto'] else '否'])+'</tr>' for r in result['ranking'])
    detail='';sensitivity=''
    chosen=next((r for r in result['ranking'] if {k:r[k] for k in ('grid_pct','lot')}==result['selected']),None)
    if chosen:
        a,b=(chosen['paths'][p] for p in ('OHLC','OLHC'))
        detail='<h2>同一条连续净值的分段统计</h2><table><tr><th>区间</th><th>OHLC盈亏/元</th><th>OLHC盈亏/元</th></tr>'
        left=a['summary']['windows']+([a['summary']['tail_window']] if a['summary']['tail_window'] else [])
        right=b['summary']['windows']+([b['summary']['tail_window']] if b['summary']['tail_window'] else [])
        for i,(x,y) in enumerate(zip(left,right)):
            tail='（不足整窗的尾段）' if i>=a['summary']['complete_count'] else ''
            detail+=f"<tr><td>{x['start_date']}—{x['end_date']}{tail}</td><td>{num(x['pnl'])}</td><td>{num(y['pnl'])}</td></tr>"
        detail+='</table><p>各段与尾段盈亏相加等于整段总盈亏；只建仓一次，边界不清仓、不补回初始仓、不重置基准。</p>'
        detail+='<p>'+ '；'.join(f"{p}：总盈亏{num(x['run']['metrics']['pnl'])}元，最大回撤{num(x['run']['metrics']['max_drawdown_pct'])}%，期末持仓{x['run']['metrics']['ending_quantity']}股/份" for p,x in chosen['paths'].items())+'</p>'
        sensitivity='<h2>稳定性诊断（不用于重选起点）</h2><ul>'
        for x in result.get('window_sensitivity',[]):
            sensitivity+=f"<li>统计窗{x['window_days']}日："+'；'.join(f"{p}完整窗均值{num(v['mean_pnl'])}元、标准差{num(v['std_pnl'])}元、总盈亏{num(v['total_pnl'])}元" for p,v in x['paths'].items())+'；成交记录完全相同。</li>'
        for x in result.get('start_sensitivity',[]):
            sensitivity+=f"<li>起点扰动至{x['start']}："+'；'.join(f"{p}总盈亏{num(v['pnl'])}元" for p,v in x['paths'].items())+'；这是另一入场情景，不是独立样本。</li>'
        sensitivity+='</ul>'
    baseline=''
    if result.get('baselines'):
        baseline='<h2>连续持有对照</h2><table><tr><th>策略</th><th>OHLC总盈亏</th><th>OLHC总盈亏</th></tr>'
        for p,label in [('hold','持有不动'),('hold_stop','持有＋同本金止损'),('cash','现金')]:
            baseline+=f'<tr><td>{label}</td>'+''.join(f"<td>{num(result['baselines'][p][k]['run']['metrics']['pnl'])}</td>" for k in ('OHLC','OLHC'))+'</tr>'
        baseline+='</table>'
    search_note=''
    if result.get('search'):
        search_note='<h2>搜索覆盖与边界</h2><p>单笔金额按配置参考价估计，不含费用；买卖笔数仅为历史起点份额空间，现金和T+1仍逐笔检查。显式参数不自动扩展；邻域完整只表示自动维度的相邻参数已计算，不是最优证明。</p><pre>'+esc(json.dumps(result['search'],ensure_ascii=False,indent=2))+'</pre>'
    all_limits=result['limitations']+stage['warnings']+plan['warnings']
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(result['name'])}连续网格研究</title>
<style>body{{font:16px/1.75 system-ui;color:#233129;background:#faf9f5;max-width:1100px;margin:40px auto;padding:0 24px}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #ddd}}.note{{background:#eeece1;padding:18px}}h2{{margin-top:32px}}</style>
<h1>{esc(result['name'])} {esc(result['symbol'])} · 连续网格研究</h1><p class="note"><b>{esc(winner)}</b>。{esc(status)}。日线两条人工路径不是真实成交或收益上下界。</p>
<h2>先确定阶段，再决定统计窗口</h2><p>背景行情从{s['background_start']}开始。阶段{s['start']}—{s['end']}，共{s['used_days']}个观察值，全部保留。起点依据：{esc(stage['reason'])}。证据截至{stage['evidence_as_of']}；{esc(stage['selection_mode'])}，不代表当时已知横盘起点。</p>
<p>统计窗口{plan['window_days']}个观察值，方法：{esc(plan['method'])}。识别到{plan['round_trip_count']}次描述性完整往返，时长{esc(plan['round_trip_durations'])}。完整窗{s['complete_windows']}段，尾段{s['tail_observations']}日单列。窗口只影响统计，不影响持仓、现金、基准、止损或成交。</p>
{search_note}<h2>固定参数比较</h2><p>完整等长窗均值与样本方差单独统计，尾段计入总盈亏但不混入跨窗方差；窗口相互关联，不是独立试验。</p><table><tr><th>间距/数量</th><th>单笔参考金额/元</th><th>预算占比%</th><th>历史初始可买/卖笔数</th><th>较低总盈亏</th><th>較低窗均值</th><th>较高方差</th><th>较高标准差</th><th>较大回撤%</th><th>Pareto</th></tr>{rows}</table>{detail}{baseline}{sensitivity}
<h2>配置参考</h2><p>预算{c['budget']:g}元，基准{c['reference_price']:g}（{esc(c['reference_source'])}，{esc(c['reference_date'])}，{esc(c['reference_time_status'])}），价格区间{c['lower_price']:g}—{c['upper_price']:g}。模拟初仓{c['simulated_initial_quantity']}，底仓{c['min_quantity']}，上限{c['max_quantity']}股/份。这些仓位是固定研究条件，不是优化结果。</p><p>{esc(c['holdings_instruction'])}{esc(c['cash_estimate_note'])}{esc(c['platform_note'])}</p><p>{esc('；'.join(c['issues']))}</p>
<h2>边界</h2><ul>{''.join('<li>'+esc(x)+'</li>' for x in all_limits)}</ul><p>数据SHA256：{esc(result.get('data_sha256',''))}</p></html>'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('symbol','kind','data','as-of','actions','metadata','regime','out'):parser.add_argument('--'+key,required=True)
    parser.add_argument('--name',default='')
    for key in ('budget','initial-fraction','min-fraction','max-fraction','fee-rate','min-fee','slippage','stop-fraction','reference-price'):
        parser.add_argument('--'+key,type=float)
    parser.add_argument('--holdings',type=int);parser.add_argument('--window-days',type=int,default=None)
    parser.add_argument('--grids');parser.add_argument('--lots',default=None,help='Explicit share candidates; omit for budget/price/position-aware search')
    args=parser.parse_args()
    overrides={k:getattr(args,k) for k in ('budget','initial_fraction','min_fraction','max_fraction','fee_rate','min_fee','slippage','stop_fraction') if getattr(args,k) is not None}
    try:
        raw=Path(args.data).read_bytes();bars=json.loads(raw)
        result=study(bars,default_config(args.kind,**overrides),args.as_of,json.loads(Path(args.actions).read_text()),
            metadata=json.loads(Path(args.metadata).read_text()),regime=json.loads(Path(args.regime).read_text()),
            symbol=args.symbol.upper(),name=args.name,window_days=args.window_days,
            grids=[float(x)/100 for x in args.grids.split(',')] if args.grids else None,
            lots=[int(x) for x in args.lots.split(',')] if args.lots is not None else None,holdings=args.holdings,reference_price=args.reference_price)
        result['data_sha256']=hashlib.sha256(raw).hexdigest()
        out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
        for file,value in [('results',result),('configuration',result['configuration'])]:
            (out/(file+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        (out/'report.html').write_text(render_report(result),encoding='utf-8')
        print(json.dumps(dict(status=result['status'],selected=result['selected'],window_days=result['window_plan']['window_days'],report=str(out/'report.html')),ensure_ascii=False))
    except (ValueError,KeyError,TypeError,OSError) as exc:parser.exit(2,f'Research input rejected: {exc}\n')


if __name__=='__main__':main()
