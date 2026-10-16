import unittest
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from dataclasses import replace
import grid_engine as ge
import run_study as rs


def history(n=242):
    end=date(2026,10,13);days=[]
    while len(days)<n:
        if end.weekday()<5:days.append(end.strftime('%Y%m%d'))
        end-=timedelta(days=1)
    wave=[10.,10.15,10.2,10.15,10.,9.85,9.8,9.85]
    return [dict(date=d,open=10.,high=10.25,low=9.75,close=wave[i%len(wave)]) for i,d in enumerate(reversed(days))]


def actions():
    return dict(verified=True,checked_through='2026-10-13',source_urls=['https://example.org/disclosures'],events=[],other_actions=[])


def metadata():
    return dict(symbol='000792.SZ',price_type='unadjusted',start='2025-01-01',end='2026-10-13')


def regime(bars, status='candidate'):
    return dict(start=datetime.strptime(bars[0]['date'],'%Y%m%d').date().isoformat(),status=status,selection_mode='retrospective',
                evidence_as_of='2026-10-13',reason='价格在区间内多次往返；这是回看候选，不是当时已知信号')


def research(bars=None, **kw):
    bars=bars or history(80)
    defaults=dict(metadata=metadata(),regime=regime(bars),window_days=10,grids=[.01],lots=[100])
    defaults.update(kw)
    return rs.study(bars,ge.default_config('stock'),'2026-10-13',actions(),**defaults)


class StudyTests(unittest.TestCase):
    def test_regime_evidence_is_required_not_implicit_full_year(self):
        with self.assertRaises(ValueError):
            rs.study(history(),ge.default_config('stock'),'2026-10-13',actions(),metadata=metadata())

    def test_statistics_partition_does_not_change_trades_or_terminal_equity(self):
        a=research(window_days=10);b=research(window_days=20)
        for path in ('OHLC','OLHC'):
            ar=a['ranking'][0]['paths'][path]['run'];br=b['ranking'][0]['paths'][path]['run']
            self.assertEqual(ar['trades'],br['trades'])
            self.assertEqual(ar['metrics']['pnl'],br['metrics']['pnl'])
            self.assertEqual(sum(t['reason']=='entry' for t in ar['trades']),1)
            self.assertFalse(any(t['reason']=='exit' for t in ar['trades']))
            stats=a['ranking'][0]['paths'][path]['summary']
            self.assertAlmostEqual(stats['sum_segment_pnl'],ar['metrics']['pnl'])

    def test_short_or_flat_history_does_not_invent_cross_window_variance(self):
        r=research(history(12),window_days=None)
        self.assertEqual(r['status'],'insufficient_evidence')
        self.assertIsNone(r['selected'])
        self.assertEqual(r['scope']['used_days'],12)
        self.assertEqual(r['scope']['discarded_days'],0)
        self.assertIsNone(r['baselines']['hold']['OHLC']['summary']['variance_pnl'])

    def test_short_window_override_cannot_create_regime_evidence(self):
        r=research(history(3),window_days=1)
        self.assertEqual(r['status'],'insufficient_evidence')
        self.assertIsNone(r['selected'])
        self.assertEqual(r['ranking'],[])

    def test_uncertain_stage_does_not_force_an_optimal_grid(self):
        bars=history(30);r=research(bars,regime=regime(bars,'uncertain'))
        self.assertEqual(r['status'],'regime_uncertain')
        self.assertIsNone(r['selected'])
        self.assertEqual(r['ranking'],[])

    def test_missing_actions_and_wrong_symbol_still_rejected(self):
        for a in (None,{},dict(actions(),verified=False),dict(actions(),other_actions=[{'split':2}])):
            with self.assertRaises(ValueError):
                rs.study(history(),ge.default_config('stock'),'2026-10-13',a,metadata=metadata(),regime=regime(history()))
        with self.assertRaises(ValueError):research(metadata=dict(metadata(),symbol='518880.SH'))

    def test_future_data_rejected(self):
        with self.assertRaises(ValueError):
            rs.study(history(),ge.default_config('stock'),'2026-10-12',actions(),metadata=metadata(),regime=regime(history()))

    def test_low_budget_cannot_create_an_all_zero_winner(self):
        bars=history(30)
        r=rs.study(bars,ge.default_config('stock',budget=500),'2026-10-13',actions(),metadata=metadata(),regime=regime(bars))
        self.assertEqual(r['status'],'not_executable');self.assertIsNone(r['selected'])

    def test_resolved_retrieval_errors_do_not_reject_valid_data(self):
        r=research(metadata=dict(metadata(),complete=True,errors=[{'resolved':True,'error':'retry recovered'}]))
        self.assertTrue(r['ranking'])
        with self.assertRaises(ValueError):research(metadata=dict(metadata(),errors=[{'resolved':False}]))

    def test_all_losing_candidates_not_enabled(self):
        bars=history(32)
        for i,b in enumerate(bars):b.update(high=10.02,low=9.98,close=10.01 if i%4<2 else 9.99)
        r=research(bars)
        self.assertTrue(r['all_candidates_nonpositive']);self.assertEqual(r['status'],'comparison_only')
        self.assertFalse(r['configuration']['enable_recommended'])

    def test_existing_holdings_do_not_prove_cash_or_sellable(self):
        c=rs.configuration(ge.default_config('stock'),23.13,0,dict(grid_pct=.8,lot=200))
        self.assertFalse(c['live_start_ready']);self.assertFalse(c['can_initial_grid_sell'])
        self.assertIsNone(c['actual_available_cash']);self.assertIsNone(c['buy_to_target_quantity'])

    def test_stock_budget_cap_is_full_budget_and_defaults_not_optimized(self):
        cfg=ge.default_config('stock')
        self.assertEqual(cfg.max_fraction,1.)
        r=research(grids=None,lots=None)
        self.assertGreater(max(x['lot'] for x in r['ranking']),200)
        self.assertTrue(r['search']['auto_lots'])

    def test_cli_writes_phase_and_continuous_valuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);bars=history(40)
            for name,value in [('data',bars),('metadata',metadata()),('actions',actions()),('regime',regime(bars))]:
                (root/(name+'.json')).write_text(json.dumps(value))
            args=[sys.executable,str(Path(rs.__file__)),'--symbol','000792.SZ','--kind','stock','--as-of','2026-10-13',
                  '--out',str(root/'out'),'--window-days','10','--grids','1','--lots','100']
            for name in ['data','metadata','actions','regime']:args+=['--'+name,str(root/(name+'.json'))]
            p=subprocess.run(args,capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            r=json.loads((root/'out/results.json').read_text())
            self.assertEqual(r['evaluation_mode'],'continuous_mark_to_market')
            self.assertEqual(r['scope']['used_days'],40)
            self.assertIn('连续',(root/'out/report.html').read_text())
            self.assertEqual(len(r['data_sha256']),64)


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
