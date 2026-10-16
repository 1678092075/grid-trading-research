"""Descriptive price-stage windows and accounting of one continuous equity path.

Stage selection is supplied by the caller, never optimized using strategy P&L.
The median center and frequency estimate use the whole selected historical stage;
they are retrospective descriptions, not causal regime detection or an optimum.
"""
from datetime import date, datetime
import math
import re
import statistics


def _iso(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected ISO date YYYY-MM-DD")
    return date.fromisoformat(value)


def _dated_bars(bars):
    if not bars:
        raise ValueError("Price stage is empty")
    dates = []
    for bar in bars:
        raw = bar.get("date")
        if not isinstance(raw, str) or not re.fullmatch(r"\d{8}", raw):
            raise ValueError("Bar dates must be YYYYMMDD")
        parsed = datetime.strptime(raw, "%Y%m%d").date()
        if dates and parsed <= dates[-1]:
            raise ValueError("Bar dates must be unique and increasing")
        close = bar.get("close")
        if isinstance(close, bool) or not isinstance(close, (int, float)) or not math.isfinite(close) or close <= 0:
            raise ValueError("Close prices must be positive finite numbers")
        dates.append(parsed)
    return dates


def validate_regime(bars, regime, as_of):
    """Validate a caller-selected start; return stage bars and unverified selection info."""
    end = _iso(as_of)
    dates = _dated_bars(bars)
    if dates[-1] > end:
        raise ValueError("Price bars extend beyond as_of")
    if not isinstance(regime, dict):
        raise ValueError("An explicit price-stage selection is required")
    allowed = {"start", "status", "selection_mode", "mode", "evidence_as_of", "reason"}
    if set(regime) - allowed:
        raise ValueError("Unexpected regime fields; causal claims and P&L-selected stages are unsupported")
    if not {"start", "status", "evidence_as_of", "reason"} <= set(regime):
        raise ValueError("Regime needs start, status, evidence_as_of and reason")
    mode = regime.get("selection_mode", regime.get("mode"))
    if mode not in ("retrospective", "user_supplied"):
        raise ValueError("selection_mode must be retrospective or user_supplied; causal detection is unsupported")
    if "selection_mode" in regime and "mode" in regime and regime["mode"] != regime["selection_mode"]:
        raise ValueError("Conflicting regime selection modes")
    status = regime["status"]
    if status not in ("candidate", "uncertain", "not_sideways"):
        raise ValueError("Unknown regime status")
    reason = regime["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("A price-based selection reason is required")
    if re.search(r"\bpnl\b|sharpe|profit|backtest.return|最优|收益最大|盈利最多|回测收益|回测盈亏|实时准确识别|无未来信息", reason, re.I):
        raise ValueError("Selection reason must describe price facts, not P&L optimization or causal accuracy")
    start, evidence = _iso(regime["start"]), _iso(regime["evidence_as_of"])
    if start > end or evidence > end:
        raise ValueError("Future stage start or future evidence is prohibited")
    if evidence < start:
        raise ValueError("Stage evidence cannot predate its start")
    if start not in dates:
        raise ValueError("Stage start must match a real observed trading date")
    stage = bars[dates.index(start):]
    info = dict(start=start.isoformat(), end=dates[-1].isoformat(), as_of=end.isoformat(),
                evidence_as_of=evidence.isoformat(), selection_mode=mode, status=status,
                reason=reason.strip(), n_observations=len(stage),
                eligible_for_optimization=status == "candidate", can_optimize=status == "candidate", causal=False,
                selection_verified=False,
                interpretation="Caller-supplied candidate stage; price rationale is recorded, not algorithmically proven.",
                warnings=["Stage statistics are retrospective; no claim of real-time detection or statistical optimality."])
    if status != "candidate":
        info["warnings"].append("Stage is not a confirmed candidate: do not optimize or recommend enabling a grid.")
    return stage, info


def _diagnostic_prices(bars, dates, actions):
    """Add cumulative ex-date gross cash to closes for diagnostic crossings only."""
    events = {}
    for event in actions or []:
        if not isinstance(event, dict) or set(event) != {"date", "cash_per_share"}:
            raise ValueError("Only explicit cash dividends are supported; splits/other actions need a separate model")
        raw = event["date"]
        if not isinstance(raw, str) or not re.fullmatch(r"\d{8}", raw):
            raise ValueError("Dividend ex-date must be YYYYMMDD")
        day = datetime.strptime(raw, "%Y%m%d").date()
        cash = event["cash_per_share"]
        if isinstance(cash, bool) or not isinstance(cash, (int, float)) or not math.isfinite(cash) or cash < 0:
            raise ValueError("Invalid cash dividend")
        if day in events:
            raise ValueError("Duplicate cash-dividend date")
        if day > dates[-1]:
            raise ValueError("Dividend occurs after the observed stage")
        if dates[0] < day <= dates[-1] and day not in dates:
            raise ValueError("Dividend ex-date is missing from stage observations")
        events[day] = cash
    accumulated, adjusted, used = 0.0, [], []
    for bar, day in zip(bars, dates):
        # Prior/start-date events are already reflected in the starting price.
        if day > dates[0] and day in events:
            accumulated += events[day]
            used.append(dict(date=day.strftime("%Y%m%d"), cash_per_share=events[day]))
        adjusted.append(bar["close"] + accumulated)
    return adjusted, used


def derive_windows(stage_bars, override=None, actions=None):
    """Choose statistics-only windows from hysteresis crossings of a historical center.

    Indices are [start_index, end_index), including all daily observations exactly
    once. An incomplete tail is retained but excluded from equal-length variance.
    """
    dates = _dated_bars(stage_bars)
    n = len(stage_bars)
    prices, used = _diagnostic_prices(stage_bars, dates, actions)
    center = statistics.median(prices)
    changes = [abs(b-a) for a, b in zip(prices, prices[1:])]
    median_change = statistics.median(changes) if changes else 0.0
    tolerance = max(abs(center), 1.0) * 1e-12
    deadband = max(0.5 * median_change, tolerance)
    states, state = [], None
    for i, price in enumerate(prices):
        side = 1 if price >= center + deadband else (-1 if price <= center - deadband else None)
        if side is not None and side != state:
            states.append(dict(index=i, date=stage_bars[i]["date"], side=side,
                               diagnostic_price=price, kind="initial_side" if state is None else "side_transition"))
            state = side
    trips = []
    # Adjacent trips may share one endpoint, but their elapsed intervals do not overlap.
    for i in range(0, len(states)-2, 2):
        a, b, c = states[i:i+3]
        trips.append(dict(start_index=a["index"], turn_index=b["index"], end_index=c["index"],
                          duration_observation_intervals=c["index"]-a["index"],
                          start_date=a["date"], end_date=c["date"]))
    durations = [t["duration_observation_intervals"] for t in trips]
    warnings = ["Descriptive retrospective heuristic using this entire price stage; not causal detection or a validated optimum.",
                "Daily closes omit intraday reversals; crossing counts are not grid fills."]
    if override is not None:
        if isinstance(override, bool) or not isinstance(override, int) or override < 1:
            raise ValueError("Window override must be a positive integer observation count")
        window, method = override, "explicit_statistics_window_override"
        warnings.append("Window override changes reporting boundaries only; never restart or liquidate the strategy at boundaries.")
    elif len(durations) >= 2:
        window = min(n, max(1, math.ceil(2 * statistics.median(durations))))
        method = "two_round_trips_median_duration"
    else:
        window, method = n, "whole_stage_insufficient_round_trips"
        warnings.append("Fewer than two observed complete round trips: frequency estimate is insufficient; report the whole stage as one segment.")
    complete = []
    def segment(start, end):
        return dict(start_index=start, end_index=end, observations=end-start,
                    start_date=stage_bars[start]["date"], end_date=stage_bars[end-1]["date"])
    for start in range(0, n-window+1, window):
        complete.append(segment(start, start+window))
    tail_start = len(complete) * window
    tail = segment(tail_start, n) if tail_start < n else None
    if len(complete) < 2:
        warnings.append("Fewer than two complete equal-length windows; sample variance and standard deviation must be null.")
    return dict(window_days=window, method=method, n_observations=n,
                stage_start=stage_bars[0]["date"], stage_end=stage_bars[-1]["date"],
                observation_dates=[b["date"] for b in stage_bars],
                index_convention="Zero-based [start_index, end_index); end_date is the last included observation.",
                heuristic="Center=median(dividend-adjusted closes); deadband half-width=max(0.5*median(abs daily close changes, including zeros), numerical tolerance). Alternate sides outside the deadband; two side changes form a nonoverlapping round trip. With >=2 trips choose ceil(2*median trip intervals), capped at stage length; otherwise whole stage.",
                diagnostic_price_method="Close plus cumulative gross per-share cash dividends after stage start; diagnostic only, not a total-return investment series.",
                center=center, deadband_half_width=deadband, median_absolute_daily_change=median_change,
                dividend_adjustments=used, crossings=states, crossing_count=max(0,len(states)-1),
                round_trips=trips, round_trip_durations=durations, round_trip_count=len(trips),
                frequency_estimate_sufficient=len(trips)>=2,
                complete_windows=complete, tail_window=tail, warnings=warnings)


def summarize_equity(daily, budget, plan):
    """Difference endpoints of ONE continuous path; never rerun a window.

    Window [s,e) P&L = daily[e-1].equity - (budget if s==0 else
    daily[s-1].equity). This assigns each daily equity change once, including
    initial acquisition cost and any final liquidation already in the path.
    """
    if isinstance(budget, bool) or not isinstance(budget, (int,float)) or not math.isfinite(budget) or budget <= 0:
        raise ValueError("Budget must be positive and finite")
    if len(daily) != plan["n_observations"] or not daily:
        raise ValueError("Equity observations must match the entire selected stage")
    if [d["date"] for d in daily] != plan["observation_dates"]:
        raise ValueError("Equity dates differ from the window plan")
    for d in daily:
        value=d["equity"]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
            raise ValueError("Equity must be finite")
    definitions = plan["complete_windows"] + ([plan["tail_window"]] if plan["tail_window"] else [])
    cursor, segments = 0, []
    for definition in definitions:
        start,end=definition["start_index"],definition["end_index"]
        if start != cursor or not start < end <= len(daily):
            raise ValueError("Window plan must partition every equity observation once")
        begin_equity=budget if start==0 else daily[start-1]["equity"]
        end_equity=daily[end-1]["equity"]
        segments.append(dict(definition, starting_equity=begin_equity, ending_equity=end_equity,
                             pnl=end_equity-begin_equity, pnl_pct_initial_budget=(end_equity-begin_equity)/budget*100))
        cursor=end
    if cursor != len(daily):
        raise ValueError("Window plan leaves equity observations unreported")
    full_count=len(plan["complete_windows"])
    full=segments[:full_count]
    if any(w["end_index"]-w["start_index"] != plan["window_days"] for w in full):
        raise ValueError("Complete windows must have equal requested lengths")
    pnl=[w["pnl"] for w in full]
    variance=statistics.variance(pnl) if len(pnl)>=2 else None
    total=daily[-1]["equity"]-budget
    reconciled=math.fsum(w["pnl"] for w in segments)
    if not math.isclose(reconciled,total,rel_tol=1e-12,abs_tol=1e-8):
        raise ValueError("Segment P&L does not reconcile to continuous total P&L")
    return dict(window_days=plan["window_days"], complete_windows=full,
                tail_window=segments[-1] if plan["tail_window"] else None,
                n_complete_windows=full_count, complete_count=full_count, windows=full,
                tail_pnl=segments[-1]["pnl"] if plan["tail_window"] else None, n_observations=len(daily),
                mean_pnl=statistics.mean(pnl) if pnl else None,
                variance_pnl=variance, std_pnl=math.sqrt(variance) if variance is not None else None,
                worst_pnl=min(pnl) if pnl else None,best_pnl=max(pnl) if pnl else None,
                positive_windows=sum(p>0 for p in pnl), total_pnl=total,
                sum_segment_pnl=reconciled, ending_equity=daily[-1]["equity"],
                accounting="Continuous equity endpoint differences; initial segment starts from budget. No cash reset, new entry, boundary liquidation or artificial compounded returns.",
                variance_scope="Sample variance (n-1) of complete equal-length window P&Ls; incomplete tail excluded; null for n<2. Windows remain dependent observations of one strategy.",
                warnings=list(plan["warnings"]))
