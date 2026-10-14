(() => {
  const API = "https://medio-campidano-vision.onrender.com/api/prices";
  const q = document.querySelector("#q");
  const town = document.querySelector("#t");
  const filters = document.querySelector(".filters");
  if (!q || !town || !filters) return;

  const style = document.createElement("style");
  style.textContent = `
    .priceBox{background:#fff;border:1px solid #dfe6e3;border-radius:16px;padding:15px;margin:14px 0 18px;box-shadow:0 5px 18px rgba(15,118,110,.055)}
    .priceHead{display:flex;gap:10px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}
    .priceTitle{font-size:17px;font-weight:850}
    .priceHelp{font-size:12px;color:#66716f;margin-top:3px;line-height:1.4}
    .priceStatus{font-size:13px;color:#43514e;margin-top:10px}
    .priceGrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:12px}
    .offer{border:1px solid #e0e7e4;border-radius:13px;padding:12px;display:flex;flex-direction:column;gap:5px}
    .retailer{font-size:12px;font-weight:850;color:#0f766e;text-transform:uppercase}
    .offerName{font-weight:800;line-height:1.3}
    .money{font-size:23px;font-weight:900;color:#17201e}
    .old{font-size:13px;color:#7a8582;text-decoration:line-through;margin-left:6px;font-weight:500}
    .offerMeta{font-size:12px;color:#596562;line-height:1.4}
    .stores{background:#eef8f5;border-radius:9px;padding:7px 8px;font-size:12px;color:#315b54}
    .src{margin-top:auto;display:inline-block;color:#0f766e;font-weight:750;text-decoration:none;font-size:12px}
    .flyers{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
    .flyer{font-size:12px;text-decoration:none;background:#f1f5f4;color:#27413c;padding:7px 9px;border-radius:999px;border:1px solid #dde6e3}
    .priceNote{margin-top:10px;background:#fff7ed;color:#8a4b08;border-radius:10px;padding:8px 10px;font-size:12px;line-height:1.4}
    @media(max-width:700px){.priceGrid{grid-template-columns:1fr}.money{font-size:21px}}
  `;
  document.head.appendChild(style);

  const box = document.createElement("section");
  box.className = "priceBox";
  box.innerHTML = `
    <div class="priceHead">
      <div>
        <div class="priceTitle">💶 Prezzi e offerte ufficiali online</div>
        <div class="priceHelp">Cerco il prodotto sulle pagine ufficiali delle catene presenti nel Medio Campidano e collego il risultato ai punti vendita locali.</div>
      </div>
    </div>
    <div id="priceStatus" class="priceStatus">Scrivi un prodotto o fotografalo per cercare eventuali prezzi pubblicati online.</div>
    <div id="priceResults"></div>
  `;
  const photo = document.querySelector(".photoBox");
  if (photo) photo.insertAdjacentElement("afterend", box);
  else filters.parentNode.insertBefore(box, filters);

  const status = box.querySelector("#priceStatus");
  const results = box.querySelector("#priceResults");
  let timer = null;
  let serial = 0;
  let lastQuery = "";
  let lastWasPhoto = false;

  const esc = s => String(s ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
  const euro = n => Number(n).toLocaleString("it-IT",{style:"currency",currency:"EUR"});
  const unique = arr => [...new Set((arr||[]).filter(Boolean))];

  function render(data, query, wasPhoto){
    const offers = data?.offers || [];
    const sources = data?.sources || [];
    if (!offers.length){
      status.innerHTML = `Nessun <b>prezzo strutturato</b> trovato online per “${esc(query)}”${town.value ? " a " + esc(town.value) : ""}.`;
      const links = sources.filter(x => x?.url && x?.localStores?.length);
      results.innerHTML = links.length ? `
        <div class="priceNote">Alcune catene pubblicano il volantino ma non rendono sempre il singolo prezzo facilmente leggibile in automatico. Puoi comunque aprire le fonti ufficiali:</div>
        <div class="flyers">${links.map(s=>`<a class="flyer" target="_blank" rel="noopener" href="${esc(s.url)}">${esc(s.retailer)} · offerte ufficiali</a>`).join("")}</div>
      ` : "";
      return;
    }
    status.innerHTML = `Trovate <b>${offers.length}</b> corrispondenze di prezzo ufficiali per “${esc(query)}”${town.value ? " nei negozi/catene presenti a " + esc(town.value) : ""}.`;
    results.innerHTML = `
      <div class="priceGrid">
        ${offers.map(o => `
          <article class="offer">
            <div class="retailer">${esc(o.retailer)}</div>
            <div class="offerName">${esc(o.title)}</div>
            <div><span class="money">${euro(o.price)}</span>${o.oldPrice ? `<span class="old">${euro(o.oldPrice)}</span>` : ""}</div>
            ${o.package ? `<div class="offerMeta">Formato: ${esc(o.package)}</div>` : ""}
            ${o.validity ? `<div class="offerMeta">Validità: ${esc(o.validity)}</div>` : ""}
            <div class="stores">${unique(o.localStores).map(s=>"📍 "+esc(s)).join("<br>")}</div>
            <a class="src" target="_blank" rel="noopener" href="${esc(o.url)}">Apri la fonte ufficiale ↗</a>
          </article>
        `).join("")}
      </div>
      <div class="priceNote">Il prezzo è quello pubblicato online dalla catena. La disponibilità della specifica referenza nel singolo punto vendita e l’applicabilità locale della promozione vanno confermate in negozio. Le offerte con tessera/app possono richiedere condizioni aggiuntive.</div>
    `;
  }

  async function search(query, wasPhoto=false){
    query = String(query||"").trim();
    if (query.length < 2){
      status.textContent = "Scrivi un prodotto o fotografalo per cercare eventuali prezzi pubblicati online.";
      results.innerHTML = "";
      return;
    }
    lastQuery = query;
    lastWasPhoto = wasPhoto;
    const mine = ++serial;
    status.innerHTML = `Cerco prezzi ufficiali per <b>“${esc(query)}”</b>…`;
    results.innerHTML = "";
    try{
      const url = API + "?q=" + encodeURIComponent(query) + (town.value ? "&town=" + encodeURIComponent(town.value) : "");
      const r = await fetch(url,{headers:{accept:"application/json"}});
      if(!r.ok) throw new Error("HTTP "+r.status);
      const data = await r.json();
      if (mine !== serial) return;
      render(data, query, wasPhoto);
    }catch(e){
      if (mine !== serial) return;
      status.textContent = "Ricerca prezzi temporaneamente non disponibile. La ricerca dei negozi continua a funzionare.";
      results.innerHTML = "";
    }
  }

  q.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => search(q.value,false), 900);
  });
  town.addEventListener("change", () => {
    if(lastQuery) search(lastQuery,lastWasPhoto);
    else if(q.value.trim().length>=2) search(q.value,false);
  });

  function watchPhotoState(){
    const ps = document.querySelector("#photoState");
    if(!ps || ps.dataset.priceWatched) return;
    ps.dataset.priceWatched = "1";
    const inspect = () => {
      const text = (ps.innerText || ps.textContent || "").trim();
      if(!/^Prodotto riconosciuto/i.test(text)) return;
      const lines = text.split(/\n+/).map(x=>x.trim()).filter(Boolean);
      if(lines.length < 2) return;
      let label = lines[1]
        .replace(/\d+\s+punt[oi]\s+vendita.*$/i,"")
        .replace(/Codice\s+\d+.*$/i,"")
        .trim();
      if(label.length>=2) search(label,true);
    };
    new MutationObserver(inspect).observe(ps,{subtree:true,childList:true,characterData:true});
    inspect();
  }
  watchPhotoState();
  const bodyObserver = new MutationObserver(watchPhotoState);
  bodyObserver.observe(document.body,{subtree:true,childList:true});
})();