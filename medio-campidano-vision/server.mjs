import http from "node:http";

const PORT = Number(process.env.PORT || 10000);
const VISION_UPSTREAM = "https://prisma-vision-web-551077264693.europe-west1.run.app/api/vision/web-detect";
const ALLOWED = new Set([
  "https://medio-campidano-negozi.onrender.com",
  "http://localhost:8000",
  "http://127.0.0.1:8000"
]);
const MAX_BODY = 9_500_000;
const UA = "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/141 Safari/537.36";
const priceCache = new Map();

const RETAILERS = [
  {
    id:"lidl", name:"Lidl", domains:["lidl.it"],
    seeds:[
      "https://www.lidl.it/c/offerte-settimanali-kw-26/",
      "https://www.lidl.it/c/natale/",
      "https://www.lidl.it/h/cereali-e-creme-spalmabili/h10096153",
      "https://www.lidl.it/h/dolci-e-snack/h10096205"
    ],
    localStores:{
      "Guspini":["Lidl — Via Parigi 1"],
      "San Gavino Monreale":["Lidl — Via Gaetano Donizetti 99"],
      "Sanluri":["Lidl — Via Parma 13"]
    }
  },
  {
    id:"eurospin", name:"Eurospin", domains:["eurospin.it"],
    seeds:[
      "https://www.eurospin.it/promozioni/",
      "https://www.eurospin.it/ribassati/"
    ],
    localStores:{
      "Sanluri":["Eurospin — Via Firenze 3"],
      "Serramanna":["Eurospin — Località Is Argiddas"],
      "Villacidro":["Eurospin — Via per San Gavino Monreale 138/B"]
    }
  },
  {
    id:"md", name:"MD", domains:["mdspa.it"],
    seeds:["https://www.mdspa.it/volantino/"],
    localStores:{
      "Serramanna":["MD — Serramanna"],
      "Villamar":["MD Discount — Via Roma 254"]
    }
  },
  {
    id:"conad", name:"Conad", domains:["conad.it"],
    seeds:["https://www.conad.it/"],
    localStores:{
      "Barumini":["Conad — SS197 km 43,800"],
      "Villamar":["Conad — Via Roma 213"]
    }
  },
  {
    id:"nonnaisa", name:"Nonna Isa", domains:["nonnaisa.com"],
    seeds:["https://www.nonnaisa.com/volantino/"],
    localStores:{
      "Arbus":["Supermercati Nonna Isa — Arbus"],
      "Guspini":["Supermercati Nonna Isa — Via XXV Aprile"],
      "San Gavino Monreale":["Market Nonna Isa — San Gavino Monreale"],
      "Sardara":["Super Nonna Isa — Via Cagliari"],
      "Villacidro":["Nonna Isa — più punti vendita a Villacidro"]
    }
  },
  {
    id:"superpan", name:"SuperPAN", domains:["supermercatipan.it"],
    seeds:["https://www.supermercatipan.it/"],
    localStores:{
      "Gonnosfanadiga":["Super Pan — Via Nazionale / Via Grazia Deledda"],
      "Sanluri":["Superpan — Sanluri"]
    }
  },
  {
    id:"coop", name:"Coop Sardegna", domains:["coopsardegna.it"],
    seeds:[
      "https://www.coopsardegna.it/volantino-coop/",
      "https://www.coopsardegna.it/vol-coop-super/",
      "https://www.coopsardegna.it/vol-incoop/"
    ],
    localStores:{
      "Pabillonis":["Coop Sardegna — Pabillonis"],
      "Serrenti":["Coop Sardegna — Via Lenin"]
    }
  },
  {
    id:"crai", name:"CRAI", domains:["crai-supermercati.it"],
    seeds:["https://www.crai-supermercati.it/"],
    localStores:{
      "Arbus":["Market CRAI — Via G. Mazzini 73/75"],
      "Barumini":["CRAI — Barumini"],
      "Furtei":["CRAI — Via Circonvallazione 1"],
      "Guspini":["CRAI — Via Torino 18"],
      "Samassi":["CRAI — Via Sanluri 10"],
      "Serramanna":["CRAI — Serramanna"]
    }
  }
];

