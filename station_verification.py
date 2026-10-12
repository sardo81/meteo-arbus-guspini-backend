"""Verify stations independently using real, fresh observations only.

Reuse archiveToHistoryRow unchanged. No immediate forecast, backfill, or
retrospective reconciliation. A partial verification is not fully complete.
"""
from __future__ import annotations
import copy
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROME = ZoneInfo('Europe/Rome')
STATIONS = {'arbus': ('Arbus', 'IARBUS7'), 'guspini': ('Guspini', 'IGUSPI1')}
MAX_AGE_SECONDS = 30 * 60


def finite(value):
    if value is None or isinstance(value, bool) or str(value).strip() == '':
        return False
    try:
        return math.isfinite(float(value))
    except (ValueError, TypeError, OverflowError):
        return False


def parse_time(value, naive_tz=ROME):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=naive_tz)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def observation_time(data):
    epoch = data.get('epoch')
    if finite(epoch):
        try:
            epoch = float(epoch)
            if epoch > 10_000_000_000:
                epoch /= 1000
            return datetime.fromtimestamp(epoch, timezone.utc)
        except (ValueError, OverflowError, OSError):
            pass
    # Collection time must never replace a missing measurement timestamp.
    return parse_time(data.get('updated'))


def station_plan(payload, value, now):
    if now.tzinfo is None:
        raise ValueError('Timezone-aware current time required')
    target = parse_time(value)
    if target is None or not 0 <= (now - target).total_seconds() <= MAX_AGE_SECONDS:
        raise RuntimeError('VERIFY_TIME_WINDOW_FAIL: no future or retrospective verification')
    sources = payload.get('stations', {}) if isinstance(payload, dict) else {}
    plan = {}
    for key, (place, code) in STATIONS.items():
        source = sources.get(code) or {}
        data = source.get('data') if isinstance(source, dict) else None
        item = {'place': place, 'stationCode': code, 'status': 'unavailable',
                'reason': 'Fonte indisponibile: nessuna osservazione reale'}
        plan[key] = item
        if not isinstance(data, dict) or not finite(data.get('temperature_c')):
            continue
        observed = observation_time(data)
        if observed is None:
            item['reason'] = 'Timestamp della misura mancante o non valido'
            continue
        age = (now - observed).total_seconds()
        if age < -120 or age > MAX_AGE_SECONDS or abs((observed-target).total_seconds()) > MAX_AGE_SECONDS:
            item['reason'] = 'Osservazione scaduta o non riferita allo slot'
            item['observedAt'] = observed.isoformat()
            continue
        item.update(status='ready', reason=None, observedAt=observed.isoformat(),
                    capturedAt=source.get('captured_at_utc'), ageSeconds=round(age),
                    data=copy.deepcopy(data))
    return plan


