import http from "node:http";

const PORT = Number(process.env.PORT || 10000);
const UPSTREAM = "https://prisma-vision-web-551077264693.europe-west1.run.app/api/vision/web-detect";
const ALLOWED = new Set([
  "https://medio-campidano-negozi.onrender.com",
  "http://localhost:8000",
  "http://127.0.0.1:8000"
]);
const MAX_BODY = 9_500_000;

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

const server=http.createServer(async(req,res)=>{
  const origin=cors(req);
  const url=new URL(req.url||"/","http://localhost");
  if(req.method==="OPTIONS") return send(res,204,"",origin,"text/plain");
  if(req.method==="GET" && url.pathname==="/health"){
    return send(res,200,JSON.stringify({ok:true,service:"medio-campidano-vision"}),origin);
  }
  if(req.method!=="POST" || url.pathname!=="/api/vision"){
    return send(res,404,JSON.stringify({error:"not_found"}),origin);
  }
  try{
    const body=await readBody(req);
    const upstream=await fetch(UPSTREAM,{
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
server.listen(PORT,"0.0.0.0",()=>console.log("vision proxy listening on",PORT));