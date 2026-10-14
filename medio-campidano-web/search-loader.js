(() => {
  if (typeof S === "undefined" || typeof N === "undefined") return;
  const qEl=document.querySelector("#q"), townEl=document.querySelector("#t"),
        catEl=document.querySelector("#c"), grid=document.querySelector("#g"),
        count=document.querySelector("#num");
  if(!qEl||!townEl||!catEl||!grid||!count) return;

  const esc=s=>String(s??"").replace(/[&<>"]/g,x=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[x]));

  // Product families: a branded query such as "succo Bravo" uses the known
  // product word ("succo") to select suitable physical shops while keeping the
  // full query available to the official-price module.
  const INTENTS=[
    {re:/\b(succo|succhi|nettare|bevanda|bibita|bibite|aranciata|cola|tonica|acqua|latte|caffe|caffè|te|tè|the|cacao|cioccolat|biscott|merend|cracker|snack|patatin|pasta|riso|farina|zucchero|sale|olio|aceto|passata|pomodor|conserv|tonno|legumi|cereal|marmellat|confettur|nutella|crema spalmabile|formaggi|mozzarella|yogurt|burro|uova|gelato|surgelat|pizza|pane|panino|detersiv|candeggina|ammorbidente|carta igienica)\b/i,
     cat:/alimentari|supermercato|discount|minimarket|surgelati|spesa/i,label:"Alimentari e supermercati"},
    {re:/\b(carne|pollo|tacchino|maiale|manzo|vitello|agnello|salsiccia|bistecca|hamburger|salame|prosciutto|mortadella|salumi)\b/i,
     cat:/macelleria|carni|salumeria|alimentari|supermercato|discount/i,label:"Macellerie e supermercati"},
    {re:/\b(pesce|orata|spigola|salmone|tonno fresco|calamaro|calamari|seppia|seppie|cozze|vongole|gamber|polpo)\b/i,
     cat:/pescheria|alimentari|supermercato|discount/i,label:"Pescherie e supermercati"},
    {re:/\b(mela|mele|banana|banane|arancia|arance|pera|pere|fragola|fragole|limone|limoni|patata|patate|pomodoro|pomodori|zucchina|zucchine|melanzana|insalata|frutta|verdura)\b/i,
     cat:/frutta|verdura|ortofrutta|alimentari|supermercato|discount/i,label:"Ortofrutta e supermercati"},
    {re:/\b(shampoo|balsamo|sapone|docciaschiuma|bagnoschiuma|deodorante|profumo|crema|cosmetico|cosmetici|trucco|dentifricio|spazzolino)\b/i,
     cat:/profumeria|cosmetica|cura casa e persona|supermercato|discount|alimentari/i,label:"Cura persona e supermercati"},
    {re:/\b(scarpa|scarpe|sneaker|stivale|stivali|sandalo|sandali|ciabatta|ciabatte)\b/i,
     cat:/calzature|abbigliamento/i,label:"Calzature"},
    {re:/\b(maglia|maglietta|camicia|pantalone|pantaloni|jeans|giacca|felpa|vestito|abito|gonna|intimo)\b/i,
     cat:/abbigliamento/i,label:"Abbigliamento"},
    {re:/\b(trapano|vite|viti|martello|cacciavite|chiave inglese|utensile|utensili|vernice|pittura|rubinetto|tubo|lampadina|cavo|presa|interruttore|silicone|colla|ferramenta)\b/i,
     cat:/ferramenta|edilizia|termoidraulica|materiale elettrico/i,label:"Ferramenta"},
    {re:/\b(smartphone|cellulare|telefono|tablet|computer|pc|notebook|laptop|stampante|cartuccia|toner|cuffie|auricolari|caricatore|cover)\b/i,
     cat:/telefonia|informatica|cartoleria/i,label:"Telefonia e informatica"},
    {re:/\b(frigo|frigorifero|lavatrice|lavastoviglie|forno|microonde|aspirapolvere|televisore|tv|phon|asciugacapelli|condizionatore|climatizzatore|stufa)\b/i,
     cat:/elettrodomestici|climatizzazione|elettrico/i,label:"Elettrodomestici"},
    {re:/\b(crocchette|mangime cane|mangime gatto|lettiera|guinzaglio|pettorina|cane|gatto|pet)\b/i,
     cat:/animali|pet|mangimi|zootecnia|alimentari|supermercato|discount/i,label:"Animali"},
    {re:/\b(quaderno|penna|matita|evidenziatore|album|cartella|zaino scuola|cancelleria|libro|romanzo)\b/i,
     cat:/cartoleria|edicola|libreria|supermercato|discount/i,label:"Cartoleria e libri"},
    {re:/\b(sedia|tavolo|divano|letto|armadio|materasso|cucina|mobile|mobili|comodino)\b/i,
     cat:/arredamento|mobili|cucine/i,label:"Arredamento"},
    {re:/\b(pneumatico|pneumatici|gomme auto|batteria auto|olio motore|filtro auto|pastiglie freno|ricambio auto|ricambi auto)\b/i,
     cat:/pneumatici|ricambi auto/i,label:"Auto e pneumatici"},
    {re:/\b(bici|bicicletta|casco bici|camera d aria|copertone bici|fitness)\b/i,
     cat:/biciclette|fitness|moto/i,label:"Bici e fitness"}
  ];

  function infer(query){
    const nq=N(query);
    if(!nq) return null;
    for(const it of INTENTS) if(it.re.test(nq)) return it;
    return null;
  }

  const chainPriority=/lidl|eurospin|\bmd\b|conad|nonna isa|super\s*pan|coop|crai|sidis|vege|dimeglio|d'italy|sant.?agostino/i;

  function render(){
    const raw=qEl.value.trim(), nq=N(raw), tokens=nq.split(" ").filter(Boolean);
    const intent=infer(raw);
    let arr=S.filter(s=>{
      if(townEl.value && s[0]!==townEl.value) return false;
      if(catEl.value && s[2]!==catEl.value) return false;
      if(!tokens.length) return true;

      const hay=s.h || N(s.join(" "));
      // Direct store/category/name search stays exact enough.
      if(tokens.every(tok=>hay.includes(tok))) return true;

      // For a recognizable product, do not require its brand/model to appear
      // in the store name. Select shop categories capable of carrying it.
      if(intent && intent.cat.test(s[2])) return true;
      return false;
    });

    if(intent && raw){
      arr.sort((a,b)=>{
        const ap=chainPriority.test(a[1])?1:0, bp=chainPriority.test(b[1])?1:0;
        if(ap!==bp) return bp-ap;
        return a[0].localeCompare(b[0],"it") || a[1].localeCompare(b[1],"it");
      });
    }

    count.textContent=arr.length;
    const suffix=intent && raw
      ? `<div style="grid-column:1/-1;margin:-2px 0 2px;color:#49605b;font-size:13px">Ricerca interpretata come <b>${esc(intent.label)}</b>. “${esc(raw)}” resta la query usata per cercare prezzi/offerte ufficiali.</div>`
      : "";

    grid.innerHTML=suffix+(arr.length?arr.map(s=>`
      <div class=c>
        <div class=t>${esc(s[0])}</div>
        <div class=n>${esc(s[1])}</div>
        <div class=k>${esc(s[2])}</div>
        <div class=w>${intent&&raw ? `Negozio compatibile con “${esc(raw)}”. Verifica la disponibilità della referenza specifica.` : "Acquisto in punto vendita. Verifica prima la disponibilità."}</div>
        <a class=b target=_blank rel=noopener href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(s[1]+" "+s[0]+" Sardegna")}">Apri su Maps</a>
      </div>`).join("")
      :'<div class=empty>Nessun punto vendita trovato. Prova un termine più generico.</div>');
  }

  qEl.oninput=render;
  townEl.onchange=render;
  catEl.onchange=render;
  render();
})();