PREPARE_JS = r'''([value, plan, runKey, verifiedAt]) => {
 const target = new Date(value).getTime();
 const archive = JSON.parse(localStorage.getItem('meteoForecastArchiveV1') || '[]');
 const previous = JSON.parse(localStorage.getItem('meteoHistoryV5') || '[]');
 const original = JSON.parse(JSON.stringify(archive));
 const additions = [], changedIds = [], reports = {};
 const validNumber = v => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
 document.getElementById('forecastTime').value = value;
 for (const key of ['arbus','guspini']) {
  for (const el of document.querySelectorAll(`[id^="${key}-obs-"],[id^="${key}-event-"]`)) {
   if ('value' in el) el.value = '';
  }
  const rainType=document.getElementById(key+'-obs-rain-type');if(rainType)rainType.value='auto';
  const hail=document.getElementById(key+'-event-hail');if(hail)hail.value='unknown';
 }
 for (const key of ['arbus','guspini']) {
  const item=plan[key];
  const report={...item,sourceStatus:item.status};delete report.data;reports[key]=report;
  const same=archive.filter(a => a && a.place===item.place && a.validTime===value);
  const existing=same.filter(a=>a.verified);
  if(existing.length && !existing.every(a=>previous.some(h=>h.source==='archive' && h.place===a.place && h.when===value && (h.archiveId===a.id || h.downloadedAt===a.issueTime)))) {
   throw new Error('VERIFY_INTEGRITY_FAIL: verified forecast without history');
  }
  const pending=same.filter(a=>!a.verified);
  if(!pending.length && existing.length) {
   report.status='already_verified';report.count=existing.length;report.reason=null;continue;
  }
  if(item.status!=='ready') continue;
  if(!pending.length) {report.status='no_archived_forecast';report.reason='Previsione prospettica assente';continue;}
  const valid=pending.every(a=>typeof a.id==='string' && a.id && Number.isFinite(new Date(a.issueTime).getTime()) && new Date(a.issueTime).getTime()<target && validNumber(a.gfs) && validNumber(a.ecmwf) && validNumber(a.baseCorrected) && validNumber(a.localForecast));
  if(!valid) {report.status='invalid_archived_forecast';report.reason='Integrità della previsione non valida';continue;}
  const data=item.data, hours=observationIntervalHours(value);
  const fields={'temp':data.temperature_c,'hum':data.humidity_pct,'dew':data.dewpoint_c,
   'pressure':data.pressure_hpa,'wind':data.wind_speed_kmh,'dir':data.wind_direction_deg,
   'gust':data.wind_gust_kmh,'rain-rate':data.rain_rate_mm_h,'rain-today':data.rain_today_mm,
   'rain-interval':data[`rain_${hours}h_mm`],'rain-max':data[`rain_rate_max_${hours}h_mm_h`],'rain-hours':hours};
  for(const [suffix,v] of Object.entries(fields)) {
   const el=document.getElementById(key+'-obs-'+suffix);if(el)el.value=validNumber(v)?String(Number(v)):'';
  }
  for(const a of pending) {
   if(previous.some(h=>h.archiveId===a.id || (h.source==='archive'&&h.place===a.place&&h.when===a.validTime&&h.downloadedAt===a.issueTime))) {
    throw new Error('VERIFY_INTEGRITY_FAIL: pending archive already has history');
   }
   const row=archiveToHistoryRow(a,Number(data.temperature_c),key);
   row.archiveId=a.id;row.observationStationCode=item.stationCode;
   row.observedAt=item.observedAt;row.observationCapturedAt=item.capturedAt;
   row.verifiedAt=verifiedAt;row.verificationRunKey=runKey;
   if(row.source!=='archive'||row.place!==a.place||row.when!==value||row.downloadedAt!==a.issueTime||row.obs!==Number(data.temperature_c)||!Number.isFinite(row.errCorrected)) {
    throw new Error('VERIFY_INTEGRITY_FAIL: invalid comparison row');
   }
   additions.push(row);a.verified=true;a.verifiedAt=verifiedAt;changedIds.push(a.id);
  }
  report.status='verified';report.count=pending.length;report.reason=null;
 }
 if(new Set(changedIds).size!==changedIds.length || archive.length!==original.length)throw new Error('VERIFY_INTEGRITY_FAIL: duplicate ids');
 for(let i=0;i<archive.length;i++) {
  const a={...archive[i]},before=original[i];
  if(changedIds.includes(a.id)){a.verified=before.verified;if('verifiedAt' in before)a.verifiedAt=before.verifiedAt;else delete a.verifiedAt;}
  if(JSON.stringify(a)!==JSON.stringify(before))throw new Error('VERIFY_INTEGRITY_FAIL: unrelated archive modified');
 }
 const history=[...additions,...previous];
 return {reports,changedIds,additions,archive,history,previousHistory:previous,previousArchive:original};
}'''


