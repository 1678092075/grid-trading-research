import unittest
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from dataclasses import replace
import grid_engine as ge
import run_study as rs


def history(n=242):
    end=date(2026,10,13);days=[]
    while len(days)<n:
        if end.weekday()<5:days.append(end.strftime('%Y%m%d'))
        end-=timedelta(days=1)
    return [dict(date=d,open=10.,high=10.2,low=9.8,close=10.) for d in reversed(days)]


def actions():
    return dict(verified=True,checked_through='2026-10-13',source_urls=['https://example.org/disclosures'],events=[],other_actions=[])


def metadata():
    return dict(symbol='000792.SZ',price_type='unadjusted',start='2025-01-01',end='2026-10-13')


class StudyTests(unittest.TestCase):
    def test_missing_or_incomplete_actions_are_rejected(self):
        for a in (None,{},dict(actions(),verified=False),dict(actions(),checked_through='2026-01-01'),dict(actions(),other_actions=[{'split':2}])):
            with self.assertRaises(ValueError):rs.study(history(),ge.default_config('stock'),'2026-10-13',a,metadata=metadata(),grids=[.01],lots=[100])

    def test_future_dates_rejected_and_four_cycles_nonoverlapping(self):
        with self.assertRaises(ValueError):rs.study(history(),ge.default_config('stock'),'2026-10-12',actions(),metadata=metadata(),grids=[.01],lots=[100])
        r=rs.study(history(),ge.default_config('stock'),'2026-10-13',actions(),metadata=metadata(),grids=[.01],lots=[100])
        self.assertEqual(len(r['cycles']),4)
        self.assertEqual(r['scope']['discarded_earliest_days'],2)
        for a,b in zip(r['cycles'],r['cycles'][1:]):self.assertLess(a['end'],b['start'])
        self.assertTrue(all(c['observations']==60 for c in r['cycles']))

    def test_not_enough_cycles_rejected(self):
        with self.assertRaises(ValueError):rs.study(history(239),ge.default_config('stock'),'2026-10-13',actions(),metadata=metadata(),grids=[.01],lots=[100])

    def test_low_budget_cannot_create_an_all_zero_winner(self):
        r=rs.study(history(),replace(ge.default_config('stock'),budget=500),'2026-10-13',actions(),metadata=metadata(),grids=[.01],lots=[100])
        self.assertEqual(r['status'],'not_executable')
        self.assertIsNone(r['selected'])
        self.assertFalse(r['configuration']['enable_recommended'])

    def test_sample_variance_uses_cycle_pnl(self):
        s=rs.cycle_stats([1,2,3,4])
        self.assertEqual(s['mean_pnl'],2.5)
        self.assertAlmostEqual(s['variance_pnl'],5/3)

    def test_metadata_mismatch_adjusted_prices_and_incomplete_bar_rejected(self):
        for m in (None,dict(metadata(),symbol='518880.SH'),dict(metadata(),price_type='forward-adjusted'),
                  dict(metadata(),data_complete_through='2026-10-12'),dict(metadata(),complete=False),
                  dict(metadata(),errors=['unresolved price']),dict(metadata(),unresolved_errors=['gap'])):
            with self.assertRaises(ValueError):
                rs.study(history(),ge.default_config('stock'),'2026-10-13',actions(),metadata=m,grids=[.01],lots=[100])

    def test_unsupported_stock_lot_regime_is_rejected(self):
        with self.assertRaises(ValueError):rs.validate_symbol('688001.SH','stock')
        with self.assertRaises(ValueError):rs.validate_symbol('000792.SZ','etf-t0')

    def test_all_losing_candidates_are_not_enable_instructions(self):
        bars=history()
        for b in bars:b.update(open=10.,high=10.01,low=9.99,close=10.)
        r=rs.study(bars,ge.default_config('stock'),'2026-10-13',actions(),metadata=metadata(),grids=[.01],lots=[100])
        self.assertTrue(r['all_candidates_nonpositive'])
        self.assertEqual(r['status'],'comparison_only')
        self.assertFalse(r['configuration']['enable_recommended'])
        self.assertTrue(r['ranking'])

    def test_existing_holdings_do_not_instruct_automatic_top_up(self):
        r=rs.study(history(),ge.default_config('stock'),'2026-10-13',actions(),metadata=metadata(),grids=[.01],lots=[100],holdings=100)
        c=r['configuration']
        self.assertEqual(c['existing_holdings'],100)
        self.assertEqual(c['position_used_for_configuration'],100)
        self.assertFalse(c['can_initial_grid_sell'])
        self.assertIsNone(c['can_initial_grid_buy'])
        self.assertIsNone(c['actual_available_cash'])
        self.assertIsNone(c['buy_to_target_quantity'])
        self.assertFalse(c['enable_recommended'])

    def test_stock_default_candidates_and_user_reference_timestamp(self):
        r=rs.study(history(),ge.default_config('stock'),'2026-10-13',actions(),metadata=metadata(),reference_price=10.)
        self.assertEqual(len(r['ranking']),20)
        self.assertIn(5.,{x['grid_pct'] for x in r['ranking']})
        self.assertIn('unknown-time',r['configuration']['reference_time_status'])
        self.assertIn('unknown-time',rs.render_report(r))

    def test_empty_account_is_not_misreported_as_live_grid_ready(self):
        c=rs.configuration(ge.default_config('stock'),23.13,0,dict(grid_pct=.8,lot=200))
        self.assertIs(c.get('live_start_ready'),False)
        self.assertTrue(c.get('model_executable'))
        self.assertFalse(c['can_initial_grid_sell'])
        self.assertIsNone(c['can_initial_grid_buy'])

    def test_existing_total_holdings_do_not_prove_available_inventory(self):
        c=rs.configuration(ge.default_config('stock'),23.13,400,dict(grid_pct=.8,lot=200))
        self.assertIsNone(c['can_initial_grid_sell'])
        self.assertTrue(c.get('position_allows_grid_sell'))

    def test_cli_writes_all_deliverables_with_data_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name,value in [('data',history()),('metadata',metadata()),('actions',actions())]:
                (root/(name+'.json')).write_text(json.dumps(value))
            p=subprocess.run([sys.executable,str(Path(rs.__file__)),'--symbol','000792.SZ','--kind','stock',
                '--as-of','2026-10-13','--data',str(root/'data.json'),'--metadata',str(root/'metadata.json'),
                '--actions',str(root/'actions.json'),'--out',str(root/'out'),'--grids','1','--lots','100'],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            result=json.loads((root/'out/results.json').read_text())
            self.assertEqual(len(result['data_sha256']),64)
            self.assertTrue((root/'out/configuration.json').exists())
            self.assertIn('样本内',(root/'out/report.html').read_text())
            self.assertIn('1%',(root/'out/report.html').read_text())


class AssetTests(unittest.TestCase):
    def test_etf_t0_and_t1_respect_settlement(self):
        bars=history(3);bars[1].update(open=10,high=10.9,low=9.9,close=10.1)
        cfg=replace(ge.default_config('etf-t0'),budget=10000,initial_fraction=.5,min_fraction=0,max_fraction=.8,grid=.01,slippage=0)
        t0=ge.run(bars,cfg,'OLHC');t1=ge.run(bars,replace(cfg,kind='etf-t1'),'OLHC')
        day=bars[1]['date']
        sold=lambda r:sum(t['quantity'] for t in r['trades'] if t['date']==day and t['side']=='sell')
        self.assertGreater(sold(t0),sold(t1))
        self.assertLessEqual(sold(t1),500)
        self.assertEqual(t0['metrics']['stamp_duty'],0)
        self.assertEqual(t0['metrics']['transfer_fee'],0)

    def test_etf_tick_is_one_mill(self):
        bars=history(2);bars[0].update(open=10.001,high=10.003,low=10.,close=10.001)
        r=ge.run(bars,ge.default_config('etf-t0'),policy='hold')
        self.assertEqual(r['trades'][0]['price'],10.002)


if __name__=='__main__':unittest.main()
