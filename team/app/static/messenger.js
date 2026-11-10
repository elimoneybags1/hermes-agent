/* One persistent boss conversation, with an activity inbox shared across pages. */
window.createTeamMessenger = function (ui) {
  'use strict';
  const {h, fill, api, prose} = ui;
  let data = {messages: [], queued: {}}, feed = {events: [], unread: 0};
  let ready = false, busy = false, open = false, tab = 'chat', full = null, error = '';
  const seen = new Set(), views = new Set();
  const time = at => at ? new Date(at).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
  const symbols = {message:'✦',run:'↗',success:'✓',attention:'!',decision:'?',idea:'◇'};
  const count = h('span', {class:'message-count',hidden:true});
  const trigger = h('button', {class:'messenger-launch', 'aria-label':'Talk to boss and view alerts', 'aria-expanded':'false', onclick:()=>toggle()},
    h('span',{class:'messenger-mark'},'✦'), h('span',null,'Boss'), count);
  const panel = h('section', {class:'messenger-popover',hidden:true,'aria-label':'Boss messages and activity'});
  const notices = h('div', {class:'message-notices','aria-live':'polite'});
  document.body.append(panel, trigger, notices);
  let draftText = '';

  function unreadLabel() {
    count.hidden = !feed.unread; count.textContent = feed.unread > 99 ? '99+' : feed.unread;
    const nav = document.querySelector('[data-page="messages"] .message-count');
    if (nav) {nav.hidden = !feed.unread;nav.textContent = feed.unread > 99 ? '99+' : feed.unread;}
  }
  async function mark(ids) {
    if (!ids.length) return;
    try {
      await api('alerts/read', {ids});
      const marked = new Set(ids);
      feed.events.forEach(e => {if(marked.has(e.id)) e.read = true;});
      feed.unread = feed.events.filter(e=>!e.read).length;
      unreadLabel(); views.forEach(v=>v.draw());
    } catch (e) {error=e.message;views.forEach(v=>v.draw());}
  }
  function follow(event) {
    mark([event.id]);
    if (event.kind === 'message') {open = false;panel.hidden = true;trigger.setAttribute('aria-expanded','false');}
    location.hash = event.href;
  }
  function notify(events) {
    events.slice(0,3).forEach(event => {
      const item = h('button', {class:'message-notice tone-'+event.kind, onclick:()=>{item.remove();follow(event);}},
        h('span',{class:'event-symbol'},symbols[event.kind]||'•'), h('span',null,h('strong',null,event.title),h('small',null,event.body.slice(0,120))));
      notices.append(item);setTimeout(()=>item.remove(),9000);
    });
  }
  async function poll() {
    if (busy) return;
    busy=true;
    try {
      const [chat, alerts] = await Promise.all([api('chat'),api('alerts')]);
      data=chat;feed=alerts;error='';
      const fresh = feed.events.filter(e=>!seen.has(e.id) && !e.read);
      if(ready) notify(fresh);
      feed.events.forEach(e=>seen.add(e.id));ready=true;
      unreadLabel();views.forEach(v=>v.draw());
    } catch(e) {error='Connection interrupted · '+e.message;views.forEach(v=>v.draw());}
    finally {busy=false;}
  }
  function bubble(m, live=false) {
    if (m.kind === 'note') return h('details', {class:'conversation-note'},h('summary',null,'Team update · '+time(m.at)),h('div',null,prose(m.text||'')));
    const mine=m.role==='you';
    return h('article',{class:'conversation-message '+(mine?'mine':'theirs')+(live?' streaming':'')},
      !mine?h('div',{class:'conversation-author'},h('span',{class:'avatar-mini'},m.role==='aide'?'A':'B'),m.role==='aide'?'Aide':'Boss'):null,
      h('div',{class:'conversation-bubble'},m.text?prose(m.text):live?h('span',{class:'typing','aria-label':'Boss is thinking'},h('i'),h('i'),h('i')):null,
        m.error?h('p',{class:'error'},m.error):null),
      m.tools?.length?h('div',{class:'conversation-tools'},m.tools.map(t=>h('span',null,t.done?'✓ ':'◌ ',t.name.replace(/^team_/,'')))):null,
      h('small',null,time(m.done||m.at)));
  }
  function conversation(compact) {
    let limit=compact?30:100, signature='', sending=false;
    const history=h('div',{class:'conversation-history',tabindex:'0','aria-label':'Conversation history'});
    const status=h('div',{class:'conversation-status',role:'status','aria-live':'polite'});
    const input=h('textarea',{rows:2,placeholder:'Tell the boss what’s on your mind…','aria-label':'Message to boss',disabled:window.TEAM_PREVIEW,
      oninput:()=>{draftText=input.value;},onkeydown:e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();form.requestSubmit();}}});
    input.value=draftText;
    const send=h('button',{class:'message-send',type:'submit',disabled:window.TEAM_PREVIEW,'aria-label':'Send to boss'},'↑');
    const form=h('form',{class:'conversation-composer',onsubmit:async e=>{
      e.preventDefault();if(sending||!input.value.trim()||window.TEAM_PREVIEW)return;
      sending=true;send.disabled=true;
      try {await api('chat',{text:input.value,to:'boss'});input.value='';draftText='';await poll();}
      catch(err){status.textContent=err.message;}
      finally {sending=false;send.disabled=false;input.focus();}
    }},input,send);
    const root=h('div',{class:'conversation'},history,status,form,h('div',{class:'composer-hint'},window.TEAM_PREVIEW?'Preview · sending messages is disabled':'Enter to send · Shift + Enter for a new line'));
    function draw() {
      status.textContent=error || (data.current?'Boss is working…':data.queued?.boss?`${data.queued.boss} message(s) queued`:'Your conversation is saved');
      const sig=JSON.stringify([data.messages,data.current,data.aide_current,limit]);
      if(sig===signature)return;
      const bottom=history.scrollHeight-history.scrollTop-history.clientHeight<100 || !signature;
      const offset=history.scrollTop;signature=sig;
      const messages=data.messages||[];
      fill(history,messages.length>limit?h('button',{class:'linkish',onclick:()=>{limit+=100;signature='';draw();}},'Load earlier messages'):null,
        messages.length?messages.slice(-limit).map(m=>bubble(m)):h('div',{class:'conversation-welcome'},h('div',{class:'welcome-orb'},'✦'),h('h2',null,'Start with a conversation'),h('p',null,'Share an idea, ask a question, or describe what you want to improve. The boss can work out the project and specifics with you.')),
        data.current?bubble(data.current,true):null,data.aide_current?bubble(data.aide_current,true):null);
      history.scrollTop=bottom?history.scrollHeight:offset;
    }
    const view={draw,root,input};views.add(view);draw();
    return {...view,destroy:()=>views.delete(view)};
  }
  function activity(compact) {
    let filter='all', signature='';
    const list=h('div',{class:'activity-list'}), status=h('p',{class:'muted',role:'status'});
    const filters=h('div',{class:'activity-filters'});
    const root=h('div',{class:'activity'},h('div',{class:'activity-head'},h('h2',null,'Activity'),h('button',{class:'linkish',onclick:()=>mark(feed.events.filter(e=>!e.read).map(e=>e.id))},'Mark all read')),filters,status,list);
    function draw() {
      status.textContent=error || (ready?`${feed.unread} unread · latest ${feed.events.length} updates`:'Connecting…');
      fill(filters,[['all','All'],['unread','Unread'],['idea','Ideas'],['run','Runs']].map(([key,label])=>h('button',{class:'filter-chip'+(filter===key?' selected':''),onclick:()=>{filter=key;signature='';draw();}},label)));
      const sig=JSON.stringify([feed.events,filter]);if(sig===signature)return;signature=sig;
      const events=feed.events.filter(e=>filter==='all'||filter==='unread'&&!e.read||filter==='idea'&&e.kind==='idea'||filter==='run'&&['run','success','attention'].includes(e.kind));
      fill(list,events.length?events.slice(0,compact?30:200).map(e=>h('button',{class:'activity-item tone-'+e.kind+(e.read?' read':' unread'),onclick:()=>follow(e)},
        h('span',{class:'event-symbol'},symbols[e.kind]),h('span',{class:'activity-copy'},h('strong',null,e.title),h('span',null,e.body),h('small',null,[e.target,time(e.at)].filter(Boolean).join(' · '))),!e.read?h('i',{class:'unread-dot'}):null)):
        h('div',{class:'activity-empty'},h('span',null,'✓'),h('p',null,'You’re all caught up')));
    }
    const view={root,draw};views.add(view);draw();return {...view,destroy:()=>views.delete(view)};
  }
  let compactView=null;
  function drawPanel() {
    if(compactView)compactView.destroy();
    compactView=tab==='chat'?conversation(true):activity(true);
    fill(panel,h('header',{class:'messenger-header'},h('span',{class:'avatar-mini'},'B'),h('strong',null,'Boss'),
      h('a',{href:'#messages',class:'linkish',onclick:()=>toggle(false)},'Open conversation ↗'),h('button',{class:'linkish','aria-label':'Close messages',onclick:()=>toggle(false)},'×')),
      h('div',{class:'messenger-tabs'},[['chat','Conversation'],['activity','Activity']].map(([key,label])=>h('button',{class:tab===key?'selected':'',onclick:()=>{tab=key;drawPanel();}},label))),compactView.root);
  }
  function toggle(value=!open) {open=value;panel.hidden=!open;trigger.setAttribute('aria-expanded',String(open));if(open){drawPanel();poll();}else if(compactView){compactView.destroy();compactView=null;}}
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&open)toggle(false);});
  function Page(main) {
    toggle(false);trigger.hidden=true;
    const chat=conversation(false),events=activity(false);full=chat;
    fill(main,h('header',{class:'messages-page-head'},h('div',null,h('div',{class:'dash-eyebrow'},'YOUR TEAM / CONVERSATION'),h('h1',null,'Talk to the boss')),h('span',{class:'conversation-live'},'● In-app messages')),
      window.TEAM_PREVIEW?h('div',{class:'dash-preview'},'Preview · history is read-only; message sending is disabled'):null,
      h('div',{class:'messages-layout'},h('section',{class:'message-thread'},h('header',null,h('span',{class:'avatar-mini'},'B'),h('div',null,h('strong',null,'Boss'),h('small',null,'Ideas, decisions, and what happens next'))),chat.root),events.root));
    poll();
    return ()=>{chat.destroy();events.destroy();full=null;trigger.hidden=false;};
  }
  function draft(text='') {draftText=text;if(full){full.input.value=text;full.input.focus();}else location.hash='#messages';}
  poll();setInterval(()=>{if(!document.hidden)poll();},5000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
  return {Page,draft,open:()=>toggle(true),snapshot:()=>data};
};
