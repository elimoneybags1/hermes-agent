/* Evidence-first dashboard pages. All displayed state comes from Team's local APIs. */
window.createTeamDashboard = function (ui) {
  'use strict';
  const {h, fill, api, fmt, badge, messenger, visuals} = ui;
  let target = '';
  const text = value => value === null || value === undefined ? 'Not recorded' : String(value);
  const stamp = value => value ? new Date(value).toLocaleString() : 'Not recorded';
  const link = (label, href) => h('a', {class: 'dash-link', href}, label);
  const empty = message => h('div', {class: 'dash-empty'}, message);
  const note = message => h('p', {class: 'dash-note'}, message);
  const card = (title, ...body) => h('section', {class: 'dash-card'}, h('h2', null, title), ...body);
  const stat = (label, value, detail) => h('div', {class: 'dash-stat'}, h('span', null, label), h('strong', null, value), h('small', null, detail));
  const state = value => badge(value || 'unreported', value === 'pass' || value === 'active' || value === 'done' ? 'badge--ok' : 'badge--outline');
  function table(headers, rows) {
    return h('div', {class: 'dash-table-wrap'}, h('table', {class: 'table'},
      h('thead', null, h('tr', null, headers.map(x => h('th', {scope: 'col'}, x)))),
      h('tbody', null, rows.length ? rows.map(row => h('tr', null, row.map(x => h('td', null, x)))) : h('tr', null, h('td', {colspan: headers.length}, 'No records for this selection.')))));
  }
  const button = (label, onclick) => h('button', {class: 'btn btn--small', onclick}, label);
  function jsonDetails(title, value) {
    return h('details', {class: 'dash-details'}, h('summary', null, title), h('pre', null, typeof value === 'string' ? value : JSON.stringify(value, null, 2)));
  }
  function dialog(title, loader) {
    const body = h('div', null, empty('Loading evidence…'));
    const modal = h('dialog', {class: 'dash-dialog', 'aria-label': title},
      h('div', {class: 'dash-dialog-head'}, h('h2', null, title), button('Close', () => modal.close())), body);
    document.body.append(modal);
    modal.addEventListener('close', () => modal.remove());
    modal.showModal();
    loader(body).catch(e => fill(body, note(e.message)));
  }
  function callTable(records) {
    return table(['When', 'Role / project', 'Requested → reported', 'Provider reported', 'Input / output', 'Result', 'Trace'], records.map(r => [
      stamp(r.ts), `${r.role || '—'} · ${r.target || 'unattributed'}`,
      h('span', null, h('code', null, r.model), ' → ', h('code', null, r.response_model || 'Unreported')),
      r.provider, `${text(r.tokens_in)} / ${text(r.tokens_out)}`,
      state(r.ok ? 'pass' : 'failed'), r.job_id ? button(`Run #${r.job_id}`, () => inspectRun(r.job_id)) : 'No job link',
    ]));
  }
  function inspectRun(id) {
    dialog(`Run #${id}`, async body => {
      const d = await api(`insights/job?id=${id}`), j = d.job;
      fill(body, note(`${j.role} · ${j.target} · ${stamp(j.started)} → ${j.ended ? stamp(j.ended) : 'No end recorded'}`),
        h('div', {class: 'dash-stats'}, stat('Outcome', j.outcome || 'Open', j.grader || 'No grader recorded'),
          stat('Attempt', j.attempt, j.failure_kind || 'Failure class not recorded')),
        card('Selection and configuration', jsonDetails('Recorded model selection and reason', j.model_choice),
          note(`Role version: ${j.role_version} · Source commit: ${j.target_commit}`)),
        card('Verification', h('pre', null, j.grader_detail || 'No verification output recorded.')),
        card('Model calls', callTable(d.calls.map(c => ({...c, role: j.role, target: j.target})))),
        card('Gateway attempts and fallbacks', note(d.gateway_attempts.note), table(['When', 'Model', 'Deployment', 'Result', 'Reason'], d.gateway_attempts.records.map(r => [stamp(r.ts), r.model, r.deployment, state(r.ok ? 'pass' : 'failed'), r.rate_limited ? 'Rate limited' : r.error || 'No failure reported']))),
        card('Work order', d.order ? jsonDetails(`Version ${d.order.version}`, d.order.body) : empty('No work order linked.')),
        card('Retry and escalation decisions', d.diagnoses.length ? d.diagnoses.map(x => h('div', {class: 'dash-event'}, state(x.action), h('p', null, x.note), note(x.cause))) : empty('No diagnosis recorded.')),
        note(d.limitations));
    });
  }
  function shell(main, title, subtitle, render, extras) {
    let alive = true, timer, sequence = 0;
    const content = h('div', {class: 'dash-content'}, empty('Loading Team records…'));
    const status = h('span', {class: 'muted', role: 'status', 'aria-live': 'polite'});
    const pick = h('select', {'aria-label': 'Project', onchange: () => {target = pick.value; load();}});
    const controls = h('div', {class: 'dash-controls'}, h('label', null, 'Project ', pick), button('Refresh', () => load()), status);
    fill(main, window.TEAM_PREVIEW ? h('div', {class: 'dash-preview', role: 'status'}, 'Comparison preview · saved state snapshot · idea edits are temporary · execution and configuration changes are disabled') : null, h('header', {class: 'dash-heading'}, h('div', null, h('div', {class: 'dash-eyebrow'}, 'TEAM / WORKSPACE'), h('h1', null, title), h('p', null, subtitle))), controls,
      extras ? extras(() => load()) : null, content);
    async function load() {
      const current = ++sequence;
      status.textContent = 'Refreshing…';
      try {
        const d = await api('insights' + (target ? '?target=' + encodeURIComponent(target) : ''));
        if (!alive || current !== sequence) return;
        if (target && !d.targets.includes(target)) target = '';
        fill(pick, h('option', {value: '', selected: !target}, 'All projects'), d.targets.map(n => h('option', {value: n, selected: n === target}, n)));
        await render(content, d, () => load(), () => alive && current === sequence);
        if (alive && current === sequence) status.textContent = 'Updated ' + new Date(d.generated_at).toLocaleTimeString();
      } catch (e) {
        if (alive && current === sequence) status.textContent = 'Update failed · ' + e.message + ' · previous data may be stale';
      }
    }
    load();
    timer = setInterval(() => { if (!document.hidden && !document.querySelector('dialog[open]') && !content.contains(document.activeElement)) load(); }, 30000);
    return () => {alive = false; clearInterval(timer); document.querySelectorAll('.dash-dialog').forEach(d => d.close());};
  }
  function projectCard(p) {
    const r = p.scout_report;
    return card(p.name, h('div', {class: 'dash-row'}, state(p.enabled ? 'enabled' : 'disabled'), badge(p.check ? 'check configured' : 'onboarding needed', 'badge--outline')),
      p.review_status ? note(`Review checkpoint: ${p.review_status.phase || 'unknown'} · ${stamp(p.review_status.updated_at)} · ${p.review_status.note || ''}${p.review_status.errors?.length ? ' · '+p.review_status.errors.join('; ') : ''}`) : null,
      note(p.path), r ? h('div', null, note(`Scout receipt saved ${stamp(r.saved_at)}`),
        h('div', {class: 'dash-mini-stats'}, h('span', null, text(r.calls), ' calls'), h('span', null, text(r.cache_hits), ' cache hits'), h('span', null, text(r.pending_chunks), ' chunks pending')),
        note(r.freshness || r.error), jsonDetails('Read saved evidence summary', r.summary || r.errors || r.error)) : empty('No scout report saved yet.'),
      p.ideas_report ? jsonDetails('AI proposals · unverified', p.ideas_report.ideas || p.ideas_report.error) : null);
  }
  function Overview(main) {
    return shell(main, 'Your team, in view', 'From an idea to verified work. Follow the evidence behind every result.', async (view, d, reload, valid) => {
      const m = await api('metrics?days=30' + (target ? '&target=' + encodeURIComponent(target) : ''));
      if (!valid()) return;
      const open = d.jobs.filter(j => !j.ended), decisions = d.decisions.filter(x => x.status === 'open');
      fill(view, h('section',{class:'boss-invite compact'},h('span',{class:'welcome-orb'},'✦'),h('div',null,h('h2',null,'What’s on your mind?'),h('p',null,'Talk to the boss. Follow the work here.')),button('Message boss →',()=>messenger.open())), h('div', {class: 'dash-stats'}, stat('Open attempts', open.length, 'Recorded as open; process liveness unverified'),
        stat('Needs you', decisions.length, 'Open decisions in the recent record window'),
        stat('Ideas waiting', d.ideas.filter(i => i.status === 'inbox').length, 'Saved without dispatching work'),
        stat('Input / output · 30d', `${fmt(m.total.input)} / ${fmt(m.total.output)}`, `${m.total.records} usage records · ${m.total.unknown_cost} costs unknown`)),
        h('div', {class: 'dash-grid'}, card('Follow the work', h('div', {class: 'dash-flow'}, ['Ideas', 'Investigation', 'Implementation', 'Verification', 'Follow-up'].map((x, i) => link(x, ['#ideas', '#orchestration', '#work', '#orchestration', '#routines'][i]))),
          note('Stage links open the underlying records. No end timestamp does not prove an agent is still running.'),
          ...d.goals.slice(0, 5).map(g => h('div', {class: 'dash-event'}, state(g.outcome || 'open'), link(g.goal, '#orchestration?goal=' + g.id), note(`${g.target} · ${g.type} · ${g.branch}`)))),
        card('Needs your attention', decisions.length ? decisions.slice(0, 6).map(x => h('div', {class: 'dash-event'}, h('strong', null, x.question), note(x.target + ' · ' + x.source), link('Review in Work →', '#work'))) : empty('No open decisions in this window.'), link('Open ideas inbox →', '#ideas'))),
        h('div', {class: 'dash-section-title'}, h('h2', null, 'Connected projects'), link('Models and routing →', '#models')),
        h('div', {class: 'dash-grid'}, d.projects.length ? d.projects.map(projectCard) : empty('No attached projects. Attach an existing checkout with team attach --disabled.')),
        note('Latest records only: up to 500 attempts and 200 goals/decisions. Usage totals cover the selected 30-day window.'));
    });
  }
  function Orchestration(main) {
    const query = new URLSearchParams(location.hash.split('?')[1] || '');
    let chosen = query.get('goal') || '';
    if(query.has('run')) inspectRun(Number(query.get('run')));
    return shell(main, 'Orchestration', 'See how Team moves work through Hermes, verification, and review.', (view, d) => {
      const goal = d.goals.find(g => String(g.id) === chosen);
      const picker = h('select', {'aria-label': 'Goal', onchange: () => {chosen = picker.value; draw();}}, h('option', {value: ''}, 'All recent work'),
        d.goals.map(g => h('option', {value: g.id, selected: String(g.id) === chosen}, `#${g.id} ${g.goal.slice(0, 90)}`)));
      function draw() {
        const selected = d.goals.find(g => String(g.id) === chosen);
        const jobs = chosen ? d.jobs.filter(j => j.goal_ids.includes(Number(chosen))) : d.jobs;
        fill(view, h('div', {class: 'dash-controls'}, picker, link('Open work and approvals →', '#work')),
          selected ? card(selected.goal, note(`${selected.target} · ${selected.type} · ${selected.branch}`),
            h('div', {class: 'dash-flow'}, pipelineNodes(selected, d)),
            jsonDetails('Recorded stage policy and verification', {stages: selected.stages, stage_index: selected.stage_idx, risk: selected.risk, acceptance: selected.acceptance_cmd, check: selected.check_cmd, detail: selected.detail})) :
            card('Configured pipelines', note('These are pipeline definitions. Per-goal records preserve the stage policy used for execution.'), ...d.pipelines.map(p => h('div', {class: 'dash-pipeline'}, h('strong', null, p.type), h('div', {class: 'dash-flow'}, p.stages.map(s => h('span', {class: 'dash-stage'}, s.name, h('small', null, s.role || 'Code gate'))))))) ,
          card('Recorded attempts', table(['Run', 'Role / project', 'Requested model', 'State', 'Selection reason', 'Started', 'Evidence'], jobs.map(j => [
            `#${j.id} · attempt ${j.attempt}`, `${j.role} · ${j.target}`, (j.model_choice || {}).spec || (j.model_choice || {}).model || 'Unreported',
            state(j.outcome || 'open'), (j.model_choice || {}).why || 'Not recorded', stamp(j.started), button('Inspect run', () => inspectRun(j.id)),
          ]))), note('Newest 500 attempts. Open means no end is recorded. Parent-session and tool-call traces are not yet captured in this ledger.'));
      }
      if (chosen && !goal) chosen = '';
      draw();
    });
  }
  function pipelineNodes(g, d) {
    let stages;
    try {stages = JSON.parse(g.stages || 'null');} catch (_) {stages = null;}
    if (!Array.isArray(stages)) stages = (d.pipelines.find(p => p.type === g.type) || {stages: []}).stages.map(s => s.name);
    return stages.map((s, i) => h('span', {class: 'dash-stage' + (i === g.stage_idx ? ' current' : '')}, typeof s === 'string' ? s : s.name, i === g.stage_idx ? h('small', null, 'Recorded stage') : null));
  }
  function Agents(main) {
    return shell(main, 'Every agent, inspectable', 'Effective project configuration, model access, instructions, and recorded work.', async (view, d, reload, valid) => {
      const a = await api('agents' + (target ? '?target=' + encodeURIComponent(target) : ''));
      if (!valid()) return;
      fill(view, h('div', {class: 'dash-controls'}, link('Propose agents and edit files →', '#agent-settings')),
        h('div', {class: 'dash-agent-grid'}, a.roles.map(r => card(r.title, h('div', {class: 'dash-row'}, state(r.status), badge(r.harness || 'unknown', 'badge--outline')),
          h('p', null, r.does), note((r.candidates || []).join(' → ') || r.model || 'No route configured'),
          note(`${r.rate.pass}/${r.rate.n} passed · roster baseline ${a.roles.reduce((n,x) => n + x.rate.pass,0)}/${a.roles.reduce((n,x) => n + x.rate.n,0)} · ${r.rate.infra} infrastructure failures`),
          r.problem ? note(r.problem) : null, button('Inspect agent →', () => inspectAgent(r.name))))),
        h('div', {class: 'dash-grid'}, card('Scout', state('utility'), h('p', null, 'Reads and summarizes source into cached evidence. It is not a registered worker role.'), link('View project receipts →', '#overview')),
          card('Rescue', state('execution mode'), h('p', null, 'Escalation policy belongs to each worker. Inspect the configured model and failed-attempt threshold on its agent.'))));
    });
  }
  function inspectAgent(name) {
    dialog(name + ' · effective configuration', async body => {
      const d = await api('insights/agent?name=' + encodeURIComponent(name) + (target ? '&target=' + encodeURIComponent(target) : '')), r = d.role;
      fill(body, note(`${r.title} · ${d.target || 'base configuration'} · fingerprint ${d.fingerprint.slice(0, 12)}`),
        card('Models and fallback policy', h('div', {class: 'dash-flow'}, (r.models.candidates.length ? r.models.candidates : [r.runs_on.model || 'No configured model']).map((m,i) => h('span', {class: 'dash-stage'}, m, h('small', null, i ? 'Allowed alternative' : 'Configured first choice')))), note(r.escalation.model ? `Escalate to ${r.escalation.model} after ${r.escalation.after_failures} failures` : 'No escalation model configured'), jsonDetails('Candidates, pin, harness, and escalation', {models: r.models, harness: r.runs_on, escalation: r.escalation, budget: r.budget}), link('See gateway fallbacks and evidence →', '#models')),
        card('Tools and access', note(d.tool_note), table(['Toolset', 'Expanded tools', 'Origin'], d.tools.map(t => [t.toolset, t.tools.join(', ') || 'No static expansion available', t.origin])), note('Base role toolsets: ' + d.base_tools.join(', '))),
        card('Skills and instructions', jsonDetails('Effective instructions', r.soul || 'No role instructions'), jsonDetails('Base instructions', d.base_instructions || 'None'),
          d.skills.map(s => jsonDetails(s.name + ' · ' + s.origin, s.content))),
        card('Verification contract', note(`${r.gate} · ${r.graded_by}`)),
        card('Recent runs', table(['Run', 'Project', 'Role version used', 'Result', 'Inspect'], d.runs.map(j => [j.id, j.target, j.role_version, state(j.outcome || 'open'), button('Open', () => inspectRun(j.id))]))),
        link('Configuration files and change history →', '#team'));
    });
  }
  function Ideas(main) {
    let selected='conversation';
    return shell(main, 'Ideas grow here', 'Start with the boss. Your conversations, discoveries, and proposals come together here.', async (view, d, reload, valid) => {
      const inbox=await api('inbox');if(!valid())return;
      const plans=(inbox.plans||[]).filter(p=>!target||p.target===target);
      const suggestions=d.projects.filter(p=>p.ideas_report);
      const tabs=[['conversation','From conversations',plans.length],['discoveries','AI discoveries',suggestions.length],['saved','Saved notes',d.ideas.length]];
      function draw() {
        fill(view,h('section',{class:'boss-invite'},h('span',{class:'welcome-orb'},'✦'),h('div',null,h('h2',null,'Just tell the boss'),h('p',null,'No project forms. Talk through the idea and work out the details together.')),button('Start a conversation →',()=>messenger.draft())),
          h('div',{class:'segmented-control'},tabs.map(([key,label,n])=>h('button',{class:'filter-chip'+(selected===key?' selected':''),onclick:()=>{selected=key;draw();}},label,h('span',{class:'tab-count'},n)))),
          h('div',{class:'dash-agent-grid'},selected==='conversation'?(plans.length?plans.map(p=>card(p.headline||p.title||'Conversation proposal',state(p.status),h('p',{class:'idea-excerpt'},p.summary||''),
            h('div',{class:'idea-goals'},(p.goals||[]).map(g=>h('div',null,state(g.status),h('span',null,g.goal)))),
            link('Review proposal →','#work'),button('Talk it through',()=>messenger.draft(`Let's discuss this proposal: ${p.headline||p.title}.`)))):empty('Proposals from the boss will appear here after you discuss an idea.')):
            selected==='discoveries'?(suggestions.length?suggestions.map(p=>card(p.name,state('unverified idea'),h('p',{class:'idea-excerpt'},p.ideas_report.ideas||p.ideas_report.error),
              jsonDetails('Read the full discovery',p.ideas_report.ideas||p.ideas_report.error),button('Discuss with boss →',()=>messenger.draft(`I'd like to discuss these unverified ideas for ${p.name}:\n\n${p.ideas_report.ideas||''}`)))):empty('New AI discoveries will appear here and in your activity inbox.')):
              (d.ideas.length?d.ideas.map(i=>card(i.title,state(i.status),h('p',{class:'idea-excerpt'},i.body),note(i.target+' · '+i.source),
                button('Discuss with boss →',()=>messenger.draft(`Let's discuss this idea: ${i.title}\n\n${i.body}`)),i.goal_id?link('Follow the work →','#orchestration?goal='+i.goal_id):null)):empty('No saved notes. Start a conversation to explore something new.'))),
          d.reviews.length?h('details',{class:'dash-card'},h('summary',null,'Post-work findings · '+d.reviews.length),d.reviews.map(r=>jsonDetails(`${r.target} · ${stamp(r.created)}`,r.summary))):null);
      }
      draw();
    });
  }
  function Models(main) {
    return shell(main, 'Models matched to the work', 'Named routes, explicit fallback policies, and measured outcomes within each role.', async (view, d, reload, valid) => {
      const c = await api('capabilities' + (target ? '?target=' + encodeURIComponent(target) : ''));
      if (!valid()) return;
      fill(view, h('div', {class: 'dash-controls'}, link('Configure tiers & fallbacks →', '#gateway-settings?tab=models')),
        h('div', {class: 'dash-grid'}, card('Configured fallback paths', ...Object.entries(c.routing.fallbacks).map(([tier, paths]) => h('div', {class: 'dash-pipeline'}, h('strong', null, tier),
        h('div', {class: 'dash-flow'}, paths.length ? paths.map(p => h('span', {class: 'dash-stage'}, p)) : note('No fallback configured')))),
        jsonDetails('Context-window fallback policy', c.routing.context_fallbacks || {}), note(c.routing.note || c.routing.error)),
        card('Why this model?', h('p', null, 'Team selects from each role’s allowed candidates. Inspect a run to see the recorded selection reason and requested versus reported models.'),
          link('Inspect execution →', '#orchestration'), link('Inspect agent policies →', '#agents'), link('Gateway health and configuration →', '#gateway'),
          note('Provider failover and quality escalation are different events. A tier is a grouping, not proof that every member performs equally.'))),
        card('Named model catalog', table(['Model', 'Tier / order', 'Configured providers'], c.routing.models.map(m => [m.id, `${m.tier || '—'} / ${m.order ?? '—'}`,
          m.providers.map(p => h('div', null, p.provider, p.unavailable ? ' · unavailable: ' + p.unavailable : ' · configured'))]))),
        card('Capability evidence', note('Observed graded outcomes by role and task type. Candidate routes may be aliases; this is not a controlled benchmark of the underlying model. Infrastructure failures are excluded from work rates.'),
          table(['Role / task', 'Candidate route', 'Passed / work attempts', 'Comparable role/task baseline', 'Infrastructure', 'Unclassified'], c.evidence.map(e => [
            `${e.role} / ${e.task_type}`, e.model, `${e.passed}/${e.work}`, `${e.baseline.passed}/${e.baseline.work}`, e.infra, e.unclassified,
          ])), note('Specialized quality evaluations and reviewed routing experiments are still to be added. No scores are invented for unevaluated capabilities.')));
    });
  }
  function Routines(main) {
    return shell(main, 'Continuous improvement', 'Scheduled work, durable claims, and the findings left behind.', (view, d) => {
      fill(view, h('div', {class: 'dash-stats'}, stat('Configured routines', d.routines.length, 'Configuration files for selected projects'),
        stat('Enabled', d.routines.filter(r => r.enabled && r.target_enabled).length, 'Both routine and project enabled'),
        stat('Unresolved claims', d.routine_runs.filter(r => r.status === 'claimed').length, 'Inspect before retrying')),
        card('Schedules and scope', d.routines.length ? table(['Routine / project', 'Action', 'Interval', 'Status', 'Next time slot (UTC)', 'Scope'], d.routines.map(r => [
          `${r.name} / ${r.target}`, r.action || r.error, r.every_s ? r.every_s + ' seconds' : 'Unknown', state(r.enabled && r.target_enabled ? 'enabled' : 'disabled'),
          r.enabled && r.target_enabled && r.every_s ? new Date((Math.floor(Date.now()/1000/r.every_s)+1)*r.every_s*1000).toISOString() : 'Not scheduled',
          r.goal || 'Read-only scout, bounded by runner',
        ])) : empty('No routines configured. No scheduled audits or GitHub research are being implied.'),
          note('Next slot is calculated from the configured interval, not proof of a running scheduler. Open goals, pauses, missing checks, and unresolved claims can block dispatch.')),
        card('Routine execution history', table(['Project / routine', 'Slot', 'Status', 'Result', 'Configuration'], d.routine_runs.map(r => [
          `${r.target} / ${r.name}`, r.slot, state(r.status), r.detail || 'No result yet', r.config.slice(0, 12),
        ]))),
        card('Next capabilities', h('p', null, 'Post-work audits, committed-code investigations, and GitHub discovery should feed the same ideas and work pipeline. Dedicated controls and budget policies for these workflows are not implemented yet.'),
          link('See recorded reviews and proposals →', '#ideas')));
    });
  }
  function svgNode(tag, attrs, value) {
    const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs || {}).forEach(([k,v]) => n.setAttribute(k, v));
    if (value !== undefined) n.textContent = value;
    return n;
  }
  function chart(points, series, label) {
    const known = points.some(p => series.some(s => s.value(p) !== null));
    if (!known) return empty('No reported values for this graph. Unknown is not zero.');
    const width = 840, height = 240, left = 64, bottom = 38, top = 20, right = 20;
    const maximum = Math.max(1, ...points.flatMap(p => series.map(s => s.value(p) || 0)));
    const first = Date.parse(points[0].day), last = Date.parse(points[points.length - 1].day);
    const x = p => left + (last === first ? .5 : (Date.parse(p.day)-first)/(last-first))*(width-left-right);
    const y = v => top + (1-v/maximum)*(height-top-bottom);
    const svg = svgNode('svg', {viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': label, class: 'dash-chart'});
    svg.append(svgNode('title', {}, label + '. Values also available in the daily table.'));
    for (let n = 0; n <= 4; n++) {
      const v = maximum*n/4;
      svg.append(svgNode('line', {x1: left, x2: width-right, y1: y(v), y2: y(v), class: 'chart-grid'}));
      svg.append(svgNode('text', {x: left-8, y: y(v)+4, 'text-anchor': 'end', class: 'chart-label'}, v < 1 && v > 0 ? v.toFixed(4) : fmt(Math.round(v))));
    }
    series.forEach(s => {
      let segment = [];
      function flush() {if (segment.length) svg.append(svgNode('polyline', {points: segment.join(' '), fill: 'none', stroke: s.color, 'stroke-width': 2.5})); segment = [];}
      points.forEach(p => {
        const value = s.value(p);
        if (value === null) {flush(); return;}
        segment.push(`${x(p)},${y(value)}`);
      });
      flush();
      points.forEach(p => {const value = s.value(p); if (value === null) return;
        const dot = svgNode('circle', {cx: x(p), cy: y(value), r: 4, fill: s.color});
        dot.append(svgNode('title', {}, `${p.day} UTC · ${s.name}: ${value}`)); svg.append(dot);
      });
    });
    [points[0], points[points.length-1]].filter((p,i,a) => i === 0 || p.day !== a[0].day).forEach(p => svg.append(svgNode('text', {x: x(p), y: height-10, 'text-anchor': 'middle', class: 'chart-label'}, p.day)));
    return h('div', null, h('div', {class: 'dash-legend'}, series.map(s => h('span', null, h('i', {style: 'background:' + s.color}), s.name))), svg);
  }
  function Usage(main) {
    let days = 30, model = '', role = '', includeHistory = false;
    return shell(main, 'Usage and spending', 'Follow input, output, cache use, and known cost back to their recorded calls.', async (view, d, reload, valid) => {
      const params = new URLSearchParams({days});
      if (includeHistory) params.set('include_history', '1');
      if (target) params.set('target', target); if (model) params.set('model', model); if (role) params.set('role', role);
      const m = await api('metrics?' + params);
      if (!valid()) return;
      if ((model && !m.models.includes(model)) || (role && !m.roles.includes(role))) {
        if (!m.models.includes(model)) model = '';
        if (!m.roles.includes(role)) role = '';
        return reload();
      }
      const time = h('div', {class:'segmented-control','aria-label':'Time window'}, [[1,'Last day'],[7,'Last week'],[30,'Last month']].map(([n,label])=>h('button',{class:'filter-chip'+(days===n?' selected':''),'aria-pressed':String(days===n),onclick:()=>{days=n;reload();}},label))); 
      const modelPick = h('select', {'aria-label': 'Model', onchange: () => {model = modelPick.value; reload();}}, h('option', {value: ''}, 'All models'), m.models.map(n => h('option', {value: n, selected: model === n}, n)));
      const rolePick = h('select', {'aria-label': 'Role', onchange: () => {role = rolePick.value; reload();}}, h('option', {value: ''}, 'All roles'), m.roles.map(n => h('option', {value: n, selected: role === n}, n)));
      const t = m.total;
      const cost = t.records && t.unknown_cost < t.records ? '$' + t.known_cost_usd.toFixed(4) : 'Unknown';
      const value = (key, missing) => p => p[missing] === p.records ? null : p[key];
      fill(view, h('div', {class: 'dash-controls'}, time, modelPick, rolePick, badge('UTC', 'badge--outline'), link('Configure tiers & fallbacks →', '#gateway-settings?tab=models')),
        m.baseline_at ? h('div', {class:'dash-controls'}, note('Measurement started '+stamp(m.baseline_at)), button(includeHistory?'Show current measurement':'Include earlier history',()=>{includeHistory=!includeHistory;reload();})) : null,
        h('div', {class: 'dash-stats usage-stats'}, stat('Reported input', fmt(t.input), `${t.missing_input} records missing input`), stat('Reported output', fmt(t.output), `${t.missing_output} records missing output`),
          stat('Known cost · USD', cost, `${t.unknown_cost} unknown · ${t.estimated_cost} estimated records`), stat('Failed call records', `${t.failed}/${t.records}`, 'Includes failed requests with recorded usage')),
        visuals.modelBars(m, name => {model=name;reload();}),
        h('details', {class:'dash-card usage-trends'}, h('summary',null,'Trends over time'), h('div', {class: 'dash-grid'}, card('Tokens over time', chart(m.daily, [
          {name: 'Input', color: '#ef5b65', value: value('input','missing_input')}, {name: 'Output', color: '#24b483', value: value('output','missing_output')}, {name: 'Cached input', color: '#bb7912', value: value('cached','missing_cache')}
        ], 'Daily reported tokens in UTC'), note('Days with records; lines connect observed days. Missing values create gaps. Partial totals exclude unreported usage.')),
          card('Known spending over time', chart(m.daily, [{name: 'Known USD (includes labeled estimates)', color: '#167f70', value: value('known_cost_usd','unknown_cost')}], 'Daily known US dollar costs in UTC'), note('No price is inferred from a model name or subscription. Unknown charges are excluded, never treated as free.')))),
        h('details',{class:'dash-card'},h('summary',null,'All models · detailed accounting'), table(['Requested tier', 'Reported model', 'Records', 'Input', 'Output', 'Known USD'], Object.values(m.by_tier).flatMap(g => g.models.map(x => [g.label,x.model || 'Unreported · requested '+x.requested_model,x.usage.records,fmt(x.usage.input),fmt(x.usage.output),x.usage.unknown_cost === x.usage.records ? 'Unknown' : x.usage.known_cost_usd.toFixed(4)])))),
        h('details', {class: 'dash-card'}, h('summary', null, 'Daily values · accessible chart data'), table(['UTC day', 'Records', 'Input', 'Output', 'Cached', 'Known USD', 'Unknown cost records'], m.daily.map(p => [p.day,p.records,p.missing_input === p.records ? 'Unknown' : p.input,p.missing_output === p.records ? 'Unknown' : p.output,p.missing_cache === p.records ? 'Unknown' : p.cached,p.unknown_cost === p.records ? 'Unknown' : p.known_cost_usd.toFixed(4),p.unknown_cost]))),
        h('details',{class:'dash-card'},h('summary',null,'Contributing records'), note(`Showing the latest ${m.records.length} of ${m.records_total} records in the filtered window. Totals include every matching record.`), callTable(m.records.slice().reverse())), note(m.note));
    });
  }
  return {overview: Overview, orchestration: Orchestration, agents: Agents, ideas: Ideas, models: Models, routines: Routines, usage: Usage};
};
