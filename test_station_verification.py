"""Offline fixtures only: never write these test observations to production."""
import copy, os, pathlib, unittest
from datetime import datetime, timedelta
from unittest.mock import Mock
from station_verification import (ROME, finite, station_plan, verify_independent,
                                  record_verification_meta, finalize_verification_state)
NOW=datetime(2026,10,12,14,3,tzinfo=ROME)
VALUE='2026-10-12T14:00'
RUN='2026-10-12_1400'

def source(temp=22.4, when=NOW-timedelta(minutes=3)):
    return {'captured_at_utc':NOW.isoformat(),'data':{
        'temperature_c':temp,'epoch':when.timestamp(),'humidity_pct':78,
        'dewpoint_c':18.3,'pressure_hpa':1015.6,'wind_speed_kmh':5.2,
        'rain_rate_mm_h':0,'rain_today_mm':0,'rain_6h_mm':None}}

def payload(arbus=None,guspini=None):
    return {'stations':{'IARBUS7':arbus or {'data':None},'IGUSPI1':guspini or {'data':None}}}

def forecast(place,identifier):
    return {'id':identifier,'place':place,'validTime':VALUE,'issueTime':'2026-10-12T10:31',
            'verified':False,'gfs':22.,'ecmwf':23.,'baseCorrected':22.5,
            'localForecast':22.7,'state':'Stabile','leadHours':3.48333,
            'localCorrection':.2,'forecastRainInterval':.3}

class Core(unittest.TestCase):
    def test_numeric_missing_not_zero(self):
        for value in [None,'',True,False,float('nan'),float('inf'),[],{}]:self.assertFalse(finite(value))
        self.assertTrue(finite(0))
    def test_guspini_independent(self):
        p=station_plan(payload(guspini=source()),VALUE,NOW)
        self.assertEqual(p['arbus']['status'],'unavailable');self.assertEqual(p['guspini']['status'],'ready')
    def test_arbus_independent(self):
        p=station_plan(payload(arbus=source()),VALUE,NOW)
        self.assertEqual(p['arbus']['status'],'ready');self.assertEqual(p['guspini']['status'],'unavailable')
    def test_old_measurement_rejected(self):
        p=station_plan(payload(guspini=source(when=NOW-timedelta(hours=2))),VALUE,NOW)
        self.assertEqual(p['guspini']['status'],'unavailable')
    def test_future_measurement_rejected(self):
        p=station_plan(payload(guspini=source(when=NOW+timedelta(minutes=10))),VALUE,NOW)
        self.assertEqual(p['guspini']['status'],'unavailable')
    def test_capture_time_is_not_measurement_time(self):
        s=source();s['data'].pop('epoch')
        p=station_plan(payload(guspini=s),VALUE,NOW)
        self.assertEqual(p['guspini']['status'],'unavailable')
    def test_no_retrospective_or_future_verification(self):
        for value in ['2026-10-12T08:00','2026-10-12T20:00']:
            with self.assertRaisesRegex(RuntimeError,'VERIFY_TIME_WINDOW_FAIL'):
                station_plan(payload(guspini=source()),value,NOW)
    def test_input_is_unchanged(self):
        s=payload(guspini=source());old=copy.deepcopy(s);station_plan(s,VALUE,NOW);self.assertEqual(s,old)
    def test_partial_not_marked_complete(self):
        old={'completedRuns':['2026-10-10_0800'],'lastSuccessAt':'original'}
        m=record_verification_meta(old,RUN,{'status':'partial'})
        self.assertNotIn(RUN,m['completedRuns']);self.assertIn(RUN,m['partialRuns']);self.assertEqual(m['lastSuccessAt'],'original')
    def test_preserve_unrelated_cloud_state(self):
        old={'archive':[],'history':[],'rain_events':[1],'settings':{'a':1}}
        fresh={**old,'rain_events':[1,2],'notification_sent_keys':['new']}
        out=finalize_verification_state(old,old,fresh,{'status':'partial'},RUN,Mock())
        self.assertEqual(out['rain_events'],[1,2]);self.assertEqual(out['notification_sent_keys'],['new'])
    def test_conflicting_cloud_update_rejected(self):
        old={'archive':[],'history':[]}
        with self.assertRaisesRegex(RuntimeError,'VERIFY_CLOUD_CONFLICT'):
            finalize_verification_state(old,old,{**old,'history':[1]},{'status':'partial'},RUN,Mock())