def verify_independent(page, value, payload, run_key, now=None):
    now = now or datetime.now(ROME)
    plan = station_plan(payload, value, now)
    draft = page.evaluate(PREPARE_JS, [value, plan, run_key, now.isoformat(timespec='seconds')])
    previous_history = draft.pop('previousHistory')
    previous_archive = draft.pop('previousArchive')
    additions = draft['additions']
    changed = draft['changedIds']
    if draft['history'] != additions + previous_history or len(additions) != len(changed):
        raise RuntimeError('VERIFY_INTEGRITY_FAIL: historical rows altered')
    if len(set(changed)) != len(changed) or len(draft['archive']) != len(previous_archive):
        raise RuntimeError('VERIFY_INTEGRITY_FAIL: archive cardinality')
    for before, after in zip(previous_archive, draft['archive']):
        restored = copy.deepcopy(after)
        if before.get('id') in changed:
            if after.get('verified') is not True:
                raise RuntimeError('VERIFY_INTEGRITY_FAIL: forecast not marked')
            if 'verified' in before:
                restored['verified'] = before['verified']
            else:
                restored.pop('verified', None)
            if 'verifiedAt' in before:
                restored['verifiedAt'] = before['verifiedAt']
            else:
                restored.pop('verifiedAt', None)
        if restored != before:
            raise RuntimeError('VERIFY_INTEGRITY_FAIL: forecast values changed')
    verified = [r['place'] for r in draft['reports'].values() if r['status'] in ('verified','already_verified')]
    status = 'complete' if len(verified)==2 else ('partial' if verified else 'unavailable')
    report = {'runKey':run_key,'validTime':value,'checkedAt':now.isoformat(timespec='seconds'),
              'status':status,'verifiedPlaces':verified,'stations':draft['reports'],
              'newComparisons':len(additions),'archiveValuesUnchanged':True}
    # Never call saveObservation(), which has immediate/retrospective fallbacks.
    page.evaluate(r'''([oldH,oldA,h,a])=>{
      if(JSON.stringify(JSON.parse(localStorage.getItem('meteoHistoryV5')||'[]'))!==JSON.stringify(oldH) || JSON.stringify(JSON.parse(localStorage.getItem('meteoForecastArchiveV1')||'[]'))!==JSON.stringify(oldA))throw new Error('VERIFY_INTEGRITY_FAIL: concurrent local change');
      localStorage.setItem('meteoHistoryV5',JSON.stringify(h));
      localStorage.setItem('meteoForecastArchiveV1',JSON.stringify(a));
    }''', [previous_history, previous_archive, draft['history'], draft['archive']])
    for item in report['stations'].values():
        print(f"VERIFY_STATION {value} place={item['place']} station={item['stationCode']} status={item['status']} observed_at={item.get('observedAt')} count={item.get('count',0)} reason={item.get('reason')}", flush=True)
    print(f'VERIFY_INDEPENDENT status={status} comparisons={len(additions)} retrospective=False', flush=True)
    return report


def record_verification_meta(meta, run_key, report):
    result = copy.deepcopy(meta)
    results = result.setdefault('verificationRuns', {})
    results[run_key] = report
    result['verificationRuns'] = dict(list(results.items())[-120:])
    result['lastVerificationAttempt'] = report
    result['lastRunStatus'] = report['status']
    result['lastProcessedRun'] = run_key
    partial = [x for x in result.get('partialRuns', []) if x != run_key]
    if report['status'] == 'partial':
        partial.append(run_key)
    result['partialRuns'] = partial[-120:]
    return result


def finalize_verification_state(original, updated, fresh, report, run_key, merge_meta):
    for key in ('history','archive'):
        if fresh.get(key, []) != original.get(key, []):
            raise RuntimeError('VERIFY_CLOUD_CONFLICT: archive/history changed during verification')
    result = copy.deepcopy(fresh)
    result['history'] = updated['history']
    result['archive'] = updated['archive']
    meta = record_verification_meta(fresh.get('automationMeta') or {}, run_key, report)
    result['automationMeta'] = meta
    if report['status'] == 'complete':
        result = merge_meta(result, run_key)
    return result