function cors(req) {
  const origin = String(req.headers.origin || "");
  return ALLOWED.has(origin) ? origin : "https://medio-campidano-negozi.onrender.com";
}
function send(res,status,body,origin,contentType="application/json; charset=utf-8"){
  res.writeHead(status,{
    "content-type":contentType,
    "cache-control":"no-store",
    "access-control-allow-origin":origin,
    "access-control-allow-methods":"GET,POST,OPTIONS",
    "access-control-allow-headers":"content-type",
    "vary":"Origin",
    "x-content-type-options":"nosniff"
  });
  res.end(body);
}
async function readBody(req){
  let total=0; const chunks=[];
  for await (const chunk of req){
    total += chunk.length;
    if(total > MAX_BODY) throw new Error("BODY_TOO_LARGE");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}
function decodeEntities(s){
  return String(s||"")
    .replace(/&nbsp;/gi," ")
    .replace(/&amp;/gi,"&")
    .replace(/&quot;/gi,'"')
    .replace(/&#39;|&apos;/gi,"'")
    .replace(/&euro;/gi,"€")
    .replace(/&agrave;/gi,"à").replace(/&egrave;/gi,"è").replace(/&eacute;/gi,"é")
    .replace(/&igrave;/gi,"ì").replace(/&ograve;/gi,"ò").replace(/&ugrave;/gi,"ù")
    .replace(/&#(\d+);/g,(_,n)=>{try{return String.fromCodePoint(Number(n))}catch{return " "}});
}
function norm(s){
  return decodeEntities(s).toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g,"")
    .replace(/[^a-z0-9]+/g," ").replace(/\s+/g," ").trim();
}
function queryTokens(q){
  const stop=new Set(["il","lo","la","i","gli","le","un","uno","una","di","da","in","con","per","e","o","the","and","prodotto","prezzo"]);
  return norm(q).split(" ").filter(x=>x.length>=3&&!stop.has(x)).slice(0,6);
}
async function fetchText(url, timeout=10000){
  const r=await fetch(url,{
    headers:{
      "user-agent":UA,
      "accept":"text/html,application/xhtml+xml,application/json;q=0.8,*/*;q=0.5",
      "accept-language":"it-IT,it;q=0.9,en;q=0.4"
    },
    redirect:"follow",
    signal:AbortSignal.timeout(timeout)
  });
  if(!r.ok) throw new Error("HTTP_"+r.status);
  const type=String(r.headers.get("content-type")||"");
  if(!type.includes("text")&&!type.includes("json")&&!type.includes("html")) throw new Error("UNSUPPORTED");
  const raw=await r.text();
  return raw.length>2_500_000?raw.slice(0,2_500_000):raw;
}
function visibleLines(html){
  const cleaned=String(html||"")
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi," ")
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi," ")
    .replace(/<(br|\/p|\/div|\/li|\/h1|\/h2|\/h3|\/h4|\/section|\/article|\/tr|\/td)>/gi,"\n")
    .replace(/<[^>]+>/g," ");
  return decodeEntities(cleaned).split(/\n+/).map(x=>x.replace(/\s+/g," ").trim()).filter(x=>x.length>1&&x.length<500);
}
function scoreLine(line,tokens){
  const n=norm(line);
  if(!n) return 0;
  let hits=0;
  for(const t of tokens) if(n.includes(t)) hits++;
  return hits/(tokens.length||1);
}
function parsePriceCandidates(text){
  const out=[];
  const re=/(\d{1,4}(?:[.,]\d{1,2})?)\s*€/g;
  let m;
  while((m=re.exec(text))){
    const val=Number(m[1].replace(",","."));
    if(!Number.isFinite(val)||val<=0||val>50000) continue;
    const around=text.slice(Math.max(0,m.index-28),Math.min(text.length,m.index+m[0].length+28)).toLowerCase();
    const unit=/\/(kg|l|lt|100\s*g|100\s*ml)|1\s*(kg|l)\s*=|al\s*kg|al\s*litro|per\s*kg/.test(around);
    out.push({value:val,raw:m[0],unit});
  }
  return out;
}
function extractValidity(text){
  const patterns=[
    /(offerte? valid[ea][^\n.]{0,70})/i,
    /((?:solo )?in punto vendita dal\s+\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\s+(?:al|a)\s+\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)/i,
    /(dal\s+\d{1,2}\s+(?:al|a)\s+\d{1,2}\s+[A-Za-zÀ-ÿ]+\s+20\d{2})/i,
    /(\d{1,2}[./-]\d{1,2}\s*-\s*\d{1,2}[./-]\d{1,2})/i
  ];
  for(const re of patterns){const m=text.match(re); if(m) return m[1].replace(/\s+/g," ").trim()}
  return "";
}
function extractPackage(text){
  const m=text.match(/\b(?:\d+\s*x\s*)?\d+(?:[.,]\d+)?\s*(?:kg|g|gr|l|lt|ml|cl|pz|pezzi)\b/i);
  return m?m[0].replace(/\s+/g," "):"";
}
function extractOffersFromHtml(retailer,url,html,q){
  const tokens=queryTokens(q);
  if(!tokens.length) return [];
  const lines=visibleLines(html);
  const out=[];
  for(let i=0;i<lines.length;i++){
    const sc=scoreLine(lines[i],tokens);
    if(sc<Math.min(1, tokens.length===1?1:0.5)) continue;
    const lo=Math.max(0,i-2), hi=Math.min(lines.length,i+7);
    const chunk=lines.slice(lo,hi).join(" · ");
    const prices=parsePriceCandidates(chunk).filter(x=>!x.unit);
    if(!prices.length) continue;
    const current=Math.min(...prices.map(x=>x.value));
    const higher=prices.map(x=>x.value).filter(v=>v>current+0.001&&v<current*4).sort((a,b)=>a-b);
    const old=higher[0]||null;
    let title=lines[i].replace(/\s+/g," ").trim();
    if(title.length>140) title=title.slice(0,140);
    if(title.length<3) continue;
    const key=retailer.id+"|"+norm(title)+"|"+current;
    if(out.some(x=>x._key===key)) continue;
    out.push({
      _key:key,
      retailer:retailer.name,
      retailerId:retailer.id,
      title,
      price:current,
      oldPrice:old,
      package:extractPackage(chunk),
      validity:extractValidity(chunk)||extractValidity(lines.slice(Math.max(0,i-30),Math.min(lines.length,i+30)).join(" ")),
      url,
      official:true,
      matchScore:sc
    });
    if(out.length>=6) break;
  }
  return out;
}
function officialLink(url,domains){
  try{
    const u=new URL(url);
    const h=u.hostname.replace(/^www\./,"");
    return domains.some(d=>h===d||h.endsWith("."+d));
  }catch{return false}
}
async function discover(retailer,q){
  const found=[...retailer.seeds];
  const domain=retailer.domains[0];
  const query='site:'+domain+' "'+q.replace(/"/g,"")+'" prezzo offerte';
  try{
    const html=await fetchText("https://html.duckduckgo.com/html/?q="+encodeURIComponent(query),8000);
    const re=/class="result__a"[^>]*href="([^"]+)"/gi;
    let m;
    while((m=re.exec(html))&&found.length<9){
      let href=decodeEntities(m[1]);
      const uddg=href.match(/[?&]uddg=([^&]+)/);
      if(uddg){try{href=decodeURIComponent(uddg[1])}catch{}}
      if(officialLink(href,retailer.domains)&&!found.includes(href)) found.push(href.split("#")[0]);
    }
  }catch{}
  return found.slice(0,9);
}
function retailerAvailable(retailer,town){
  if(!town) return Object.values(retailer.localStores).flat();
  return retailer.localStores[town]||[];
}
async function searchRetailer(retailer,q,town){
  const localStores=retailerAvailable(retailer,town);
  if(!localStores.length) return {offers:[],source:null};
  const pages=await discover(retailer,q);
  let offers=[];
  for(const page of pages){
    try{
      const html=await fetchText(page,9000);
      const parsed=extractOffersFromHtml(retailer,page,html,q)
        .map(x=>({...x,localStores}));
      offers.push(...parsed);
      if(offers.length>=5) break;
    }catch{}
  }
  offers.sort((a,b)=>(b.matchScore-a.matchScore)||(a.price-b.price));
  const dedup=[];
  const seen=new Set();
  for(const x of offers){
    const k=norm(x.title)+"|"+x.price;
    if(seen.has(k)) continue;
    seen.add(k); dedup.push(x);
    if(dedup.length>=4) break;
  }
  return {
    offers:dedup,
    source:{
      retailer:retailer.name,
      url:pages[0]||retailer.seeds[0],
      localStores,
      hasParsedPrices:dedup.length>0
    }
  };
}
async function searchPrices(q,town){
  const key=norm(q)+"|"+String(town||"");
  const hit=priceCache.get(key);
  if(hit&&Date.now()-hit.at<10*60*1000) return hit.value;
  const active=RETAILERS.filter(r=>retailerAvailable(r,town).length);
  const settled=await Promise.allSettled(active.map(r=>searchRetailer(r,q,town)));
  let offers=[]; const sources=[];
  for(const s of settled){
    if(s.status!=="fulfilled") continue;
    offers.push(...s.value.offers);
    if(s.value.source) sources.push(s.value.source);
  }
  offers.sort((a,b)=>a.price-b.price);
  const value={
    query:q,
    town:town||"",
    offers:offers.slice(0,20),
    sources,
    generatedAt:new Date().toISOString(),
    note:"Prezzi ricavati da pagine ufficiali pubbliche. La disponibilità nel singolo punto vendita va verificata; volantini e promozioni possono avere validità territoriale o temporale."
  };
  priceCache.set(key,{at:Date.now(),value});
  if(priceCache.size>80){
    const oldest=[...priceCache.entries()].sort((a,b)=>a[1].at-b[1].at).slice(0,20);
    for(const [k] of oldest) priceCache.delete(k);
  }
  return value;
}