class Browser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw=sync_playwright().start()
        exe=os.environ.get('TEST_CHROMIUM_EXECUTABLE')
        cls.browser=cls.pw.chromium.launch(headless=True,args=['--no-sandbox'],**({'executable_path':exe} if exe else {}))
        cls.html=pathlib.Path(os.environ.get('TEST_FRONTEND_HTML','frontend.html')).read_text()
        start='\nregisterPwaServiceWorker();\nrenderPwaState();\n';end='\n</script>\n</body>'
        assert cls.html.count(start)==1 and cls.html.count(end)==1
        cls.html=cls.html.replace(start,'\nif(false){'+start,1).replace(end,'\n}'+end,1)
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.context=self.browser.new_context(timezone_id='Europe/Rome')
        self.page=self.context.new_page()
        self.page.route('**/*',lambda r:r.abort())
        self.page.evaluate("() => { const store=new Map(); Object.defineProperty(window,'localStorage',{value:{getItem:k=>store.has(k)?store.get(k):null,setItem:(k,v)=>store.set(k,String(v)),removeItem:k=>store.delete(k)}, configurable:true}); }")
        self.page.set_content(self.html,wait_until='domcontentloaded')
        self.a=[forecast('Arbus','a1'),forecast('Guspini','g1')]
        self.h=[{'when':'2026-10-10T08:00','source':'archive','place':'Arbus','obs':20,'legacy':'unchanged'}]
        self.seed(self.a,self.h)
    def tearDown(self):self.context.close()
    def seed(self,a,h):
        self.page.evaluate("([a,h])=>{localStorage.setItem('meteoForecastArchiveV1',JSON.stringify(a));localStorage.setItem('meteoHistoryV5',JSON.stringify(h));}",[a,h])
    def state(self):
        return self.page.evaluate("()=>({archive:JSON.parse(localStorage.getItem('meteoForecastArchiveV1')),history:JSON.parse(localStorage.getItem('meteoHistoryV5'))})")
    def run_verify(self,p):return verify_independent(self.page,VALUE,p,RUN,NOW)
    def test_guspini_archived_arbus_unchanged(self):
        report=self.run_verify(payload(guspini=source()))
        s=self.state();self.assertEqual(report['status'],'partial')
        self.assertEqual(s['archive'][0],self.a[0]);self.assertTrue(s['archive'][1]['verified'])
        self.assertEqual(s['history'][1:],self.h);self.assertEqual(s['history'][0]['obs'],22.4)
        self.assertEqual(s['history'][0]['observationStationCode'],'IGUSPI1')
        self.assertEqual(s['history'][0]['archiveId'],'g1')
        self.assertEqual(s['history'][0]['corrected'],self.a[1]['localForecast'])
    def test_arbus_archived_guspini_unchanged(self):
        report=self.run_verify(payload(arbus=source()))
        s=self.state();self.assertEqual(report['verifiedPlaces'],['Arbus'])
        self.assertEqual(s['archive'][1],self.a[1]);self.assertEqual(s['history'][0]['place'],'Arbus')
    def test_both_available(self):
        r=self.run_verify(payload(source(),source()));s=self.state()
        self.assertEqual(r['status'],'complete');self.assertEqual(len(s['history']),len(self.h)+2)
    def test_both_unavailable_no_changes(self):
        before=self.state();r=self.run_verify(payload())
        self.assertEqual(r['status'],'unavailable');self.assertEqual(self.state(),before)
    def test_zero_temperature_is_real(self):
        self.run_verify(payload(guspini=source(temp=0)))
        self.assertEqual(self.state()['history'][0]['obs'],0)
    def test_stale_dom_values_are_cleared(self):
        self.page.evaluate("()=>{document.getElementById('arbus-obs-temp').value='33';document.getElementById('arbus-obs-rain-interval').value='4';document.getElementById('arbus-event-rain').value='5';}")
        self.run_verify(payload(guspini=source()))
        self.assertEqual(self.state()['archive'][0],self.a[0]);self.assertEqual(len(self.state()['history']),len(self.h)+1)
    def test_missing_forecast_for_arbus_does_not_block_guspini(self):
        self.seed(self.a[1:],self.h);r=self.run_verify(payload(source(),source()))
        self.assertEqual(r['status'],'partial');self.assertEqual(r['stations']['arbus']['status'],'no_archived_forecast')
    def test_future_issue_time_rejected_per_place(self):
        self.a[0]['issueTime']='2026-10-12T14:01';self.seed(self.a,self.h)
        r=self.run_verify(payload(source(),source()));self.assertEqual(r['verifiedPlaces'],['Guspini'])
    def test_idempotent_retry(self):
        self.run_verify(payload(guspini=source()));before=self.state()
        r=self.run_verify(payload(guspini=source()));self.assertEqual(self.state(),before)
        self.assertEqual(r['newComparisons'],0)
    def test_stale_measurement_not_saved(self):
        before=self.state();r=self.run_verify(payload(guspini=source(when=NOW-timedelta(hours=2))))
        self.assertEqual(self.state(),before);self.assertEqual(r['status'],'unavailable')
    def test_unrelated_forecast_preserved(self):
        other=forecast('Guspini','g2');other['validTime']='2026-10-12T20:00'
        self.a.append(other);self.seed(self.a,self.h);self.run_verify(payload(guspini=source()))
        self.assertEqual(self.state()['archive'][2],other)
    def test_duplicate_archive_ids_rejected_without_write(self):
        self.a.append(copy.deepcopy(self.a[1]));self.seed(self.a,self.h);before=self.state()
        with self.assertRaises(Exception):self.run_verify(payload(guspini=source()))
        self.assertEqual(self.state(),before)
    def test_old_verified_without_history_rejected(self):
        self.a[0]['verified']=True;self.seed(self.a,self.h);before=self.state()
        with self.assertRaises(Exception):self.run_verify(payload(guspini=source()))
        self.assertEqual(self.state(),before)

if __name__=='__main__':unittest.main()
