#!/usr/bin/env node
'use strict';
// Read-only iFinD OHLC acquisition. No credentials or brokerage APIs are read here.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const DAY = 86400000;
const sha256 = value => crypto.createHash('sha256').update(value).digest('hex');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function dateValue(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw Error('Expected ISO date YYYY-MM-DD');
  const n = Date.parse(value + 'T00:00:00Z');
  if (!Number.isFinite(n) || new Date(n).toISOString().slice(0, 10) !== value) throw Error('Invalid date');
  return n;
}
function oneYearStart(end) {
  dateValue(end);
  const [y, m, d] = end.split('-').map(Number);
  const clamped = Math.min(d, new Date(Date.UTC(y - 1, m, 0)).getUTCDate());
  return new Date(Date.UTC(y - 1, m - 1, clamped) + DAY).toISOString().slice(0, 10);
}
function months(start, end) {
  const result = [];
  for (let cursor = start.slice(0, 7) + '-01'; cursor <= end;) {
    const [y, m] = cursor.split('-').map(Number);
    const last = new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10);
    result.push({month: cursor.slice(0, 7), from: cursor < start ? start : cursor, to: last > end ? end : last});
    cursor = new Date(Date.UTC(y, m, 1)).toISOString().slice(0, 10);
  }
  return result;
}
function queryFor(symbol, batch, explicit = false) {
  const base = `${symbol} ${batch.from}至${batch.to}，逐日查询不复权开盘价、最高价、最低价、收盘价。`;
  return base + (explicit ? '必须是每个交易日自己的日开盘、日最高、日最低、日收盘；禁止区间最高价、区间最低价或其他区间统计。' : '')
    + '返回日期列和证券代码列；完整覆盖所请求的每个日历日，非交易日OHLC留空，不填充；不是净值，不要前复权或后复权。';
}
function numeric(cell) {
  const s = String(cell ?? '').trim();
  if (['', '--', '-', 'null', 'None', 'NaN', '\\t'].includes(s)) return null;
  if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(s)) throw Error('Malformed OHLC number');
  const n = Number(s);
  if (!Number.isFinite(n) || n <= 0) throw Error('OHLC must be finite and positive');
  return n;
}
function parseResponse(raw, {symbol, from, to, asOf = to}) {
  dateValue(from); dateValue(to); dateValue(asOf);
  if (raw.ok === false || raw.isError || raw.data?.error) throw Error('iFinD returned a failed response');
  const content = raw.data?.result?.content ?? raw.result?.content;
  if (!Array.isArray(content)) throw Error('Missing iFinD content');
  const answers = content.filter(c => c.type === 'text').map(c => {
    let decoded;
    try { decoded = JSON.parse(c.text); } catch { throw Error('iFinD text is not JSON'); }
    if (decoded.code !== 1 || typeof decoded.data?.answer !== 'string') throw Error('Missing successful OHLC answer');
    return decoded.data;
  });
  if (!answers.length) throw Error('Empty response');
  const byDate = new Map(), excludedSymbols = new Set(), adjustmentChecks = [];
  let duplicateCount = 0;
  for (const answer of answers) {
    const params = answer.indicators_params || {};
    for (const [name, config] of Object.entries(params)) {
      if (/区间.*[高低]价/.test(name)) throw Error('Interval extrema are not daily OHLC');
      if (config['复权方式'] && config['复权方式'] !== '不复权') throw Error('Adjusted prices rejected');
    }
    adjustmentChecks.push(Object.keys(params).length ? params : {status: 'No adjustment metadata returned; requested unadjusted'});
    let columns = null;
    for (const line of answer.answer.split(/\r?\n/)) {
      if (!line.trim().startsWith('|')) continue;
      const cells = line.trim().split('|').slice(1, -1).map(x => x.trim());
      if (cells.every(c => /^:?-+:?$/.test(c))) continue;
      if (cells.some(c => c === '证券代码')) {
        if (cells.some(c => /区间.*[高低]价/.test(c))) throw Error('Interval extrema are not daily OHLC');
        const clean = cells.map(c => c.replace(/[（(].*$/, '').trim());
        columns = {};
        for (const [key, names] of Object.entries({symbol:['证券代码'], date:['日期','交易日期'], open:['开盘价','日开盘价'], high:['最高价','日最高价'], low:['最低价','日最低价'], close:['收盘价','日收盘价']})) {
          columns[key] = clean.findIndex(c => names.includes(c));
          if (columns[key] < 0) throw Error('Missing exact daily OHLC header: ' + key);
        }
        continue;
      }
      if (!columns) throw Error('Data row without daily OHLC header');
      if (cells[columns.symbol] !== symbol) { excludedSymbols.add(cells[columns.symbol] || '(empty)'); continue; }
      const compact = cells[columns.date].replaceAll('-', '');
      if (!/^\d{8}$/.test(compact)) throw Error('Invalid row date');
      const iso = compact.slice(0,4)+'-'+compact.slice(4,6)+'-'+compact.slice(6);
      dateValue(iso);
      if (iso > asOf) throw Error('Future data relative to as-of rejected');
      const row = {date: compact};
      for (const key of ['open','high','low','close']) row[key] = numeric(cells[columns[key]]);
      if (byDate.has(compact)) {
        if (JSON.stringify(byDate.get(compact)) !== JSON.stringify(row)) throw Error('Conflicting duplicate date');
        duplicateCount++;
      } else byDate.set(compact, row);
    }
  }
  const rows = [], skipped = [], outsideRequestedRange = [];
  for (let n = dateValue(from); n <= dateValue(to); n += DAY) {
    const iso = new Date(n).toISOString().slice(0,10), date = iso.replaceAll('-', '');
    const row = byDate.get(date);
    if (!row) throw Error('Incomplete calendar coverage: ' + date + '; trading-day-only response needs independent calendar evidence');
    const missing = ['open','high','low','close'].filter(k => row[k] === null);
    if (missing.length) {
      const weekday = new Date(n).getUTCDay();
      skipped.push({date, missing, kind: weekday === 0 || weekday === 6 ? 'weekend_missing_ohlc' : 'weekday_missing_ohlc_requires_review', retained_fields: Object.fromEntries(Object.entries(row).filter(([k,v]) => k !== 'date' && v !== null))});
      continue;
    }
    if (!(row.low <= Math.min(row.open,row.close) && Math.max(row.open,row.close) <= row.high)) throw Error('Inconsistent OHLC: ' + date);
    rows.push(row);
  }
  for (const date of byDate.keys()) if (date < from.replaceAll('-','') || date > to.replaceAll('-','')) outsideRequestedRange.push(date);
  if (!byDate.size) throw Error('Requested symbol absent');
  return {rows, skipped, duplicateCount, excludedSymbols:[...excludedSymbols], outsideRequestedRange, adjustmentChecks};
}
function mergeRows(groups) {
  const map = new Map();
  for (const rows of groups) for (const row of rows) {
    if (map.has(row.date) && JSON.stringify(map.get(row.date)) !== JSON.stringify(row)) throw Error('Conflicting duplicate date between batches');
    map.set(row.date,row);
  }
  return [...map.values()].sort((a,b) => a.date.localeCompare(b.date));
}
function parseArgs(argv) {
  const opts = {};
  for (let i=0;i<argv.length;i++) {
    const key=argv[i];
    if (key==='--refresh') {opts.refresh=true;continue;}
    if (key==='--help') {opts.help=true;continue;}
    if (!['--symbol','--service','--as-of','--out','--ifind-module','--raw-dir','--start'].includes(key) || !argv[i+1] || argv[i+1].startsWith('--')) throw Error('Unknown or incomplete CLI option');
    opts[key.slice(2)]=argv[++i];
  }
  return opts;
}
async function main(argv=process.argv.slice(2)) {
  const opts=parseArgs(argv);
  if (opts.help) {console.log('node fetch_history.cjs --symbol 000792.SZ --service stock|fund --as-of YYYY-MM-DD --out DIR [--ifind-module PATH] [--raw-dir DIR] [--refresh] [--start YYYY-MM-DD]\nDefault period is one calendar year; --start explicitly narrows it. Offline replay reads raw-YYYY-MM.json and verifies symbol, OHLC and calendar coverage.');return;}
  if (!/^\d{6}\.(SH|SZ)$/.test(opts.symbol || '') || !['stock','fund'].includes(opts.service) || !opts.out || !opts['as-of']) throw Error('Provide symbol with SH/SZ suffix, service, as-of and output directory');
  const end=opts['as-of'];dateValue(end);
  const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  if (end>today) throw Error('as-of is in the future in Asia/Shanghai');
  const start=opts.start || oneYearStart(end);dateValue(start);
  if (start> end || start<oneYearStart(end)) throw Error('--start must narrow the natural-year period');
  const out=path.resolve(opts.out);fs.mkdirSync(out,{recursive:true});
  const cacheDir=path.join(out,'raw-cache');fs.mkdirSync(cacheDir,{recursive:true});
  const tool=opts.service==='stock'?'get_stock_performance':'get_fund_market_performance';
  let call;
  if (!opts['raw-dir']) {
    const mod=opts['ifind-module'] || path.join(process.env.IFIND_SKILL_ROOT || path.join(os.homedir(),'.agents','skills','ifind-finance-data'),'call-node.js');
    call=require(path.resolve(mod)).call;
    if (typeof call!=='function') throw Error('iFinD module does not export call');
  }
  const fetchedAt=new Date().toISOString(), groups=[], rawRecords=[], batchMeta=[], errors=[];
  let lastRequest=0;
  for (const batch of months(start,end)) {
    let success=false;
    for (let attempt=0;attempt<(opts['raw-dir']?1:3);attempt++) {
      const query=queryFor(opts.symbol,batch,attempt>0);
      const request={symbol:opts.symbol,service:opts.service,tool,from:batch.from,to:batch.to,query};
      const fingerprint=sha256(JSON.stringify(request));
      const cache=path.join(cacheDir,fingerprint+'.json');
      let raw, mode='network', sourceRetrievedAt=null;
      try {
        if (opts['raw-dir']) {
          const file=path.join(path.resolve(opts['raw-dir']),'raw-'+batch.month+'.json');
          raw=JSON.parse(fs.readFileSync(file,'utf8'));mode='offline_replay';
        } else if (!opts.refresh && batch.month!==end.slice(0,7) && fs.existsSync(cache)) {
          const envelope=JSON.parse(fs.readFileSync(cache,'utf8'));
          if (envelope.fingerprint!==fingerprint || JSON.stringify(envelope.request)!==JSON.stringify(request)) throw Error('Cache identity mismatch');
          raw=envelope.raw;sourceRetrievedAt=envelope.retrieved_at;mode='verified_cache';
        } else {
          await sleep(Math.max(0,lastRequest+600-Date.now()));lastRequest=Date.now();
          raw=await call(opts.service,tool,{query});sourceRetrievedAt=new Date().toISOString();
        }
        const record={request,fingerprint,attempt:attempt+1,mode,source_retrieved_at:sourceRetrievedAt,raw};rawRecords.push(record);
        const parsed=parseResponse(raw,{symbol:opts.symbol,from:batch.from,to:batch.to,asOf:end});
        if (mode==='network') fs.writeFileSync(cache,JSON.stringify({fingerprint,request,retrieved_at:sourceRetrievedAt,raw},null,2));
        groups.push(parsed.rows);batchMeta.push({...batch,fingerprint,mode,source_retrieved_at:sourceRetrievedAt,rows:parsed.rows.length,...Object.fromEntries(Object.entries(parsed).filter(([k])=>k!=='rows'))});
        success=true;break;
      } catch (error) {
        // Only our fixed validation messages are logged; transport exceptions may embed credentials.
        const known=/^(iFinD returned|Missing|Empty|Interval|Adjusted|Data row|Invalid row|Invalid date|Future|Malformed|OHLC|Conflicting|Incomplete|Inconsistent|Requested symbol|Cache identity)/.test(error.message);
        const message=known?error.message:'Acquisition failed (details withheld; inspect provider status and raw response)';
        errors.push({...batch,attempt:attempt+1,fingerprint,error:message,resolved:false});
        if (raw) fs.writeFileSync(path.join(cacheDir,`${fingerprint}.failed-${attempt+1}.json`),JSON.stringify(raw,null,2));
      }
    }
    if (success) for (const error of errors.filter(e=>e.month===batch.month)) error.resolved=true;
    else batchMeta.push({...batch,status:'failed'});
  }
  let daily=[],mergeError=null;
  try {daily=mergeRows(groups);} catch(error) {mergeError=error.message;errors.push({error:mergeError,resolved:false});}
  if (!daily.length && !mergeError) errors.push({error:'No usable OHLC rows in requested range',resolved:false});
  const complete=!mergeError && daily.length>0 && batchMeta.every(b=>b.status!=='failed');
  const dailyText=JSON.stringify(daily,null,2)+'\n';
  const metadata={symbol:opts.symbol,service:opts.service,source:`iFinD MCP ${opts.service}/${tool}`,price_type:'unadjusted',as_of:end,latest_valid_trading_day:daily.at(-1)?.date||null,retrieved_at:fetchedAt,completed_at:new Date().toISOString(),requested:{start,end},actual:{start:daily[0]?.date||null,end:daily.at(-1)?.date||null,count:daily.length},complete,errors,batches:batchMeta,skipped_dates:batchMeta.flatMap(b=>b.skipped||[]),sha256:sha256(dailyText),corporate_actions_status:'NOT VERIFIED: separately obtain and review dividends/splits; missing actions must never be assumed to mean none.',limitations:['Daily OHLC contains no real intraday execution order.','Weekday missing OHLC requires holiday/suspension verification; skipped observations are not assumed exchange holidays.','Calendar completeness means requested calendar dates were returned, not independently verified exchange trading-day coverage.','Most recent requested month always refreshes online; offline replay never claims fresh data and has unknown original retrieval time.','Offline replay accepts a covering monthly response and records dates trimmed outside the requested partial month; future dates are rejected.']};
  fs.writeFileSync(path.join(out,'raw.json'),JSON.stringify(rawRecords,null,2));
  fs.writeFileSync(path.join(out,'metadata.json'),JSON.stringify(metadata,null,2));
  // An incomplete acquisition must not leave an apparently usable daily.json from this or an older run.
  if (complete) fs.writeFileSync(path.join(out,'daily.json'),dailyText);
  else {fs.writeFileSync(path.join(out,'daily.partial.json'),dailyText);if(fs.existsSync(path.join(out,'daily.json')))fs.unlinkSync(path.join(out,'daily.json'));}
  console.log(JSON.stringify({symbol:opts.symbol,complete,rows:daily.length,start:metadata.actual.start,end:metadata.actual.end,unresolved_errors:errors.filter(e=>!e.resolved).length,weekday_missing_review:metadata.skipped_dates.filter(d=>d.kind==='weekday_missing_ohlc_requires_review').length}));
  if (!complete) process.exitCode=1;
  return metadata;
}
module.exports={dateValue,oneYearStart,months,queryFor,parseResponse,mergeRows,parseArgs,main};
if(require.main===module)main().catch(()=>{console.error('Failed before acquisition: check CLI dates, paths, and iFinD module configuration. No credentials are logged.');process.exitCode=1;});
