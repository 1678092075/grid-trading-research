"""Daily artificial-path stock / ETF grid experiment with settlement rules.

No brokerage execution. Terminal equity may contain unsold stock when a one-price
bar blocks liquidation; inspect fully_liquidated instead of assuming all P&L is
realized. Dividend receipt dates are unknown: net receivables are valued during
the run and hypothetically settled at its end, never spent before then.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import math
import statistics


@dataclass(frozen=True)
class Config:
    kind: str = "stock"
    budget: float = 20000
    initial_fraction: float = .5
    min_fraction: float = .25
    max_fraction: float = .8
    grid: float = .015
    lot: int = 100
    fee_rate: float = .0001
    min_fee: float = .3
    slippage: float = .01
    stop_fraction: float = .05
    lower_fraction: float = .9
    upper_fraction: float = 1.1
    stamp_rate: float = .0005
    transfer_rate: float = .00001


def money(value):
    return float(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def tick_down(value, tick=.01):
    return round(math.floor(value/tick+1e-8)*tick, 6)


def tick_up(value, tick=.01):
    return round(math.ceil(value/tick-1e-8)*tick, 6)


def validate_bars(bars, tick=.01):
    if len(bars)<2:raise ValueError('Need at least two dated OHLC observations')
    last=''
    for b in bars:
        datetime.strptime(b['date'],'%Y%m%d')
        if b['date']<=last:raise ValueError('Duplicate or unordered date')
        last=b['date']
        if any(b.get(k,1)!=1 for k in ('split_ratio','split_factor')):
            raise ValueError('Stock splits are unsupported')
        o,h,l,c=(b[k] for k in ('open','high','low','close'))
        if not all(math.isfinite(p) and p>0 and abs(p/tick-round(p/tick))<1e-6 for p in (o,h,l,c)):
            raise ValueError('Prices must be positive finite unadjusted valid ticks')
        if not l<=min(o,c)<=max(o,c)<=h:raise ValueError('Inconsistent OHLC')


def run(bars,cfg=Config(),path='OHLC',policy='grid',dividends=None):
    if cfg.kind not in ("stock", "etf-t0", "etf-t1"):
        raise ValueError("Asset settlement must be explicitly confirmed")
    tick=.01 if cfg.kind=="stock" else .001
    t1=cfg.kind!="etf-t0"
    down=lambda p:tick_down(p,tick)
    up=lambda p:tick_up(p,tick)
    validate_bars(bars,tick)
    if path not in ('OHLC','OLHC') or policy not in ('grid','hold','hold_stop','cash'):
        raise ValueError('Unsupported path or policy')
    if not 0<=cfg.min_fraction<=cfg.initial_fraction<=cfg.max_fraction<=1:
        raise ValueError('Position fractions must be ordered within [0,1]')
    if (cfg.budget<=0 or not math.isfinite(cfg.budget) or cfg.lot<=0 or cfg.lot%100
            or not 0<cfg.grid<1 or not 0<cfg.stop_fraction<1
            or not 0<cfg.lower_fraction<cfg.upper_fraction):
        raise ValueError('Invalid budget, lot, grid, range or stop')
    if any(not math.isfinite(x) or x<0 for x in
           (cfg.fee_rate,cfg.min_fee,cfg.slippage,cfg.stamp_rate,cfg.transfer_rate)):
        raise ValueError('Invalid transaction cost')
    dividend_map={}
    for event in dividends or []:
        if set(event)!={'date','cash_per_share'}:
            raise ValueError('Only cash dividends supported; splits/other actions require an explicit model')
        datetime.strptime(event['date'],'%Y%m%d')
        gross=event['cash_per_share']
        if not math.isfinite(gross) or gross<0:raise ValueError('Invalid cash dividend')
        if event['date'] in dividend_map:raise ValueError('Duplicate dividend date')
        if bars[0]['date']<event['date']<=bars[-1]['date'] and event['date'] not in {b['date'] for b in bars}:
            raise ValueError('Dividend ex-date missing from bars')
        dividend_map[event['date']]=gross

    initial_price=bars[0]['close'];ref=initial_price
    lower=initial_price*cfg.lower_fraction;upper=initial_price*cfg.upper_fraction
    floor=math.floor(cfg.budget*cfg.min_fraction/initial_price/100)*100
    cap=math.floor(cfg.budget*cfg.max_fraction/initial_price/100)*100
    target_initial=math.floor(cfg.budget*cfg.initial_fraction/initial_price/100)*100
    cash=cfg.budget;qty=0;sellable=0;receivable=0.;dividend_gross=0.;dividend_tax=0.
    peak=cfg.budget;min_equity=cfg.budget;maxdd=0.;stopped=False;initial_qty=0
    trades=[];daily=[];events=[];tradable=True;last_day=False

    def mark(p):
        nonlocal peak,min_equity,maxdd
        equity=cash+receivable+qty*p
        peak=max(peak,equity);min_equity=min(min_equity,equity)
        maxdd=max(maxdd,1-equity/peak)
        return equity

    def quote(side,p,n):
        px=up(p+cfg.slippage) if side=='buy' else down(p-cfg.slippage)
        if px<=0:raise ValueError('Slippage exceeds price')
        notional=money(n*px)
        fee=money(max(cfg.min_fee,notional*cfg.fee_rate))
        stamp=money(notional*cfg.stamp_rate) if side=='sell' and cfg.kind=='stock' else 0.
        transfer=money(notional*cfg.transfer_rate) if cfg.kind=='stock' else 0.
        return px,notional,fee,stamp,transfer

    def fill(side,p,n,date,reason):
        nonlocal cash,qty,sellable,ref
        if not n:return
        if not tradable:raise ValueError('Attempted fill on blocked day')
        px,notional,fee,stamp,transfer=quote(side,p,n)
        cost=fee+stamp+transfer
        if side=='buy':
            if last_day and t1:raise ValueError('Final-day buying is forbidden under T+1')
            if notional+cost>cash+1e-8:raise ValueError('Insufficient available cash')
            cash=money(cash-notional-cost);qty+=n
            if not t1:sellable+=n
        else:
            if n>sellable:raise ValueError('T+1 violation: sale exceeds overnight inventory')
            cash=money(cash+notional-cost);qty-=n;sellable-=n
        ref=px
        trades.append(dict(date=date,side=side,quantity=n,price=px,fee=fee,
            stamp_duty=stamp,transfer_fee=transfer,total_cost=money(cost),reason=reason,
            quantity_after=qty,sellable_after=sellable,cash_after=cash))
        mark(p)

    def stop_if_needed(p,date):
        nonlocal stopped
        equity=mark(p)
        if not stopped and policy in ('grid','hold_stop') and qty and equity<=cfg.budget*(1-cfg.stop_fraction)+1e-8:
            stopped=True
            events.append(dict(date=date,event='stop_trigger',price=p,quantity=qty,
                               locked_quantity=qty-sellable,tradable=tradable))
            if tradable and sellable:fill('sell',p,sellable,date,'stop')
        return stopped

    def can_buy(p):
        if stopped or (last_day and t1) or not tradable or qty+cfg.lot>cap:return False
        _,value,fee,stamp,transfer=quote('buy',p,cfg.lot)
        return value+fee+stamp+transfer<=cash+1e-8

    def can_sell():
        return tradable and sellable>=cfg.lot and qty-cfg.lot>=floor

    def at_open(p,date):
        if stopped:
            mark(p)
            if tradable and sellable:fill('sell',p,sellable,date,'stop_pending')
            return
        if stop_if_needed(p,date) or not tradable or policy!='grid':return
        if not lower<=p<=upper:return
        if p>=ref*(1+cfg.grid)-1e-10 and can_sell():fill('sell',p,cfg.lot,date,'grid')
        elif p<=ref*(1-cfg.grid)+1e-10 and can_buy(p):fill('buy',p,cfg.lot,date,'grid')
        stop_if_needed(p,date)

    def segment(a,b,date):
        current=a
        for _ in range(10000):
            if stopped or abs(current-b)<1e-9:break
            candidates=[]
            if b<current:
                if policy in ('grid','hold_stop') and qty:
                    trigger=down((cfg.budget*(1-cfg.stop_fraction)-cash-receivable)/qty)
                    if b<=trigger<=current:candidates.append((trigger,'stop'))
                trigger=down(ref*(1-cfg.grid))
                if policy=='grid' and b<=trigger<current and lower<=trigger<=upper and can_buy(trigger):
                    candidates.append((trigger,'buy'))
                event=max(candidates,key=lambda x:x[0]) if candidates else None
            else:
                trigger=up(ref*(1+cfg.grid))
                if policy=='grid' and current<trigger<=b and lower<=trigger<=upper and can_sell():
                    candidates.append((trigger,'sell'))
                event=min(candidates,key=lambda x:x[0]) if candidates else None
            if event is None:break
            current,side=event
            if side=='stop':stop_if_needed(current,date)
            else:
                mark(current);fill(side,current,cfg.lot,date,'grid');stop_if_needed(current,date)
        else:raise RuntimeError('Too many synthetic fills in a segment')
        mark(b)

    def blocked(bar):
        return (bar['high']==bar['low'] or bar.get('volume',1)==0
                or bar.get('suspended',False) or bar.get('tradable',True) is False)

    def snapshot(bar):
        return dict(date=bar['date'],equity=mark(bar['close']),quantity=qty,cash=cash,
                    sellable_quantity=sellable,locked_quantity=qty-sellable,
                    dividend_receivable=receivable,stopped=stopped,tradable=tradable)

    tradable=not blocked(bars[0])
    entry_blocked=not tradable and policy!='cash'
    if entry_blocked:
        events.append(dict(date=bars[0]['date'],event='entry_blocked_cycle_stays_cash'))
    elif policy!='cash':
        # Include acquisition costs in affordability; do not borrow for initial entry.
        while target_initial:
            _,v,f,s,t=quote('buy',initial_price,target_initial)
            if v+f+s+t<=cash+1e-8:break
            target_initial-=100
        fill('buy',initial_price,target_initial,bars[0]['date'],'entry')
        initial_qty=qty;stop_if_needed(initial_price,bars[0]['date'])
    daily.append(snapshot(bars[0]))
    for i,bar in enumerate(bars[1:],1):
        date=bar['date'];sellable=qty;tradable=not blocked(bar);last_day=i==len(bars)-1
        if date in dividend_map and qty:
            gross=money(qty*dividend_map[date]);tax=money(gross*.2) if cfg.kind=='stock' else 0.;net=money(gross-tax)
            dividend_gross=money(dividend_gross+gross);dividend_tax=money(dividend_tax+tax)
            receivable=money(receivable+net)
            events.append(dict(date=date,event='dividend_receivable',eligible_quantity=qty,
                               gross=gross,tax=tax,net=net))
        if not tradable:events.append(dict(date=date,event='blocked_day',quantity=qty))
        if not entry_blocked:
            at_open(bar['open'],date)
            points=[bar[k] for k in (('open','high','low','close') if path=='OHLC' else ('open','low','high','close'))]
            for a,b in zip(points,points[1:]):segment(a,b,date)
        daily.append(snapshot(bar))
    if qty and tradable and sellable:fill('sell',bars[-1]['close'],sellable,bars[-1]['date'],'exit')
    if receivable:
        events.append(dict(date=bars[-1]['date'],event='assumed_terminal_dividend_settlement',amount=receivable))
        cash=money(cash+receivable);receivable=0.
    daily[-1]=snapshot(bars[-1])
    equities=[cfg.budget]+[d['equity'] for d in daily]
    returns=[b/a-1 for a,b in zip(equities,equities[1:])]
    variance=statistics.variance(returns)*252 if len(returns)>1 else 0
    vol=math.sqrt(variance)
    metrics=dict(pnl=equities[-1]-cfg.budget,return_pct=(equities[-1]/cfg.budget-1)*100,
        annual_vol_pct=vol*100,annual_variance=variance,
        sharpe_zero_rf=statistics.mean(returns)*252/vol if vol>1e-12 else 0,
        max_drawdown_pct=maxdd*100,worst_initial_loss_pct=(1-min_equity/cfg.budget)*100,
        stopped=stopped,commission=money(sum(t['fee'] for t in trades)),
        stamp_duty=money(sum(t['stamp_duty'] for t in trades)),
        transfer_fee=money(sum(t['transfer_fee'] for t in trades)),
        dividend_gross=dividend_gross,dividend_tax=dividend_tax,
        dividend_net=money(dividend_gross-dividend_tax),
        grid_trades=sum(t['reason']=='grid' for t in trades),
        buy_trades=sum(t['reason']=='grid' and t['side']=='buy' for t in trades),
        sell_trades=sum(t['reason']=='grid' and t['side']=='sell' for t in trades),
        daily_returns=returns,fully_liquidated=qty==0,ending_quantity=qty,
        pending_stop_quantity=qty if stopped else 0,entry_blocked=entry_blocked)
    return dict(metrics=metrics,daily=daily,trades=trades,events=events,
        initial_quantity=initial_qty,min_quantity=floor,max_quantity=cap,
        limitations=limitations(cfg))



def default_config(kind, **overrides):
    """Asset defaults are explicit; a generic ETF never silently becomes T+0."""
    from dataclasses import replace
    if kind=='stock':base=Config()
    elif kind in ('etf-t0','etf-t1'):
        base=Config(kind=kind,budget=25000,initial_fraction=.585,min_fraction=.344,
                    max_fraction=.86,grid=.008,min_fee=.1,slippage=.001,
                    stamp_rate=0,transfer_rate=0)
    else:raise ValueError('Use stock, etf-t0 or etf-t1 after verifying the product')
    return replace(base,**overrides)


def limitations(cfg):
    return [
        '样本内历史比较；日线OHLC/OLHC是人工路径，不是真实成交或盈亏上下界。',
        '每轮总权益含现金、持仓浮盈亏和分红应收；每轮盈亏不是每格利润。',
        '每轮独立建仓，期末尝试清仓，开盘跳空只成交一格；现金不生息。',
        '所有一字价、零成交量和显式停牌日不假定成交；初始建仓阻塞则该轮现金，期末可能留仓。',
        'T+0当日买入可卖。T+1只能卖隔夜库存，末日禁买；止损锁仓须下一可成交日开盘续卖。',
        '止损按初始资本比例，含底仓全清且当轮不重启；不是净值高点回撤限制，跳空可穿透阈值。',
        '股票计卖出印花税及双边过户费；ETF均不计。各笔费用四舍五入至分，另计不利滑点。',
        '现金分红按除息日隔夜股数记应收；股票保守预扣20%，ETF默认现金分红免税。应收期间不可用于买入，期末假设到账。',
        '现金分红不自动调整网格基准及区间；不支持送转、拆股等其他公司行动。',
        '价格区间外停止新增网格交易，返回区间可恢复；部分成交、非一字涨跌停排队和APP延迟未模拟。',
        '券商APP若没有总权益止损/最大持仓功能，则无法完整复现研究规则；配置不是交易委托。',
    ]
