import unittest
from datetime import date,timedelta
from regime_windows import validate_regime,derive_windows,summarize_equity


def bars(prices):
    return [dict(date=(date(2026,1,1)+timedelta(days=i)).strftime('%Y%m%d'),close=p) for i,p in enumerate(prices)]


class RegimeWindowsTests(unittest.TestCase):
    def regime(self,**kw):
        value=dict(start='2026-01-01',status='candidate',selection_mode='retrospective',
                   evidence_as_of='2026-02-01',reason='价格围绕中枢多次上下穿越，阶段内高低点范围接近。')
        value.update(kw);return value

    def test_selection_validates_actual_start_and_never_claims_causal(self):
        stage,info=validate_regime(bars([10]*8),self.regime(),'2026-02-01')
        self.assertEqual(len(stage),8);self.assertFalse(info['causal']);self.assertFalse(info['selection_verified'])
        for change in [dict(start='2026-03-01'),dict(start='2026-01-20'),dict(evidence_as_of='2026-03-01'),dict(selection_mode='causal'),dict(reason='回测收益最高的开始日'),dict(causal=True)]:
            with self.assertRaises(ValueError):validate_regime(bars([10]*8),self.regime(**change),'2026-02-01')

    def test_uncertain_and_non_sideways_are_not_optimization_eligible(self):
        for status in ('uncertain','not_sideways'):
            _,info=validate_regime(bars([10]*8),self.regime(status=status),'2026-02-01')
            self.assertFalse(info['eligible_for_optimization'])

    def test_short_flat_and_monotone_histories_use_whole_stage(self):
        for prices in ([10],[10,11],[10]*30,list(range(10,40))):
            p=derive_windows(bars(prices));self.assertEqual(p['window_days'],len(prices))
            self.assertFalse(p['frequency_estimate_sufficient']);self.assertIsNone(p['tail_window'])

    def test_dividend_does_not_create_a_false_down_up_trip(self):
        raw=bars([10,10,9,10,10,9,10])
        events=[dict(date=raw[2]['date'],cash_per_share=1),dict(date=raw[5]['date'],cash_per_share=1)]
        p=derive_windows(raw,actions=events)
        self.assertEqual(p['round_trip_count'],0)
        self.assertEqual(len(p['dividend_adjustments']),2)
        self.assertEqual(raw[2]['close'],9)

    def test_frequency_and_duration_change_windows_without_four_segment_target(self):
        fast=derive_windows(bars([9,11]*20))
        slow=derive_windows(bars(([9]*4+[11]*4)*5))
        self.assertEqual(fast['window_days'],4)
        self.assertEqual(slow['window_days'],16)
        short=derive_windows(bars([9,11,9]))
        self.assertEqual(short['window_days'],3)
        self.assertEqual(len(fast['complete_windows']),10)
        self.assertEqual(len(slow['complete_windows']),2)
        self.assertEqual(slow['tail_window']['observations'],8)

    def test_continuous_accounting_keeps_all_days_and_entry_cost(self):
        b=bars([9,11]*5+[9]);plan=derive_windows(b,override=4)
        values=[99,102,101,104,103,105,104,106,102,107,108]
        daily=[dict(date=d['date'],equity=v) for d,v in zip(b,values)]
        result=summarize_equity(daily,100,plan)
        self.assertEqual([s['pnl'] for s in result['complete_windows']],[4,2])
        self.assertEqual(result['tail_window']['pnl'],2)
        self.assertEqual(result['mean_pnl'],3)
        self.assertEqual(result['variance_pnl'],2)
        self.assertEqual(result['total_pnl'],8)
        self.assertEqual(result['sum_segment_pnl'],8)
        self.assertEqual(result['complete_windows'][1]['starting_equity'],104)

    def test_one_window_or_only_tail_has_no_fake_variance(self):
        b=bars([10,11,12])
        d=[dict(date=x['date'],equity=100+i) for i,x in enumerate(b)]
        for override in (None,10):
            s=summarize_equity(d,100,derive_windows(b,override=override))
            self.assertIsNone(s['variance_pnl']);self.assertIsNone(s['std_pnl'])
            self.assertEqual(s['sum_segment_pnl'],2)

    def test_window_override_changes_statistics_not_continuous_total(self):
        b=bars([9,11]*12);d=[dict(date=x['date'],equity=100+i*.5) for i,x in enumerate(b)]
        a=summarize_equity(d,100,derive_windows(b,override=7));c=summarize_equity(d,100,derive_windows(b,override=9))
        self.assertEqual(a['total_pnl'],c['total_pnl'])
        self.assertNotEqual(a['n_complete_windows'],c['n_complete_windows'])

    def test_reject_missing_dates_overlap_and_splits(self):
        b=bars([9,11]*8);p=derive_windows(b,override=5);d=[dict(date=x['date'],equity=100) for x in b]
        with self.assertRaises(ValueError):summarize_equity(d[1:],100,p)
        p['complete_windows'][1]['start_index']=4
        with self.assertRaises(ValueError):summarize_equity(d,100,p)
        with self.assertRaises(ValueError):derive_windows(b,actions=[dict(date=b[2]['date'],split_ratio=2)])


if __name__=='__main__':unittest.main()
