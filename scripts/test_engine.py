"""Economic and A-share settlement invariants for the stock engine."""
import unittest
from dataclasses import replace
import grid_engine as se


def bar(date, p, high=None, low=None, close=None):
    return dict(date=date, open=p, high=p+.01 if high is None else high,
                low=p-.01 if low is None else low, close=p if close is None else close)


class StockEngineTests(unittest.TestCase):
    def cfg(self, **kw):
        return replace(se.Config(budget=10000, initial_fraction=.5,
                       min_fraction=0, max_fraction=.8, grid=.01,
                       slippage=0, fee_rate=0, min_fee=0,
                       stamp_rate=0, transfer_rate=0), **kw)

    def test_t1_can_sell_old_inventory_after_buying_today(self):
        bars=[bar('20260101',10),bar('20260102',10,10.9,9.9,10.1),bar('20260105',10)]
        r=se.run(bars,self.cfg(),'OLHC',liquidate_end=True)
        day=[t for t in r['trades'] if t['date']=='20260102']
        self.assertEqual(day[0]['side'],'buy')
        self.assertTrue(any(t['side']=='sell' for t in day))
        self.assertEqual(sum(t['quantity'] for t in day if t['side']=='sell'),500)
        self.assertTrue(all(t['sellable_after']>=0 for t in r['trades']))

    def test_stop_sells_old_inventory_then_locked_shares_next_open(self):
        bars=[bar('20260101',10),bar('20260102',10,10,8.8,9),bar('20260105',8,8.1,7.9,8)]
        r=se.run(bars,self.cfg(),'OHLC',liquidate_end=True)
        stops=[t for t in r['trades'] if t['reason'] in ('stop','stop_pending')]
        self.assertEqual(stops[0]['date'],'20260102')
        self.assertEqual(stops[0]['quantity'],500)
        self.assertEqual(stops[1]['date'],'20260105')
        self.assertEqual(stops[1]['price'],8)
        self.assertGreater(stops[1]['quantity'],0)
        first=r['trades'].index(stops[0])
        self.assertFalse(any(t['side']=='buy' for t in r['trades'][first:]))
        self.assertTrue(r['metrics']['stopped'])
        self.assertLess(r['metrics']['pnl'],-500)

    def test_tick_tax_and_transfer_are_charged_and_reconcile(self):
        bars=[bar('20260101',10),bar('20260102',10)]
        r=se.run(bars,se.Config(),'OHLC','hold',liquidate_end=True)
        buy,sell=r['trades']
        self.assertEqual((buy['price'],sell['price']),(10.01,9.99))
        self.assertEqual(buy['stamp_duty'],0)
        self.assertEqual(sell['stamp_duty'],5.0)
        self.assertEqual(buy['transfer_fee'],.1)
        self.assertEqual(sell['transfer_fee'],.1)
        self.assertAlmostEqual(r['metrics']['pnl'],-27.2)
        self.assertTrue(all(abs(t['price']*100-round(t['price']*100))<1e-7 for t in r['trades']))

    def test_dividend_receivable_counts_equity_but_not_available_cash(self):
        bars=[bar('20260101',10),bar('20260102',9.2),bar('20260105',9.2)]
        r=se.run(bars,self.cfg(),'OHLC','hold',liquidate_end=True,
                 dividends=[dict(date='20260102',cash_per_share=1)])
        self.assertAlmostEqual(r['daily'][1]['equity'],10000)
        self.assertAlmostEqual(r['daily'][1]['cash'],5000)
        self.assertAlmostEqual(r['daily'][1]['dividend_receivable'],400)
        self.assertAlmostEqual(r['metrics']['pnl'],0)
        self.assertAlmostEqual(r['metrics']['dividend_tax'],100)
        self.assertAlmostEqual(r['metrics']['dividend_net'],400)

    def test_dividend_only_overnight_quantity_and_no_lookahead(self):
        bars=[bar('20260101',10),bar('20260102',10,10,9.9,9.9),bar('20260105',9.9)]
        r=se.run(bars,self.cfg(),'OHLC',liquidate_end=True,dividends=[dict(date='20260102',cash_per_share=.1)])
        self.assertEqual(r['daily'][0]['dividend_receivable'],0)
        self.assertAlmostEqual(r['daily'][1]['dividend_receivable'],40)

    def test_no_new_buys_on_last_day(self):
        bars=[bar('20260101',10),bar('20260102',10,10,9.5,9.5)]
        r=se.run(bars,self.cfg(),'OHLC',liquidate_end=True)
        self.assertFalse(any(t['side']=='buy' and t['date']=='20260102' for t in r['trades']))
        self.assertEqual(r['daily'][-1]['quantity'],0)

    def test_one_price_day_defers_stop_and_never_fakes_liquidation(self):
        bars=[bar('20260101',10),bar('20260102',8,8,8,8)]
        r=se.run(bars,self.cfg(),'OHLC',liquidate_end=True)
        self.assertEqual(len(r['trades']),1)
        self.assertTrue(r['metrics']['stopped'])
        self.assertFalse(r['metrics']['fully_liquidated'])
        self.assertEqual(r['metrics']['pending_stop_quantity'],500)
        self.assertAlmostEqual(r['metrics']['pnl'],-1000)

    def test_split_is_explicitly_rejected(self):
        bars=[bar('20260101',10),bar('20260102',5)]
        with self.assertRaises(ValueError):
            se.run(bars,self.cfg(),liquidate_end=True,dividends=[dict(date='20260102',cash_per_share=0,split_ratio=2)])

    def test_blocked_stop_sells_only_on_next_tradable_open(self):
        bars=[bar('20260101',10),bar('20260102',8,8,8,8),bar('20260105',7.8)]
        r=se.run(bars,self.cfg(),'OHLC',liquidate_end=True)
        self.assertEqual(len(r['trades']),2)
        self.assertEqual(r['trades'][-1]['date'],'20260105')
        self.assertEqual(r['trades'][-1]['price'],7.8)
        self.assertEqual(r['trades'][-1]['reason'],'stop_pending')

    def test_cash_inventory_and_net_dividend_reconcile_to_equity(self):
        bars=[bar('20260101',10),bar('20260102',10,10.9,9.9,10.1),bar('20260105',9.8)]
        cfg=self.cfg(fee_rate=.0001,min_fee=.3,stamp_rate=.0005,transfer_rate=.00001,slippage=.01)
        r=se.run(bars,cfg,'OLHC',liquidate_end=True,dividends=[dict(date='20260102',cash_per_share=.1)])
        cash=cfg.budget;qty=0
        for t in r['trades']:
            side=1 if t['side']=='buy' else -1
            cash-=side*t['quantity']*t['price']+t['fee']+t['stamp_duty']+t['transfer_fee']
            qty+=side*t['quantity']
            self.assertAlmostEqual(cash,t['cash_after'])
            self.assertEqual(qty,t['quantity_after'])
        cash+=r['metrics']['dividend_net']
        self.assertAlmostEqual(cash+qty*bars[-1]['close']-cfg.budget,r['metrics']['pnl'])

    def test_fractional_stop_threshold_uses_stock_tick(self):
        bars=[bar('20260101',10),bar('20260102',10,10,8.8,9),bar('20260105',9)]
        r=se.run(bars,self.cfg(min_fee=.3,slippage=.01),'OHLC',liquidate_end=True)
        self.assertTrue(r['metrics']['stopped'])
        self.assertTrue(all(abs(t['price']*100-round(t['price']*100))<1e-7 for t in r['trades']))


