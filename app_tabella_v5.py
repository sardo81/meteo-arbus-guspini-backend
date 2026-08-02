#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import re, time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

STATIONS={
 'IARBUS7':{'name':'Arbus (SU)','url':'https://www.wunderground.com/dashboard/pws/IARBUS7'},
 'IGUSPI1':{'name':'Guspini (SU)','url':'https://www.wunderground.com/dashboard/pws/IGUSPI1'},
}
NUMBER_RE=r'-?\d+(?:[.,]\d+)?'
VALID_WIND_DIRS={'N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'}
api=FastAPI(title='Meteo Arbus-Guspini API',version='5.0-table-click')
api.add_middleware(CORSMiddleware,allow_origins=['*'],allow_credentials=False,allow_methods=['GET','HEAD'],allow_headers=['*'])
CACHE_SECONDS=300
_cache={'timestamp':0.0,'data':None}

def to_float(v:Optional[str])->Optional[float]:
    if not v:return None
    m=re.search(NUMBER_RE,v.replace(',','.'))
    if not m:return None
    try:return float(m.group(0))
    except ValueError:return None

def f_to_c(v):return None if v is None else round((v-32)*5/9,1)
def mph_to_kmh(v):return None if v is None else round(v*1.609344,1)
def inhg_to_hpa(v):return None if v is None else round(v*33.8638867,1)
def inches_to_mm(v):return None if v is None else round(v*25.4,1)
def clean(v:str)->str:return re.sub(r'\s+',' ',v.replace('\xa0',' ')).strip()

def dismiss_cookies(page:Page)->None:
    for label in ['Accept All','I Accept','Accept','Agree','Accetta tutto','Accetto']:
        try:
            btn=page.get_by_role('button',name=re.compile(label,re.I))
            if btn.count():btn.first.click(timeout=2000);page.wait_for_timeout(500);return
        except Exception:pass

def click_table_view(page:Page)->None:
    """Apre la vista tabella usando selettori normali e fallback JavaScript."""
    try:
        h=page.get_by_text('Weather History',exact=False).first
        h.scroll_into_view_if_needed(timeout=10000)
        page.wait_for_timeout(1500)
    except Exception:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)

    selectors=[
        'a:has-text("table")',
        'button:has-text("table")',
        '[role="button"]:has-text("table")',
        '[role="tab"]:has-text("table")',
        'span:has-text("table")',
        'li:has-text("table")',
    ]
    for selector in selectors:
        try:
            loc=page.locator(selector)
            for i in range(min(loc.count(),10)):
                item=loc.nth(i)
                txt=clean(item.inner_text(timeout=3000)).lower()
                if txt=='table' or txt.endswith(' table') or txt.startswith('table '):
                    item.scroll_into_view_if_needed(timeout=5000)
                    item.click(timeout=8000,force=True)
                    page.wait_for_timeout(5000)
                    return
        except Exception:
            continue

    clicked=page.evaluate("""
    () => {
      const nodes=Array.from(document.querySelectorAll(
        'a,button,[role="button"],[role="tab"],span,li,div'
      ));
      const candidates=nodes.filter(el => {
        const text=(el.textContent||'').trim().toLowerCase();
        const style=window.getComputedStyle(el);
        const visible=style.display!=='none' &&
          style.visibility!=='hidden' &&
          el.getClientRects().length>0;
        return visible && text==='table';
      });
      if(!candidates.length) return false;
      const el=candidates[candidates.length-1];
      el.scrollIntoView({block:'center'});
      el.click();
      return true;
    }
    """)
    if clicked:
        page.wait_for_timeout(5000)
        return

    body=clean(page.locator('body').inner_text(timeout=10000))
    found=[x for x in body.split(' ') if 'table' in x.lower()][:10]
    raise RuntimeError(f'Vista Table non trovata. Diagnostica: {found}')


def extract_visible_tables(page:Page)->List[List[List[str]]]:
    out=[]; tables=page.locator('table:visible')
    for i in range(tables.count()):
        rows_out=[]; rows=tables.nth(i).locator('tr')
        for r in range(rows.count()):
            cells=rows.nth(r).locator('th, td')
            row=[clean(cells.nth(c).inner_text(timeout=5000)) for c in range(cells.count())]
            if row:rows_out.append(row)
        if rows_out:out.append(rows_out)
    return out

def score_table(rows):
    if not rows:return -1
    head=' | '.join(rows[0]).lower();score=min(len(rows),20)
    for token in ('time','ora','temperature','temperatura','humidity','umid','wind','vento'):
        if token in head:score+=2
    return score

def find_column(headers,patterns):
    low=[h.lower() for h in headers]
    for p in patterns:
        for i,h in enumerate(low):
            if p in h:return i
    return None

def row_value(row,index):return None if index is None or index>=len(row) else row[index]

def parse_temp(cell,header):
    if not cell:return None
    v=to_float(cell)
    if v is None:return None
    unit=(header+' '+cell).upper()
    if '°F' in unit or re.search(r'\bF\b',unit):return f_to_c(v)
    if v>55:return f_to_c(v)
    return round(v,1)

def parse_speed(cell,header):
    if not cell:return None
    v=to_float(cell)
    if v is None:return None
    return mph_to_kmh(v) if 'mph' in (header+' '+cell).lower() else round(v,1)