const server=http.createServer(async(req,res)=>{
  const origin=cors(req);
  const url=new URL(req.url||"/","http://localhost");
  if(req.method==="OPTIONS") return send(res,204,"",origin,"text/plain");
  if(req.method==="GET" && url.pathname==="/health"){
    return send(res,200,JSON.stringify({ok:true,service:"medio-campidano-vision-prices",retailers:RETAILERS.length}),origin);
  }
  if(req.method==="GET" && url.pathname==="/api/prices"){
    const q=String(url.searchParams.get("q")||"").trim().slice(0,120);
    const town=String(url.searchParams.get("town")||"").trim().slice(0,80);
    if(q.length<2) return send(res,400,JSON.stringify({error:"query_too_short"}),origin);
    try{
      const result=await searchPrices(q,town);
      return send(res,200,JSON.stringify(result),origin);
    }catch(error){
      return send(res,502,JSON.stringify({error:"price_search_failed"}),origin);
    }
  }
  if(req.method!=="POST" || url.pathname!=="/api/vision"){
    return send(res,404,JSON.stringify({error:"not_found"}),origin);
  }
  try{
    const body=await readBody(req);
    const upstream=await fetch(VISION_UPSTREAM,{
      method:"POST",
      headers:{"content-type":"application/json","accept":"application/json"},
      body,
      signal:AbortSignal.timeout(25000)
    });
    const raw=await upstream.text();
    return send(res,upstream.status,raw,origin);
  }catch(error){
    const status=error?.message==="BODY_TOO_LARGE"?413:502;
    return send(res,status,JSON.stringify({error:status===413?"image_too_large":"vision_proxy_failed"}),origin);
  }
});
server.listen(PORT,"0.0.0.0",()=>console.log("medio campidano vision + prices listening on",PORT));