class ContinuousEngineTests(unittest.TestCase):
    cfg=StockEngineTests.cfg

    def test_continuous_mode_allows_last_day_buys_and_has_no_exit(self):
        bars=[bar('20260101',10),bar('20260102',10,10,9.7,9.7)]
        r=se.run(bars,self.cfg(),'OHLC',liquidate_end=False)
        self.assertEqual(sum(t['reason']=='entry' for t in r['trades']),1)
        self.assertFalse(any(t['reason']=='exit' for t in r['trades']))
        self.assertTrue(any(t['side']=='buy' and t['date']=='20260102' for t in r['trades']))
        self.assertGreater(r['daily'][-1]['locked_quantity'],0)
        self.assertGreater(r['daily'][-1]['quantity'],500)
        self.assertEqual(r['metrics']['terminal_valuation'],'mark_to_market')
        self.assertEqual(r['metrics']['initial_capital'],10000)

    def test_one_entry_and_reference_is_carried_across_days(self):
        bars=[bar('20260101',10),bar('20260102',10,10,9.9,9.94),bar('20260105',9.94,9.95,9.93,9.94)]
        r=se.run(bars,self.cfg(),'OHLC',liquidate_end=False)
        self.assertEqual(sum(t['reason']=='entry' for t in r['trades']),1)
        self.assertEqual([d['reference_price'] for d in r['daily']],[10,9.9,9.9])
        self.assertEqual(r['daily'][-1]['cash'],r['daily'][1]['cash'])
        self.assertEqual(r['daily'][-1]['quantity'],r['daily'][1]['quantity'])

    def test_last_day_locked_stop_is_not_erroneously_cleared(self):
        bars=[bar('20260101',10),bar('20260102',10,10,8.8,9)]
        r=se.run(bars,self.cfg(),'OHLC',liquidate_end=False)
        self.assertTrue(r['metrics']['stopped'])
        self.assertEqual(r['metrics']['pending_stop_quantity'],300)
        self.assertEqual(r['daily'][-1]['quantity'],300)
        self.assertEqual(r['daily'][-1]['sellable_quantity'],0)
        self.assertFalse(r['metrics']['fully_liquidated'])
        self.assertFalse(any(t['reason']=='exit' for t in r['trades']))

    def test_continuous_dividend_is_receivable_not_spendable_cash(self):
        bars=[bar('20260101',10),bar('20260102',9.2)]
        r=se.run(bars,self.cfg(),policy='hold',dividends=[dict(date='20260102',cash_per_share=1)],liquidate_end=False)
        last=r['daily'][-1]
        self.assertEqual(last['cash'],5000)
        self.assertEqual(last['dividend_receivable'],400)
        self.assertEqual(last['quantity'],500)
        self.assertEqual(last['equity'],10000)
        self.assertFalse(any(e['event']=='assumed_terminal_dividend_settlement' for e in r['events']))

    def test_marked_equity_conserves_cash_holdings_receivables_and_costs(self):
        bars=[bar('20260101',10),bar('20260102',10,10.5,9.9,10.1),bar('20260105',9.8)]
        cfg=self.cfg(fee_rate=.0001,min_fee=.3,stamp_rate=.0005,transfer_rate=.00001,slippage=.01)
        r=se.run(bars,cfg,'OLHC',dividends=[dict(date='20260102',cash_per_share=.1)],liquidate_end=False)
        cash=cfg.budget;qty=0;fees=0
        for t in r['trades']:
            sign=1 if t['side']=='buy' else -1
            cost=t['fee']+t['stamp_duty']+t['transfer_fee']
            cash-=sign*t['quantity']*t['price']+cost
            fees+=cost;qty+=sign*t['quantity']
            self.assertAlmostEqual(cash,t['cash_after'])
            self.assertEqual(qty,t['quantity_after'])
        last=r['daily'][-1]
        self.assertAlmostEqual(last['cash'],cash)
        self.assertAlmostEqual(last['equity'],cash+qty*bars[-1]['close']+last['dividend_receivable'])
        self.assertAlmostEqual(r['metrics']['pnl'],last['equity']-cfg.budget)
        self.assertAlmostEqual(fees,r['metrics']['commission']+r['metrics']['stamp_duty']+r['metrics']['transfer_fee'])

    def test_explicit_liquidation_mode_and_asset_defaults(self):
        r=se.run([bar('20260101',10),bar('20260102',10)],self.cfg(),policy='hold',liquidate_end=True)
        self.assertEqual(r['metrics']['terminal_valuation'],'liquidation')
        self.assertEqual(r['daily'][-1]['quantity'],0)
        self.assertEqual(se.default_config('stock').max_fraction,1)
        self.assertEqual(se.default_config('etf-t0').max_fraction,.86)

    def test_library_default_stays_compatible_with_terminal_liquidation(self):
        r=se.run([bar('20260101',10),bar('20260102',10)],self.cfg(),policy='hold')
        self.assertEqual(r['metrics']['terminal_valuation'],'liquidation')
        self.assertEqual(r['trades'][-1]['reason'],'exit')

    def test_etf_continuous_last_day_buys_remain_sellable(self):
        r=se.run([bar('20260101',10),bar('20260102',10,10,9.7,9.7)],
                 se.default_config('etf-t0'),liquidate_end=False)
        self.assertTrue(any(t['date']=='20260102' and t['side']=='buy' for t in r['trades']))
        self.assertFalse(any(t['reason']=='exit' for t in r['trades']))
        self.assertEqual(r['daily'][-1]['locked_quantity'],0)
        self.assertEqual(r['daily'][-1]['sellable_quantity'],r['daily'][-1]['quantity'])


if __name__=='__main__':unittest.main()
