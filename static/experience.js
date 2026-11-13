/* Progressive enhancement: all financial workflows remain in the Flask client. */
(() => {
  const content = document.querySelector('.content');
  if (!content) return;
  const bar = document.createElement('header');
  bar.className = 'command-bar';
  bar.innerHTML = '<div><strong>Workspace</strong> <span> / Accounts payable</span></div><div class="system-live">INTELLIGENCE ONLINE</div><span class="workspace-tag">ASA / CONTROL ROOM</span>';
  content.prepend(bar);
  const title = document.querySelector('.hero h1');
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
})();
