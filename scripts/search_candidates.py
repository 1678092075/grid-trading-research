"""Bounded candidate coverage; no strategy returns are used to choose a regime."""
import math
from grid_engine import money, tick_up


def lot_space(cfg, price):
    initial=math.floor(cfg.budget*cfg.initial_fraction/price/100)*100
    floor=math.floor(cfg.budget*cfg.min_fraction/price/100)*100
    cap=math.floor(cfg.budget*cfg.max_fraction/price/100)*100
    tick=.01 if cfg.kind=='stock' else .001
    entry_price=tick_up(price+cfg.slippage,tick)
    def cost(q):
        value=money(q*entry_price)
        return value+money(max(cfg.min_fee,value*cfg.fee_rate))+(
            money(value*cfg.transfer_rate) if cfg.kind=='stock' else 0)
    while initial and cost(initial)>cfg.budget:initial-=100
    # This is a position-space bound, not a promise of cash or T+1 availability.
    # The engine applies fees, affordability and settlement to every fill.
    buy=max(0,cap-initial);sell=max(0,initial-floor)
    maximum=max(buy,sell)
    n=maximum//100
    if n<=96:
        coarse=list(range(100,maximum+1,100))
    else:
        coarse=sorted({100,200,maximum,buy,sell} |
                      {max(100,round(n*i/16)*100) for i in range(1,17)})
        coarse=[q for q in coarse if 0<q<=maximum]
    return dict(initial_quantity=initial,min_quantity=floor,max_quantity=cap,
                initial_buy_space=buy,initial_sell_space=sell,maximum_lot=maximum,
                coarse_lots=coarse,full_lot_grid=n<=96)


def grid_neighbors(value, coarse):
    """Refine only the declared grid range, in 0.1 percentage-point steps."""
    lo=max((g for g in coarse if g<value-1e-10),default=min(coarse))
    hi=min((g for g in coarse if g>value+1e-10),default=max(coarse))
    return sorted({value,lo,hi} | {i/1000 for i in range(math.ceil(lo*1000-1e-8),
                                                       math.floor(hi*1000+1e-8)+1)})


def lot_neighbors(value, space):
    # Bounded for unusually large budgets; the report discloses partial coverage.
    return [q for q in range(max(100,value-1200),min(space['maximum_lot'],value+1200)+1,100)]


def immediate_neighbors(g, q, grids, space, auto_grid, auto_lot):
    pairs=[]
    if auto_grid:
        for x in (round(g-.001,9),round(g+.001,9)):
            if min(grids)-1e-9<=x<=max(grids)+1e-9:pairs.append((x,q))
    if auto_lot:
        for x in (q-100,q+100):
            if 100<=x<=space['maximum_lot']:pairs.append((g,x))
    return pairs