def parse_pressure(cell,header):
    if not cell:return None
    v=to_float(cell)
    if v is None:return None
    unit=(header+' '+cell).lower()
    return inhg_to_hpa(v) if ('inhg' in unit or re.search(r'\bin\b',unit) or v<100) else round(v,1)

def parse_rain(cell,header):
    if not cell:return None
    v=to_float(cell)
    if v is None:return None
    unit=(header+' '+cell).lower()
    return inches_to_mm(v) if (' in' in unit or 'inch' in unit) else round(v,1)

def parse_history_table(rows):
    if len(rows)<2:raise RuntimeError('Tabella oraria vuota')
    headers=rows[0]; data_rows=[r for r in rows[1:] if len(r)>=2]
    idx_time=find_column(headers,('time','ora','date','data'))
    idx_temp=find_column(headers,('temperature','temp','temperatura'))
    idx_dew=find_column(headers,('dew point','dew','rugiada'))
    idx_hum=find_column(headers,('humidity','humid','umid'))
    idx_wind=find_column(headers,('wind speed','speed','velocità vento','vento'))
    idx_dir=find_column(headers,('wind direction','direction','direzione'))
    idx_gust=find_column(headers,('gust','raffica'))
    idx_pressure=find_column(headers,('pressure','pressione'))
    idx_rain=find_column(headers,('precip','rain','pioggia'))
    if idx_temp is None:raise RuntimeError(f'Colonna temperatura non trovata: {headers}')
    selected=None
    for row in reversed(data_rows):
        t=parse_temp(row_value(row,idx_temp),headers[idx_temp])
        if t is not None and -35<=t<=55:selected=row;break
    if selected is None:raise RuntimeError('Nessuna temperatura plausibile')
    temp=parse_temp(row_value(selected,idx_temp),headers[idx_temp])
    dew=parse_temp(row_value(selected,idx_dew),headers[idx_dew]) if idx_dew is not None else None
    hum=to_float(row_value(selected,idx_hum))
    wind=parse_speed(row_value(selected,idx_wind),headers[idx_wind]) if idx_wind is not None else None
    gust=parse_speed(row_value(selected,idx_gust),headers[idx_gust]) if idx_gust is not None else None
    pressure=parse_pressure(row_value(selected,idx_pressure),headers[idx_pressure]) if idx_pressure is not None else None
    rain=parse_rain(row_value(selected,idx_rain),headers[idx_rain]) if idx_rain is not None else None
    wind_dir=row_value(selected,idx_dir)
    if wind_dir:
        wind_dir=clean(wind_dir).upper()
        if wind_dir not in VALID_WIND_DIRS:wind_dir=None
    warnings=[]
    if hum is not None and not 0<=hum<=100:warnings.append('Umidità fuori intervallo');hum=None
    if dew is not None and temp is not None and dew>temp+0.5:warnings.append('Dew point superiore alla temperatura');dew=None
    if wind is not None and not 0<=wind<=250:warnings.append('Vento fuori intervallo');wind=None
    if gust is not None and not 0<=gust<=300:warnings.append('Raffica fuori intervallo');gust=None
    return {'source':'wunderground_history_table','parser_version':'5.0-table-click','status':'table_read','updated':row_value(selected,idx_time),'age_minutes':None,'stale':None,'temperature_c':temp,'dewpoint_c':dew,'humidity_pct':hum,'wind_dir':wind_dir,'wind_speed_kmh':wind,'wind_gust_kmh':gust,'pressure_hpa':pressure,'rain_today_mm':rain,'selected_row':selected,'headers':headers,'quality_warnings':warnings}

def scrape_station(browser:Browser,code:str,meta:Dict[str,str])->Dict[str,Any]:
    out={'station_code':code,'station_name':meta['name'],'url':meta['url'],'captured_at_utc':datetime.now(timezone.utc).isoformat(),'parser_version':'5.0-table-click','data':None,'errors':[]}
    page=browser.new_page(viewport={'width':1440,'height':2600},locale='en-US',user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36')
    try:
        page.goto(meta['url'],wait_until='domcontentloaded',timeout=90000);dismiss_cookies(page);page.wait_for_timeout(7000);click_table_view(page);page.wait_for_timeout(5000)
        tables=extract_visible_tables(page)
        if not tables:raise RuntimeError('Nessuna tabella HTML visibile')
        out['data']=parse_history_table(max(tables,key=score_table))
    except PlaywrightTimeoutError:out['errors'].append('Timeout durante il caricamento')
    except Exception as exc:out['errors'].append(f'{type(exc).__name__}: {exc}')
    finally:page.close()
    return out

def collect_all():
    results={}
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-dev-shm-usage','--no-sandbox'])
        try:
            for code,meta in STATIONS.items():results[code]=scrape_station(browser,code,meta)
        finally:browser.close()
    return results

@api.get('/')
@api.head('/')
def root():return {'status':'ok','message':'Backend meteo Arbus-Guspini attivo','parser':'Weather History Table v5','endpoint':'/stations'}
@api.get('/health')
@api.head('/health')
def health():return {'status':'ok'}
@api.get('/stations')
def stations(force:bool=False):
    now=time.time()
    if not force and _cache['data'] is not None and now-_cache['timestamp']<CACHE_SECONDS:return {'cached':True,'cache_age_seconds':round(now-_cache['timestamp']),'stations':_cache['data']}
    data=collect_all();_cache['timestamp']=time.time();_cache['data']=data
    return {'cached':False,'cache_age_seconds':0,'stations':data}
