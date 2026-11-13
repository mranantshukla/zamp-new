/* Progressive enhancement: all financial workflows remain in the Flask client. */
(() => {
  const content = document.querySelector('.content');
  if (!content) return;
  const bar = document.createElement('header');
  bar.className = 'command-bar';
  bar.innerHTML = '<div><strong>Workspace</strong> <span> / Accounts payable</span></div><div class="system-live">INTELLIGENCE ONLINE</div><span class="workspace-tag">ASA / CONTROL ROOM</span>';
  content.prepend(bar);
  const title = document.querySelector('.hero h1');
  document.querySelector('.hero-action').href='#inbox';
  title.innerHTML = 'Finance, at the<br><em>speed of thought.</em>';
  document.querySelector('.hero p').textContent = 'From document to decision. One intelligent workspace to capture invoices, verify every detail, and move with confidence.';
  const foot = document.createElement('div'); foot.className = 'hero-footnote'; foot.textContent = 'SOURCE → EVIDENCE → DECISION';
  document.querySelector('.hero-copy').append(foot);
  document.querySelector('.hero-graphic').innerHTML = '<span class="orbital-caption">NEURAL OPERATIONS<b>AP Intelligence Engine</b></span><div class="orbit"></div><div class="orbit orbit-two"></div><div class="core">'+Array.from({length:6},(_,i)=>'<div class="core-face">'+(i===0?'A':i===1?'S':i===2?'A':'·')+'</div>').join('')+'</div><div class="node node-one"></div><div class="node node-two"></div><div class="hero-label">DETERMINISTIC CONTROL<br>HUMAN JUDGEMENT</div>';
  const tabs = ['uploadtab','sampletab'].map(id=>document.getElementById(id));
  tabs.forEach((tab,i)=>{tab.setAttribute('aria-selected',String(i===0));tab.addEventListener('click',()=>tabs.forEach(t=>t.setAttribute('aria-selected',String(t===tab))));});
  const monitor = document.getElementById('timeline').closest('.card'); monitor.id='monitor';
  monitor.querySelector('h2').textContent='Execution console';
  const motion = !matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(motion){document.querySelectorAll('.hero,.card').forEach((el,i)=>{el.classList.add('reveal');el.style.animationDelay=Math.min(i*60,300)+'ms';});}
  document.addEventListener('visibilitychange',()=>document.body.classList.toggle('page-hidden',document.hidden));
  const links=[...document.querySelectorAll('.app-nav a')];
  function nav(){const hash=location.hash||'#home';links.forEach(a=>a.classList.toggle('active',a.getAttribute('href')===hash));}
  addEventListener('hashchange',nav);nav();
  const grid=document.querySelector('.grid');
  const upload=document.getElementById('upload');
  const history=document.getElementById('history');
  const queue=document.getElementById('reviewqueue');
  const result=document.getElementById('result');
  const settings=document.getElementById('aiconfiguration');
  const sections={home:[document.querySelector('.hero'),document.getElementById('metrics'),grid,upload,monitor],inbox:[upload,queue],extraction:[result],review:[queue,result],records:[history],console:[monitor,result],automations:[settings],vendors:[],analytics:[document.getElementById('metrics'),history]};
  const viewTitles={home:'Overview',inbox:'Intake inbox',extraction:'Extraction evidence',review:'Exception review',records:'Invoice records',vendors:'Vendor intelligence',analytics:'AP analytics',automations:'Automations & AI',console:'Execution console'};
  const viewBar=document.createElement('div');viewBar.className='view-bar';content.insertBefore(viewBar,document.querySelector('.hero'));
  const bulk=document.createElement('form');bulk.className='bulk-intake';bulk.innerHTML='<label>Process a batch <small>Up to 25 PDF or image documents</small><input type="file" name="invoice_files" accept=".pdf,.png,.jpg,.jpeg" multiple required></label><button class="button">Start batch →</button><p class="muted" role="status"></p>';upload.append(bulk);
  bulk.onsubmit=async e=>{e.preventDefault();const status=bulk.querySelector('[role=status]');status.textContent='Preparing batch…';try{const response=await fetch('/api/runs/bulk',{method:'POST',headers:{'X-CSRF-Token':csrf},body:new FormData(bulk)});const data=await response.json();if(!response.ok)throw Error(data.error||'Batch failed');status.textContent=`${data.runs.length} documents queued. Open Records or Execution Console for progress.`;bulk.reset();refresh();}catch(error){status.textContent=error.message}};
  const search=document.createElement('input');search.id='recordsearch';search.type='search';search.placeholder='Search vendor, invoice, PO, GSTIN or status';history.insertBefore(search,history.querySelector('.tablewrap'));
  const exportLink=document.createElement('a');exportLink.className='button export-link';exportLink.href='/api/runs/export.csv';exportLink.textContent='Export CSV ↓';history.insertBefore(exportLink,history.querySelector('.tablewrap'));
  let searchTimer;search.addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{const q=search.value.toLowerCase();document.querySelectorAll('#historyrows tr').forEach(row=>row.hidden=!row.textContent.toLowerCase().includes(q));},120)});
  const vendorPanel=document.createElement('section');vendorPanel.className='card view-panel';vendorPanel.innerHTML='<span class="eyebrow">PROCUREMENT INTELLIGENCE</span><h2>Vendor profiles</h2><p class="muted">Profiles below summarise processed records. Vendor approval still depends on the vendor master.</p><div id="vendorprofiles" class="profile-grid"></div>';content.append(vendorPanel);sections.vendors=[vendorPanel];
  const navMap=[['Home','home'],['My invoices','records'],['Scan & upload','inbox'],['Review centre','review'],['Reports','analytics'],['AI configuration','automations']];
  links.forEach(link=>{const hit=navMap.find(([label])=>link.textContent.includes(label));if(hit)link.href='#'+hit[1]});
  const extra=[['Extraction','extraction','◇'],['Vendors','vendors','◈'],['Execution Console','console','⌘']];
  extra.forEach(([label,id,icon])=>{const a=document.createElement('a');a.href='#'+id;a.innerHTML=`<span>${icon}</span>${label}`;document.querySelector('.app-nav').append(a)});
  function renderView(){const view=(location.hash||'#home').slice(1);const selected=sections[view]?view:'home';document.querySelectorAll('.hero,.metrics,.grid,#upload,#monitor,#result,#reviewqueue,#history,#aiconfiguration,.view-panel').forEach(el=>el.classList.add('view-hidden'));sections[selected].forEach(el=>{el?.classList.remove('view-hidden');if(el===result && !active)el.classList.add('hide')});if(selected==='inbox'){grid.classList.remove('view-hidden');monitor.classList.add('view-hidden')}if(selected==='console'){grid.classList.remove('view-hidden');upload.classList.add('view-hidden')}viewBar.innerHTML=`<span class="eyebrow">ASA / ${selected.toUpperCase()}</span><h2>${viewTitles[selected]}</h2>`;document.querySelectorAll('.app-nav a').forEach(a=>a.classList.toggle('active',a.hash==='#'+selected));if(selected==='vendors')loadVendors();}
  async function loadVendors(){const target=document.getElementById('vendorprofiles');const rows=await(await fetch('/api/runs')).json();const profiles=new Map();rows.forEach(r=>{if(!r.vendor)return;const p=profiles.get(r.vendor)||{name:r.vendor,count:0,approved:0,value:0};p.count++;if(r.status==='APPROVED')p.approved++;p.value+=Number(r.total)||0;profiles.set(r.vendor,p)});target.innerHTML=[...profiles.values()].map(p=>`<article><small>VENDOR RECORD</small><h3>${esc(p.name)}</h3><p>${p.count} document(s) · ${p.approved} approved</p><b>${new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR'}).format(p.value)}</b></article>`).join('')||'<p class="muted">No vendor records yet.</p>'}
  addEventListener('hashchange',renderView);renderView();
  const openRun=window.showRun;
  window.showRun=async id=>{location.hash='#console';renderView();await openRun(id);result.classList.remove('view-hidden');};
})();
