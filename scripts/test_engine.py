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
        r=se.run(bars,self.cfg(),'OLHC')
        day=[t for t in r['trades'] if t['date']=='20260102']
        self.assertEqual(day[0]['side'],'buy')
        self.assertTrue(any(t['side']=='sell' for t in day))
        self.assertEqual(sum(t['quantity'] for t in day if t['side']=='sell'),500)
        self.assertTrue(all(t['sellable_after']>=0 for t in r['trades']))

    def test_stop_sells_old_inventory_then_locked_shares_next_open(self):
        bars=[bar('20260101',10),bar('20260102',10,10,8.8,9),bar('20260105',8,8.1,7.9,8)]
        r=se.run(bars,self.cfg(),'OHLC')
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
        r=se.run(bars,se.Config(),'OHLC','hold')
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
        r=se.run(bars,self.cfg(),'OHLC','hold',
                 dividends=[dict(date='20260102',cash_per_share=1)])
        self.assertAlmostEqual(r['daily'][1]['equity'],10000)
        self.assertAlmostEqual(r['daily'][1]['cash'],5000)
        self.assertAlmostEqual(r['daily'][1]['dividend_receivable'],400)
        self.assertAlmostEqual(r['metrics']['pnl'],0)
        self.assertAlmostEqual(r['metrics']['dividend_tax'],100)
        self.assertAlmostEqual(r['metrics']['dividend_net'],400)

    def test_dividend_only_overnight_quantity_and_no_lookahead(self):
        bars=[bar('20260101',10),bar('20260102',10,10,9.9,9.9),bar('20260105',9.9)]
        r=se.run(bars,self.cfg(),'OHLC',dividends=[dict(date='20260102',cash_per_share=.1)])
        self.assertEqual(r['daily'][0]['dividend_receivable'],0)
        self.assertAlmostEqual(r['daily'][1]['dividend_receivable'],40)

    def test_no_new_buys_on_last_day(self):
        bars=[bar('20260101',10),bar('20260102',10,10,9.5,9.5)]
        r=se.run(bars,self.cfg(),'OHLC')
        self.assertFalse(any(t['side']=='buy' and t['date']=='20260102' for t in r['trades']))
        self.assertEqual(r['daily'][-1]['quantity'],0)

    def test_one_price_day_defers_stop_and_never_fakes_liquidation(self):
        bars=[bar('20260101',10),bar('20260102',8,8,8,8)]
        r=se.run(bars,self.cfg(),'OHLC')
        self.assertEqual(len(r['trades']),1)
        self.assertTrue(r['metrics']['stopped'])
        self.assertFalse(r['metrics']['fully_liquidated'])
        self.assertEqual(r['metrics']['pending_stop_quantity'],500)
        self.assertAlmostEqual(r['metrics']['pnl'],-1000)

    def test_split_is_explicitly_rejected(self):
        bars=[bar('20260101',10),bar('20260102',5)]
        with self.assertRaises(ValueError):
            se.run(bars,self.cfg(),dividends=[dict(date='20260102',cash_per_share=0,split_ratio=2)])

    def test_blocked_stop_sells_only_on_next_tradable_open(self):
        bars=[bar('20260101',10),bar('20260102',8,8,8,8),bar('20260105',7.8)]
        r=se.run(bars,self.cfg(),'OHLC')
        self.assertEqual(len(r['trades']),2)
        self.assertEqual(r['trades'][-1]['date'],'20260105')
        self.assertEqual(r['trades'][-1]['price'],7.8)
        self.assertEqual(r['trades'][-1]['reason'],'stop_pending')

    def test_cash_inventory_and_net_dividend_reconcile_to_equity(self):
        bars=[bar('20260101',10),bar('20260102',10,10.9,9.9,10.1),bar('20260105',9.8)]
        cfg=self.cfg(fee_rate=.0001,min_fee=.3,stamp_rate=.0005,transfer_rate=.00001,slippage=.01)
        r=se.run(bars,cfg,'OLHC',dividends=[dict(date='20260102',cash_per_share=.1)])
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
        r=se.run(bars,self.cfg(min_fee=.3,slippage=.01),'OHLC')
        self.assertTrue(r['metrics']['stopped'])
        self.assertTrue(all(abs(t['price']*100-round(t['price']*100))<1e-7 for t in r['trades']))


if __name__=='__main__':unittest.main()
