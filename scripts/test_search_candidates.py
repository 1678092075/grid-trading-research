import unittest
from dataclasses import replace
from grid_engine import default_config, run
from search_candidates import lot_space
from test_study import research, history


class SearchTests(unittest.TestCase):
    def test_low_price_and_larger_budget_expand_share_candidates(self):
        c=default_config('stock')
        low=lot_space(c,3.31);high=lot_space(c,150)
        self.assertEqual(low['maximum_lot'],3000)
        self.assertIn(1500,low['coarse_lots'])
        self.assertLess(high['maximum_lot'],low['maximum_lot'])
        self.assertGreater(lot_space(replace(c,budget=50000),3.31)['maximum_lot'],3000)

    def test_first_order_bound_and_fees_match_engine_start(self):
        bars=history(8)
        c=default_config('stock',initial_fraction=1,min_fraction=.25)
        space=lot_space(c,bars[0]['close'])
        trajectory=run(bars,c,liquidate_end=False)
        self.assertEqual(space['initial_quantity'],trajectory['initial_quantity'])
        for q in range(space['maximum_lot']+100,space['maximum_lot']+401,100):
            self.assertGreater(q,space['initial_buy_space'])
            self.assertGreater(q,space['initial_sell_space'])

    def test_large_search_is_bounded_and_includes_position_limits(self):
        space=lot_space(default_config('stock',budget=10000000),3)
        self.assertFalse(space['full_lot_grid'])
        self.assertLessEqual(len(space['coarse_lots']),22)
        self.assertIn(space['maximum_lot'],space['coarse_lots'])
        self.assertIn(space['initial_sell_space'],space['coarse_lots'])

    def test_explicit_candidates_are_not_silently_expanded(self):
        r=research(grids=[.01,.02],lots=[100,200])
        self.assertEqual({(x['grid_pct'],x['lot']) for x in r['ranking']},
                         {(1.,100),(1.,200),(2.,100),(2.,200)})
        self.assertFalse(r['search']['auto_lots'])
        self.assertFalse(r['search']['auto_grids'])

    def test_auto_search_records_amounts_and_actual_neighbors(self):
        r=research(grids=None,lots=None)
        self.assertGreater(len(r['ranking']),20)
        self.assertTrue(r['search']['neighbor_complete'])
        pairs={(round(x['grid_pct'],6),x['lot']) for x in r['ranking']}
        g=r['selected']['grid_pct'];q=r['selected']['lot']
        for other in (q-100,q+100):
            if 100<=other<=r['search']['position_space']['maximum_lot']:
                self.assertIn((g,other),pairs)
        for x in r['ranking']:
            self.assertAlmostEqual(x['reference_trade_value'],round(x['lot']*r['configuration']['reference_price'],2))
            self.assertAlmostEqual(x['trade_budget_pct'],x['reference_trade_value']/r['config']['budget']*100)

    def test_no_search_when_frequency_evidence_is_insufficient(self):
        b=history(8)
        r=research(bars=b,grids=None,lots=None,window_days=None)
        self.assertEqual(r['status'],'insufficient_evidence')
        self.assertEqual(r['ranking'],[])
        self.assertIsNone(r['selected'])
        self.assertNotIn('search',r)


if __name__=='__main__':unittest.main()
