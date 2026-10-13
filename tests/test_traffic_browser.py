"""Новая UI-докупка: устаревший расчёт, двойной клик, ошибка pending."""
import subprocess
import unittest
from pathlib import Path


class TrafficBrowserTests(unittest.TestCase):
    def test_stale_quote_does_not_override_quantity_and_double_click_creates_one_invoice(self):
        script=r'''
const assert=require('assert/strict'),vm=require('vm'),fs=require('fs');
const element=()=>({dataset:{},handlers:{},disabled:false,textContent:'',addEventListener(name,handler){this.handlers[name]=handler;}});
const box=element(),pay=element(),notice=element(),gb=element();
box.dataset={trafficAvailable:'10',trafficPackGb:'100'};
const nodes={buyTrafficBox:box,buyTrafficBtn:pay,trafficPurchaseNotice:notice,buyTrafficGb:gb};
let first,quotes=0,created=0,failCreate=false,lastPayload;
const result=(data,ok=true)=>({ok,json:async()=>data});
const location={pathname:'/profile/',href:''};
const context=vm.createContext({window:{},location,confirm:()=>true,
 document:{getElementById:id=>nodes[id]||null,querySelectorAll:()=>[]},
 fetch:async(path,options)=>{
  const payload=JSON.parse(options.body);
  if(path.endsWith('/quote')) {
   quotes++;if(quotes===1)return new Promise(resolve=>{first=()=>resolve(result({final_amount:49,total_limit_gb:600}));});
   return result({final_amount:98,total_limit_gb:700});
  }
  created++;lastPayload=payload;
  return failCreate?result({detail:'У вас уже есть неоплаченный счёт'},false):result({final_amount:98,payment_url:'https://example.com/pay'});
 }});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 box.handlers.click({target:{closest:()=>({dataset:{buyTraffic:'1'}})}});await tick();
 assert.equal(pay.textContent,'Докупить за 98.00 ₽');assert.equal(pay.disabled,false);
 first();await tick();assert.equal(pay.textContent,'Докупить за 98.00 ₽');
 await Promise.all([pay.handlers.click(),pay.handlers.click()]);
 assert.equal(created,1);assert.equal(lastPayload.packs,2);assert.equal(lastPayload.source,'web');
 assert.equal(location.href,'https://example.com/pay');
 failCreate=true;await pay.handlers.click();
 assert.match(notice.textContent,/неоплаченный счёт/);assert.equal(pay.disabled,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',script,str(Path(__file__).resolve().parents[1]/'webapp/static/js/traffic.js')],
                              capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
