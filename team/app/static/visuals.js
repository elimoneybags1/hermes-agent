/* Model comparisons and gateway views keep operational detail behind inspection. */
window.createTeamVisuals = function (ui) {
  'use strict';
  const {h,fill,api,fmt} = ui;
  const btn=(label,click,selected=false)=>h('button',{class:'filter-chip'+(selected?' selected':''),onclick:click},label);
  function modelBars(metrics, select) {
    const total=v=>v.input+v.output;
    const groups=Object.entries(metrics.by_tier||{}).sort((a,b)=>total(b[1].total)-total(a[1].total)||a[0].localeCompare(b[0]));
    const named=new Map();
    groups.forEach(([,g])=>g.models.forEach(m=>{if(m.model && total(m.usage)>0)named.set(m.model,(named.get(m.model)||0)+total(m.usage));}));
    const ranked=[...named].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0])), top=new Set(ranked.slice(0,10).map(([name])=>name));
    const maximum=Math.max(1,...groups.flatMap(([,g])=>g.models.filter(m=>m.model).map(m=>total(m.usage))));
    const allCalls=groups.reduce((n,[,g])=>n+g.total.records,0);
    const identifiedCalls=groups.reduce((n,[,g])=>n+g.models.filter(m=>m.model).reduce((sum,m)=>sum+m.usage.records,0),0);
    const amount=(v,key)=>v['missing_'+key]===v.records?'—':fmt(v[key]);
    function counts(v) {return h('span',{class:'model-bar-counts'},h('span',{class:'input-color',title:'Input tokens'},amount(v,'input')),h('span',{class:'output-color',title:'Output tokens'},amount(v,'output')));}
    function row(m) {
      const v=m.usage, name=m.model;
      return h('button',{class:'model-bar-row',onclick:()=>select(m.model),
        'aria-label':`${name}: ${v.input.toLocaleString()} reported input, ${v.output.toLocaleString()} reported output tokens.`,
        title:`${name}\nReported responder\nInput: ${v.input.toLocaleString()}\nOutput: ${v.output.toLocaleString()}\n${v.records} call records · ${v.missing_input} missing input · ${v.missing_output} missing output`},
        h('span',{class:'model-bar-name'},h('span',{class:'model-child-arrow'},'↳'),h('span',{class:'model-bar-identity'},h('strong',null,name),h('small',null,`${v.records} calls`))),
        h('span',{class:'model-bar-track'},h('span',{class:'model-bar-input',style:`width:${v.input/maximum*100}%`}),h('span',{class:'model-bar-output',style:`width:${v.output/maximum*100}%`})),counts(v));
    }
    function group([key,g]) {
      const sorted=g.models.slice().sort((a,b)=>total(b.usage)-total(a.usage)||(a.model||'').localeCompare(b.model||''));
      const shown=sorted.filter(m=>m.model&&top.has(m.model)), other=sorted.filter(m=>m.model&&!top.has(m.model));
      const missing=sorted.filter(m=>!m.model);
      return h('section',{class:'tier-usage-group','aria-label':g.label+' usage'},
        h('div',{class:'tier-usage-heading'},h('div',null,h('span',{class:'tier-usage-label'},g.label),h('small',null,`${g.total.records} calls · ${g.models.filter(m=>m.model).length} reported models`)),counts(g.total)),
        h('div',{class:'model-bar-list'},shown.length?shown.map(row):other.length?null:h('div',{class:'dash-empty'},'No identified model usage in this tier for this period.')),
        other.length?h('details',{class:'tier-usage-more'},h('summary',null,`${other.length} more models · included in tier total`),other.map(row)):null,
        missing.length?h('details',{class:'tier-usage-more missing-model-usage'},
          h('summary',null,`Model identity missing · ${missing.reduce((n,m)=>n+m.usage.records,0)} calls · ${fmt(missing.reduce((n,m)=>n+total(m.usage),0))} tokens included in tier total`),
          h('p',{class:'muted'},'These records contain token usage but no concrete responder name. They stay in totals, but do not set the model chart scale. Older tier aliases cannot identify the model that answered.'),
          missing.map(m=>h('div',{class:'model-bar-footer'},h('span',null,'Requested '+m.requested_model),counts(m.usage)))):null);
    }
    return h('section',{class:'dash-card model-bars'},h('div',{class:'dash-section-title'},h('div',null,h('span',{class:'dash-eyebrow'},'WHERE THE TOKENS GO'),h('h2',null,'Usage by tier and model')),
      h('div',{class:'bar-legend'},h('span',null,h('i',{class:'input-swatch'}),'Input'),h('span',null,h('i',{class:'output-swatch'}),'Output'))),
      groups.length?groups.map(group):h('div',{class:'dash-empty'},'No recorded usage in this period.'),
      h('div',{class:'model-bar-footer'},h('span',null,`Top ${top.size} of ${ranked.length} reported models by tokens · grouped by requested tier`),h('span',null,ranked.length>=10?`Top-10 cutoff · ${fmt(ranked[9][1])} tokens`:'Models appear as usage is recorded')),
      h('small',{class:'muted'},`${identifiedCalls} of ${allCalls} calls have model identity. Tier totals include all calls. Bars compare identified models only; click a model to filter across tiers. Direct requests have no recorded tier.`));
  }
  function Gateway(main) {
    let alive=true, data=null, routing=null, selected=null, search='', pane='routes', timer;
    const status=h('span',{class:'muted',role:'status'}), body=h('div',{class:'gateway-content'});
    const searchInput=h('input',{type:'search',placeholder:'Find a model or provider…','aria-label':'Find a model or provider',oninput:()=>{search=searchInput.value.toLowerCase();const cursor=searchInput.selectionStart;draw();searchInput.focus();searchInput.setSelectionRange(cursor,cursor);}});
    fill(main,window.TEAM_PREVIEW?h('div',{class:'dash-preview'},'Comparison preview · configuration changes and execution are disabled'):null,
      h('header',{class:'dash-heading'},h('div',{class:'dash-eyebrow'},'CONNECTIONS / ROUTING'),h('h1',null,'Gateway'),h('p',null,'A clear path from your agents to the models that answer.')),
      h('div',{class:'dash-controls'},status,h('a',{class:'dash-link',href:'#gateway-settings?tab=models'},'Configure tiers & fallbacks →'),h('a',{class:'dash-link',href:'#gateway-settings'},'Advanced controls ↗')),body);
    function service(name,label,sub) {
      const value=data.services[name], health=value?.healthy;
      return h('div',{class:'gateway-hop '+(health?'healthy':'unhealthy')},h('span',{class:'hop-icon'},name==='litellm'?'⇶':'◈'),h('strong',null,label),h('span',null,sub),h('small',null,health?'● Connected':'● Not answering'));
    }
    function modelCard(m) {
      const rate=m.rate, n=rate?.n||0, percent=n?Math.round(rate.ok/n*100):null;
      return h('button',{class:'gateway-model'+(selected===m.id?' selected':''),onclick:()=>{selected=selected===m.id?null:m.id;draw();if(selected)body.querySelector('.route-inspector')?.scrollIntoView({behavior:'smooth',block:'nearest'});}},
        h('span',{class:'gateway-model-top'},h('span',{class:'tier-dot tier-'+m.tier}),h('span',{class:'muted'},m.tier||'unpooled'),h('span',{class:'gateway-model-count'},n?`${n} calls`:'No recent calls')),
        h('strong',null,m.id),h('span',{class:'provider-chips'},m.providers.map(p=>h('span',null,p))),
        h('span',{class:'gateway-success-track'},h('i',{style:`width:${percent||0}%`})),
        h('small',null,n?`${rate.ok}/${n} successful · ${rate.rl||0} rate limited`:'No success rate measured'),
        h('span',{class:'gateway-inspect'},selected===m.id?'Close details ↑':'Inspect route →'));
    }
    function draw() {
      if(!alive||!data)return;
      const models=data.models.filter(m=>[m.id,...m.providers,...m.unavailable].join(' ').toLowerCase().includes(search));
      const detail=data.models.find(m=>m.id===selected);
      const tiers=[...new Set(data.models.map(m=>m.tier).filter(Boolean))];
      const n=data.models.reduce((sum,m)=>sum+(m.rate?.n||0),0), ok=data.models.reduce((sum,m)=>sum+(m.rate?.ok||0),0);
      fill(body,h('div',{class:'gateway-path'},h('a',{class:'gateway-hop agent-hop',href:'#agents'},h('span',{class:'hop-icon'},'◎'),h('strong',null,'Your agents'),h('span',null,'Role-specific policy'),h('small',null,'Inspect agents →')),
        h('span',{class:'gateway-arrow'},'→'),service('litellm','LiteLLM','Chooses a model'),h('span',{class:'gateway-arrow'},'→'),service('portkey','Portkey','Routes to a provider')),
        h('div',{class:'gateway-summary'},h('div',null,h('strong',null,data.models.length),h('span',null,'configured models')),h('div',null,h('strong',null,n),h('span',null,`calls · last ${data.hours}h`)),h('div',null,h('strong',null,n?`${ok}/${n}`:'—'),h('span',null,'successful calls'))),
        h('div',{class:'dash-section-title'},h('div',{class:'segmented-control'},btn('Model routes',()=>{pane='routes';draw();},pane==='routes'),btn('Fallbacks',()=>{pane='fallbacks';draw();},pane==='fallbacks')),searchInput),
        pane==='routes'?h('div',{class:'gateway-models-grid'},models.length?models.map(modelCard):h('div',{class:'dash-empty'},'No matching model routes.')):
          h('div',{class:'fallback-grid'},tiers.map(t=>h('section',{class:'fallback-card'},h('h2',null,h('span',{class:'tier-dot tier-'+t}),t),
            h('div',{class:'fallback-steps'},h('span',{class:'fallback-primary'},t),...(routing?.fallbacks[t]||[]).flatMap(f=>[h('span',{class:'gateway-arrow'},'→'),h('span',null,f)])),
            h('small',null,(routing?.fallbacks[t]||[]).length?'Configured fallback order':'No fallback configured')))),
        detail?h('section',{class:'dash-card route-inspector'},h('div',{class:'dash-section-title'},h('h2',null,detail.id),btn('Close',()=>{selected=null;draw();})),
          h('div',{class:'route-facts'},h('div',null,h('small',null,'Tier'),h('strong',null,detail.tier||'None')),h('div',null,h('small',null,'Configured priority'),h('strong',null,detail.order??'Unknown')),h('div',null,h('small',null,'Providers in order'),h('strong',null,detail.providers.join(' → ')||'None available'))),
          detail.unavailable.length?h('p',{class:'muted'},'Unavailable: '+detail.unavailable.join(', ')):null,
          h('a',{href:'#orchestration',class:'dash-link'},'Follow the actual calls in a run →')):null,
        h('details',{class:'dash-card'},h('summary',null,'How to read this'),h('p',{class:'dash-note'},'Service health is checked live. Model cards show configured providers and measured gateway outcomes. A configured route is not a claim of model availability. Use a run’s session-linked evidence to inspect actual fallback attempts.')));
    }
    async function load() {
      try {const [d,c]=await Promise.all([api('gateway/diagram'),api('capabilities')]);if(!alive)return;data=d;routing=c.routing;status.textContent='Updated '+new Date().toLocaleTimeString();draw();}
      catch(e){if(alive)status.textContent='Could not refresh · '+e.message;}
    }
    status.textContent='Checking gateway…';load();timer=setInterval(()=>{if(!document.hidden&&!body.contains(document.activeElement))load();},30000);
    return()=>{alive=false;clearInterval(timer);};
  }
  return {modelBars,Gateway};
};
