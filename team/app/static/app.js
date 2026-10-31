/* Team's app. Plain DOM, no build step.
 *
 * Work: the boss chat on the left (its questions and plans inline, a check
 * to accept a plan), the org chart on the right (Boss over Planner,
 * Implementer, Rescue; each goal with the tier it asked for, the model that
 * answered and that model's rank in its tier; every token).
 * Team, LiteLLM, Portkey: editors. Every change is reviewed as a diff with
 * Team's own checks first, then applied and committed; see team/app/edit.py.
 */
(function () {
  "use strict";

  // ---------- helpers ----------

  function h(tag, attrs) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        const v = attrs[k];
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") el.className = v;
        else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
        else if (k === "value") el.value = v;
        else if (k === "checked") el.checked = v;
        else el.setAttribute(k, v === true ? "" : v);
      }
    }
    for (let i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, c) {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) c.forEach(function (x) { add(el, x); });
    else el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }

  // replaceChildren, but flattening arrays and skipping null like h() does.
  function fill(el) {
    el.replaceChildren();
    for (let i = 1; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }

  async function api(path, body) {
    const opts = { headers: { "x-team-token": window.TEAM_TOKEN } };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const r = await fetch("/api/" + path, opts);
    const data = await r.json().catch(function () { return {}; });
    if (r.status === 403 && /reload the page/.test(data.error || "")) { location.reload(); }
    if (!r.ok) throw new Error(data.error || r.status + " " + r.statusText);
    return data;
  }

  let toastTimer = null;
  function toast(msg, bad) {
    const t = document.getElementById("toast");
    t.textContent = msg;
    t.className = "toast" + (bad ? " toast--bad" : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, bad ? 9000 : 3500);
  }
  function fail(e) { toast(String(e.message || e), true); }

  function fmt(n) {
    if (n === null || n === undefined) return "—";
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e4) return Math.round(n / 1e3) + "k";
    return String(n);
  }
  function num(n) {
    return h("span", { class: "num", title: n === null || n === undefined ? "not recorded" : n.toLocaleString() }, fmt(n));
  }
  function when(ts) {
    if (!ts) return "";
    const d = new Date(ts.replace(/([+-]\d\d)(\d\d)$/, "$1:$2"));
    return isNaN(d) ? ts : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  function clone(x) { return JSON.parse(JSON.stringify(x)); }

  // Bold, inline code, then text: enough for the boss's replies, built as DOM (never innerHTML).
  function inline(text) {
    const out = [];
    const re = /(`[^`]+`|\*\*[^*]+\*\*)/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) out.push(text.slice(last, m.index));
      const t = m[0];
      out.push(t[0] === "`" ? h("code", null, t.slice(1, -1)) : h("strong", null, t.slice(2, -2)));
      last = m.index + t.length;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }
  function prose(text) {
    const out = [];
    const chunks = text.split(/```[a-z]*\n?/);
    chunks.forEach(function (chunk, i) {
      if (i % 2) { out.push(h("pre", null, chunk.replace(/\n$/, ""))); return; }
      chunk.split(/\n\s*\n/).forEach(function (para) {
        if (!para.trim()) return;
        const lines = para.split("\n");
        if (lines.every(function (l) { return /^\s*([-*]|\d+\.)\s+/.test(l) || !l.trim(); })) {
          const ordered = /^\s*\d+\./.test(lines[0]);
          out.push(h(ordered ? "ol" : "ul", null, lines.filter(function (l) { return l.trim(); }).map(function (l) {
            return h("li", null, inline(l.replace(/^\s*([-*]|\d+\.)\s+/, "")));
          })));
        } else if (/^#{1,4}\s/.test(lines[0]) && lines.length === 1) {
          out.push(h("h4", null, inline(lines[0].replace(/^#+\s/, ""))));
        } else {
          const p = h("p");
          lines.forEach(function (l, j) { if (j) p.appendChild(h("br")); add(p, inline(l)); });
          out.push(p);
        }
      });
    });
    return out;
  }

  function badge(text, cls, title) { return h("span", { class: "badge " + (cls || ""), title: title }, text); }

  // ---------- routing ----------

  let stopPage = null;
  function route() {
    const page = (location.hash || "#").slice(1) || "work";
    document.querySelectorAll("#nav a").forEach(function (a) {
      a.classList.toggle("on", a.dataset.page === page);
    });
    if (stopPage) stopPage();
    stopPage = null;
    const main = document.getElementById("main");
    main.className = "page page--" + page;
    fill(main);
    const pages = { work: Work, team: TeamPage, litellm: LiteLLMPage, portkey: PortkeyPage };
    stopPage = (pages[page] || Work)(main) || null;
  }
  window.addEventListener("hashchange", route);

  function every(ms, fn) {
    let alive = true, timer = null;
    async function tick() {
      if (!alive) return;
      try { await fn(); } catch (e) { /* shown by the caller */ }
      if (alive) timer = setTimeout(tick, typeof ms === "function" ? ms() : ms);
    }
    tick();
    return function () { alive = false; clearTimeout(timer); };
  }

  // ---------- the sidebar: services, roles, tokens ----------

  let gatewayCache = null, boardCache = null, chatBusy = false, chatTools = [];
  async function services() {
    try {
      const o = await api("gateway");
      gatewayCache = o;
      fill(document.getElementById("services"), Object.keys(o.services).map(function (name) {
        const s = o.services[name];
        return h("a", { class: "svc " + (s.healthy ? "svc--ok" : "svc--bad"), href: "#" + name,
          title: name + " on " + s.host + ":" + s.port + (s.healthy ? ", healthy" : ", not answering") },
          h("span", { class: "dot" }), name, h("span", { class: "svc-state" }, s.healthy ? "up" : "down"));
      }));
    } catch (e) { /* the pages show their own errors */ }
  }
  every(30000, services);

  // The roles, like a squad list: who is working right now, and on what model.
  function drawSidebar() {
    const b = boardCache;
    if (!b) return;
    const cfg = b.configured, z = b.tokens.zones;
    const planning = chatTools.some(function (t) { return t.name === "team_propose" && !t.done; });
    const roles = [
      { key: "boss", title: "Boss", sub: cfg.boss.model || "?", working: chatBusy, note: b.waiting.length ? b.waiting.length + " plan" + (b.waiting.length > 1 ? "s" : "") + " for you" : null },
      { key: "lead", title: "Planner", sub: cfg.lead.model || "?", working: planning, note: planning ? "drafting" : null },
      { key: "implement", title: "Implementer", sub: (cfg.implement.tiers || []).join(" → "), working: b.columns.implement.length > 0, note: b.columns.implement.length ? b.columns.implement.length + " building" : null },
      { key: "rescue", title: "Rescue", sub: cfg.rescue.model || "?", working: b.columns.rescue.length > 0, note: b.columns.rescue.length ? b.columns.rescue.length + " rescuing" : null },
    ];
    fill(document.getElementById("roles"), roles.map(function (r) {
      return h("button", { class: "role" + (r.working ? " role--working" : ""), title: "Open " + r.title + " in Team", onclick: function () {
        try { sessionStorage.setItem("team-sel", r.key === "lead" ? "lead" : r.key === "rescue" ? "implement" : r.key); } catch (e) { /* private window */ }
        location.hash = "#team";
      } },
        h("span", { class: "role-dot" }),
        h("span", { class: "role-text" }, h("span", { class: "role-name" }, r.title), h("span", { class: "role-sub" }, r.sub)),
        h("span", { class: "role-state" }, r.note || (r.working ? "working" : "idle")));
    }));
    const t = b.tokens.total;
    fill(document.getElementById("side-tokens"),
      h("div", { class: "tile-label" }, "Tokens" + (b.tokens.since ? " since " + b.tokens.since.slice(5, 10).replace("-", "/") : "")),
      h("div", { class: "tile-big" }, fmt(t.tokens_in), h("span", { class: "tile-unit" }, " in")),
      h("div", { class: "tile-row" }, h("span", null, fmt(t.tokens_out) + " out"), h("span", null, (t.calls || 0) + " calls")),
      h("div", { class: "tile-row muted" }, "cache ", t.cache_read === null && t.cache_write === null ? "not recorded" : fmt(t.cache_read) + " / " + fmt(t.cache_write)));
  }
  async function pollSidebar() {
    try {
      const r = await Promise.all([api("board"), api("chat")]);
      boardCache = r[0]; chatBusy = !!r[1].current; chatTools = r[1].current ? r[1].current.tools : [];
      drawSidebar();
    } catch (e) { /* the pages show their own errors */ }
  }
  every(15000, pollSidebar);

  // ---------- Work: chat + chart ----------

  function Work(main) {
    const chatList = h("div", { class: "chat-list" });
    const inboxBox = h("div", { class: "chat-inbox" });
    const input = h("textarea", { class: "chat-input", rows: "3", placeholder: "Talk to the boss… (Enter sends, Shift+Enter for a new line)" });
    const sendBtn = h("button", { class: "btn btn--primary", onclick: send }, "Send");
    const chart = h("div", { class: "chart-wrap" }, h("div", { class: "muted" }, "Loading…"));
    let busy = false;

    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    });

    async function send() {
      const text = input.value.trim();
      if (!text || busy) return;
      try {
        await api("chat", { text: text });
        input.value = "";
        busy = true;
        await loadChat();
      } catch (e) { fail(e); }
    }

    async function newChat() {
      if (!confirm("Start a new conversation with the boss? The old one is kept on disk.")) return;
      try { await api("chat/new", {}); lastSig = ""; await loadChat(); } catch (e) { fail(e); }
    }

    function toolChip(t) {
      const label = t.name.replace(/^team_/, "");
      return h("span", { class: "tool" + (t.done ? "" : " tool--live"), title: t.input ? JSON.stringify(t.input).slice(0, 400) : "" },
        t.done ? "✓ " : "… ", label, t.done && t.seconds ? " " + Math.round(t.seconds) + "s" : "",
        !t.done && t.name === "team_propose" ? " (the planner is drafting; this takes minutes)" : "");
    }

    function message(m, live) {
      if (m.role === "you") return h("div", { class: "msg msg--you" }, h("div", { class: "bubble" }, prose(m.text)), h("div", { class: "meta" }, when(m.at)));
      const t = m.tokens;
      return h("div", { class: "msg msg--boss" + (live ? " msg--live" : "") },
        h("div", { class: "who" }, "Boss"),
        m.tools && m.tools.length ? h("div", { class: "tools" }, m.tools.map(toolChip)) : null,
        h("div", { class: "bubble" }, m.text ? prose(m.text) : (live ? h("span", { class: "typing" }, h("i"), h("i"), h("i")) : null),
          m.error ? h("div", { class: "error" }, m.error) : null),
        h("div", { class: "meta" }, when(m.done || m.at),
          t ? h("span", { title: "this message: input, output, cache read, cache write" }, " · in ", num(t.input), " out ", num(t.output),
            " · cache ", num(t.cache_read), " / ", num(t.cache_write)) : null));
    }

    let chatData = null, plans = [], lastSig = "";
    const planEls = {};

    function stamp(ts) { return ts ? Date.parse(ts.replace(/([+-]\d\d)(\d\d)$/, "$1:$2")) : NaN; }

    // The conversation with each plan placed right after the boss message that proposed it.
    function drawChat() {
      if (!chatData) return;
      const c = chatData;
      const sig = JSON.stringify([c.messages.length, c.current && c.current.text, c.current && c.current.tools,
        plans.map(function (p) { return [p.id, p.status]; })]);
      if (sig === lastSig) return;
      lastSig = sig;
      const atBottom = chatList.scrollHeight - chatList.scrollTop - chatList.clientHeight < 80;
      const first = c.messages.length ? stamp(c.messages[0].at) : Infinity;
      const live = function (p) { return p.status === "waiting" || p.status === "accepted"; };
      const shown = plans.filter(function (p) { return live(p) || stamp(p.created) >= first; })
        .sort(function (a, b) { return stamp(a.created) - stamp(b.created); });
      const after = {}, tail = [];
      shown.forEach(function (p) {
        const t = stamp(p.created);
        let k = -1;
        c.messages.forEach(function (m, i) {
          if (m.role === "boss" && stamp(m.at) <= t && t <= stamp(m.done || m.at) + 1000) k = i;
        });
        if (k < 0) c.messages.forEach(function (m, i) { if (stamp(m.at) <= t) k = i; });
        if (k < 0 || (c.current && t >= stamp(c.current.at))) tail.push(p);  // proposed in the turn still running
        else (after[k] = after[k] || []).push(p);
      });
      fill(chatList);
      if (!c.messages.length && !c.current && !shown.length) {
        chatList.appendChild(h("div", { class: "empty" },
          h("p", null, "Tell the boss what you want built. It asks what it needs, writes the goals, and has the planner split them into parts."),
          h("p", { class: "muted" }, "Each plan shows up here, goal by goal and part by part, with Accept, Send back and Dismiss.")));
      }
      c.messages.forEach(function (m, i) {
        chatList.appendChild(message(m));
        (after[i] || []).forEach(function (p) { chatList.appendChild(planEl(p)); });
      });
      if (c.current) chatList.appendChild(message(c.current, true));
      tail.forEach(function (p) { chatList.appendChild(planEl(p)); });
      if (atBottom || busy) chatList.scrollTop = chatList.scrollHeight;
    }

    // Reused while a plan's status is unchanged, so an open section or a half-typed note survives a refresh.
    function planEl(p) {
      const key = p.status + JSON.stringify(p.goal_ids);
      if (!planEls[p.id] || planEls[p.id].key !== key) planEls[p.id] = { key: key, el: planCard(p, reload) };
      return planEls[p.id].el;
    }

    async function loadChat() {
      chatData = await api("chat");
      busy = !!chatData.current;
      if (chatBusy !== busy) { chatBusy = busy; chatTools = chatData.current ? chatData.current.tools : []; drawSidebar(); }
      sendBtn.disabled = busy;
      sendBtn.textContent = busy ? "Answering…" : "Send";
      drawChat();
    }

    async function loadInbox() {
      const box = await api("inbox");
      plans = box.plans;
      drawChat();
      fill(inboxBox);
      if (box.decisions.length) inboxBox.appendChild(h("div", { class: "inbox-title" }, "Questions for you"));
      box.decisions.forEach(function (d) { inboxBox.appendChild(decisionCard(d, reload)); });
    }

    async function loadBoard() {
      const b = await api("board");
      boardCache = b; drawSidebar();
      fill(chart, Chart(b, reload));
    }

    async function reload() {
      await Promise.all([loadInbox().catch(fail), loadBoard().catch(fail), loadChat().catch(fail)]);
    }

    main.appendChild(h("section", { class: "pane pane--chat" },
      h("div", { class: "pane-head" },
        h("div", { class: "pane-title" }, h("span", { class: "avatar" }, "B"), h("div", null, h("h2", null, "Boss"), h("div", { class: "muted" }, "plans the work with you"))),
        h("button", { class: "btn btn--ghost btn--small", onclick: newChat }, "New conversation")),
      chatList, inboxBox,
      h("div", { class: "composer" }, input, sendBtn)));
    main.appendChild(h("section", { class: "pane pane--chart" }, chart));

    const stops = [
      every(function () { return busy ? 1000 : 5000; }, function () { return loadChat().catch(fail); }),
      every(function () { return busy ? 3000 : 6000; }, function () { return Promise.all([loadInbox(), loadBoard()]).catch(fail); }),
    ];
    input.focus();
    return function () { stops.forEach(function (s) { s(); }); };
  }

  const PLAN_STATUS = {
    waiting: ["Waiting for you", "badge--live"],
    accepted: ["Accepted: starts within a minute", "badge--ok"],
    releasing: ["Starting", "badge--ok"],
    released: ["Building: see the chart", "badge--ok"],
    dismissed: ["Dismissed", "badge--outline"],
    failed: ["Could not start", "badge--bad"],
  };

  // A plan, goal by goal and part by part, with what you can do about it.
  function planCard(c, after) {
    const st = PLAN_STATUS[c.status] || [c.status, "badge--outline"];
    const waiting = c.status === "waiting";
    const nParts = c.goals.reduce(function (n, g) { return n + g.parts.length; }, 0);
    let goalsOpen = waiting;
    const goalsBox = h("div", { class: "plan-goals", hidden: !goalsOpen }, c.goals.map(function (g) {
      return h("div", { class: "plan-goal" },
        h("div", { class: "plan-goal-head" }, h("span", { class: "goal-n" }, "Goal " + g.n),
          h("span", null, g.goal),
          g.after && g.after.length ? badge("after goal " + g.after.join(", "), "badge--outline") : badge("starts right away", "badge--outline")),
        g.plan ? h("div", { class: "muted plan-why" }, "Planner: " + g.plan) : null,
        h("div", { class: "plan-parts" }, g.parts.map(partView)));
    }));
    const note = h("textarea", { class: "chat-input", rows: "2", placeholder: "What should change? The boss re-proposes it." });
    const sendBack = h("div", { class: "sendback", hidden: true }, note,
      h("div", { class: "actions" },
        h("button", { class: "btn btn--primary", onclick: async function () {
          try { await api("plans/" + c.id + "/sendback", { text: note.value }); toast("Sent back to the boss."); after(); } catch (e) { fail(e); }
        } }, "Send back to the boss"),
        h("button", { class: "btn btn--ghost", onclick: function () { sendBack.hidden = true; } }, "Cancel")));
    const toggle = h("button", { class: "linkish plan-toggle", onclick: function () {
      goalsOpen = !goalsOpen; goalsBox.hidden = !goalsOpen; toggle.textContent = (goalsOpen ? "▾ " : "▸ ") + label;
    } });
    const label = c.goals.length + " goal" + (c.goals.length > 1 ? "s" : "") + ", " + nParts + " part" + (nParts > 1 ? "s" : "") + " for free workers";
    toggle.textContent = (goalsOpen ? "▾ " : "▸ ") + label;
    return h("div", { class: "plan plan--" + c.status },
      h("div", { class: "plan-top" }, h("span", { class: "plan-kicker" }, "Plan " + c.id), badge(st[0], st[1])),
      h("div", { class: "plan-headline" }, c.headline),
      h("div", { class: "summary" }, prose(c.summary || "")),
      (c.warnings || []).length ? h("div", { class: "plan-warn" }, h("strong", null, "Check before accepting"),
        h("ul", null, c.warnings.map(function (w) { return h("li", null, w); }))) : null,
      c.error ? h("div", { class: "problems" }, c.error) : null,
      toggle, goalsBox,
      waiting ? h("div", { class: "plan-actions" },
        h("button", { class: "btn btn--primary btn--big", onclick: async function (e) {
          e.target.disabled = true;
          try { await api("plans/" + c.id + "/approve", {}); toast("Accepted. Its goals start within a minute; watch the chart."); after(); }
          catch (err) { fail(err); e.target.disabled = false; }
        } }, "✓ Accept plan"),
        h("button", { class: "btn", onclick: function () { sendBack.hidden = false; note.focus(); } }, "Send back with changes"),
        h("button", { class: "btn btn--ghost", onclick: async function () {
          if (!confirm("Dismiss plan " + c.id + "? Nothing in it will be built.")) return;
          try { await api("plans/" + c.id + "/dismiss", {}); after(); } catch (e) { fail(e); }
        } }, "Dismiss")) : null,
      sendBack);
  }

  function partView(p) {
    return h("div", { class: "part" },
      h("div", null, h("strong", null, p.title)),
      h("div", { class: "muted" }, p.role + " on " + p.model + " · " + p.steps + " steps" + (p.depends_on && p.depends_on.length ? " · after " + p.depends_on.join(", ") : "")),
      h("div", { class: "tags" }, p.toolsets.map(function (t) { return badge(t); }), p.skills.map(function (s) { return badge(s, "badge--outline"); })),
      h("div", { class: "muted" }, "owns ", h("code", null, p.owned_paths.join(", "))),
      p.context.length ? h("div", { class: "muted" }, "reads " + p.context.join(", ")) : null);
  }

  function decisionCard(d, after) {
    let open = false;
    const body = h("div", { class: "card-body", hidden: true },
      h("div", { class: "summary" }, prose(d.context || "")),
      h("div", { class: "actions" },
        d.options.map(function (o) {
          return h("button", { class: "btn " + (o === d.recommendation ? "btn--primary" : ""), onclick: async function () {
            try { await api("decisions/" + d.id + "/answer", { answer: o }); after(); } catch (e) { fail(e); }
          } }, o);
        }),
        h("button", { class: "btn btn--ghost", onclick: async function () {
          try { await api("decisions/" + d.id + "/dismiss", {}); after(); } catch (e) { fail(e); }
        } }, "Dismiss")));
    return h("div", { class: "card" },
      h("div", { class: "card-head" },
        h("button", { class: "card-title", onclick: function () { open = !open; body.hidden = !open; } }, d.question),
        badge(d.source, "badge--outline")),
      body);
  }

  // ---------- the chart ----------

  function tokens(t, calls) {
    t = t || {};
    const none = t.cache_read === null && t.cache_write === null;
    return h("div", { class: "tokens" },
      h("span", null, "in ", num(t.tokens_in)), h("span", null, "out ", num(t.tokens_out)),
      h("span", { title: "cache read / cache write" }, "cache ", none ? h("span", { class: "muted" }, "not recorded") : [num(t.cache_read), " / ", num(t.cache_write)]),
      calls === false ? null : h("span", { class: "muted" }, (t.calls || 0) + " calls"));
  }

  function rank(m) {
    if (!m.rank) return null;
    return h("span", { class: "rank " + (m.rank === 1 ? "rank--top" : "rank--down"),
      title: m.rank === 1 ? "the tier's first choice" : "not the tier's first choice" }, "#" + m.rank + (m.of ? " of " + m.of : ""));
  }

  function routeView(a) {
    const r = a.route || {}, models = r.models || [];
    return h("div", { class: "route" },
      h("span", { class: "muted" }, a.asked || "?", " → "),
      models.length ? models.map(function (m) { return h("span", { class: "model" }, m.model, " ", rank(m)); })
        : h("span", { class: "muted" }, a.outcome ? "no answer recorded" : "working"),
      r.top_tried === false && models.some(function (m) { return m.rank > 1; })
        ? h("span", { class: "warn", title: "LiteLLM never tried the tier's #1 during this attempt" }, "#1 not tried") : null);
  }

  function outcome(o) {
    if (!o) return badge("running", "badge--live");
    return h("span", { class: "outcome outcome--" + o }, o === "pass" ? "✓ pass" : "✗ " + o);
  }

  function attemptView(a) {
    const r = a.route || {};
    return h("div", { class: "attempt" },
      h("div", { class: "attempt-head" }, h("span", { class: "n" }, a.zone === "lead" ? "plan" : "#" + a.attempt), routeView(a), outcome(a.outcome)),
      (r.tried || []).length > 1 ? h("div", { class: "muted" }, "tried: " + r.tried.map(function (t) {
        return (t.ok ? "✓ " : "✗ ") + t.model + (t.rank ? " #" + t.rank : "");
      }).join("  →  ")) : null,
      (r.passed_over || []).length ? h("div", { class: "muted" }, "passed over: " + r.passed_over.slice(0, 3).map(function (p) {
        return "#" + p.rank + " " + p.model + " (" + p.why + ")";
      }).join("; ")) : null,
      r.by === "time" ? h("div", { class: "muted", title: "no session id matched LiteLLM's log; matched by tier and time" }, "model matched by time") : null,
      a.detail ? h("div", { class: "detail" }, a.detail) : null,
      tokens(a.tokens));
  }

  const openGoals = {};
  function goalCard(g) {
    const last = g.attempts[g.attempts.length - 1];
    const body = h("div", { class: "card-body", hidden: !openGoals[g.id] },
      h("div", { class: "kv-line" }, h("span", { class: "muted" }, "check "), h("code", null, g.check)),
      h("div", { class: "kv-line" }, h("span", { class: "muted" }, "branch "), h("code", null, g.branch)),
      g.order_bytes ? h("div", { class: "muted" }, "work order v" + g.order_version + ", " + fmt(g.order_bytes) + " bytes") : null,
      (g.files && g.files.length) ? h("div", { class: "goal-files" }, h("span", { class: "muted" }, "changed "), g.files.map(function (f) { return h("div", null, h("code", null, f)); })) : null,
      g.planned ? attemptView(g.planned) : null,
      g.attempts.map(attemptView),
      g.detail ? h("div", { class: "detail" }, g.detail) : null);
    const text = h("div", { class: "goal-text" + (openGoals[g.id] ? "" : " clamp"), title: g.goal }, g.goal);
    return h("div", { class: "card goal goal--" + g.station },
      h("button", { class: "goal-head", onclick: function () {
        openGoals[g.id] = !openGoals[g.id]; body.hidden = !openGoals[g.id]; text.classList.toggle("clamp", !openGoals[g.id]);
      } },
        h("div", { class: "goal-top" }, h("span", { class: "goal-id" }, "#" + g.id), g.outcome ? outcome(g.outcome) : (last ? badge("attempt " + last.attempt, "badge--live") : badge("waiting", "badge--outline"))),
        text),
      h("div", { class: "goal-meta" },
        last ? routeView(last) : h("div", { class: "muted" }, "waiting for its dependencies"),
        g.owns.length ? h("div", { class: "tags" }, g.owns.map(function (f) { return h("code", { class: "chip", title: f }, f.split("/").pop()); })) : null),
      body);
  }

  const COLORS = { lead: "violet", implement: "blue", rescue: "amber", done: "green" };

  function column(key, title, sub, items, extra) {
    return h("div", { class: "col col--" + COLORS[key] },
      h("div", { class: "col-head" },
        h("span", { class: "col-dot" }), h("span", { class: "col-title" }, title),
        h("span", { class: "col-count" }, String(items.length))),
      h("div", { class: "col-sub muted" }, sub),
      h("div", { class: "col-body" },
        extra || null,
        items.length ? items.map(goalCard) : (extra ? null : h("div", { class: "empty-col" }, "Nothing here"))));
  }

  function tile(label, value, sub, cls) {
    return h("div", { class: "tile " + (cls || "") },
      h("div", { class: "tile-label" }, label),
      h("div", { class: "tile-value" }, value),
      sub ? h("div", { class: "tile-sub muted" }, sub) : null);
  }

  const ZONES = { boss: "Boss", lead: "Planner", implement: "Implementer", rescue: "Rescue", other: "Not attributed" };
  let showTiers = false, showTokens = false;

  function Chart(b) {
    const cfg = b.configured, z = b.tokens.zones, t = b.tokens.total;
    const landed = b.done.filter(function (g) { return g.station === "landed"; });
    const stopped = b.done.filter(function (g) { return g.station !== "landed"; });
    const building = b.columns.implement.length + b.columns.rescue.length;
    const tiersBox = h("div", { class: "tiers", hidden: !showTiers }, b.tiers.map(function (tr) {
      return h("div", { class: "card tier" }, h("div", { class: "tier-name" }, tr.tier),
        tr.models.map(function (m) {
          const failing = m.last_fail && m.last && m.last_fail === m.last;
          return h("div", { class: "tier-row" + (m.rank === 1 ? " tier-row--top" : "") },
            h("span", { class: "n" }, "#" + m.rank), h("span", { class: "tier-model" }, m.model),
            h("span", { class: "muted", title: "successful calls / calls in LiteLLM's log" }, m.calls ? (m.ok || 0) + "/" + m.calls : "unused"),
            m.rl ? h("span", { class: "warn" }, m.rl + " rate-limited") : null,
            failing ? h("span", { class: "warn", title: m.last_fail }, "last call failed") : null);
        }));
    }));
    const tokensBox = h("div", { class: "card token-table", hidden: !showTokens },
      h("div", { class: "token-row token-row--total" }, h("span", null, "All"), tokens(t)),
      Object.keys(ZONES).map(function (k) { return h("div", { class: "token-row" }, h("span", null, ZONES[k]), tokens(z[k])); }));
    const toggle = function (label, get, set, box) {
      const btn = h("button", { class: "linkish", onclick: function () { set(!get()); box.hidden = !get(); btn.textContent = (get() ? "▾ " : "▸ ") + label; } }, (get() ? "▾ " : "▸ ") + label);
      return btn;
    };
    return h("div", { class: "board" },
      h("div", { class: "board-head" },
        h("h1", null, "Work"),
        h("div", { class: "muted" }, b.waiting.length ? b.waiting.length + " plan" + (b.waiting.length > 1 ? "s" : "") + " waiting for you in the chat" : building ? building + " goal" + (building > 1 ? "s" : "") + " building" : "Quiet. Tell the boss what to build.")),
      h("div", { class: "tiles" },
        tile("Waiting for you", String(b.waiting.length), "plans to accept", b.waiting.length ? "tile--accent" : ""),
        tile("Building", String(building), "goals with a worker on them"),
        tile("Landed", [String(landed.length), stopped.length ? h("span", { class: "tile-dim" }, " / " + stopped.length + " stopped") : null], "check passed, merged"),
        tile("Tokens in", fmt(t.tokens_in), fmt(t.tokens_out) + " out · " + (t.calls || 0) + " calls"),
        tile("Cache", t.cache_read === null && t.cache_write === null ? "—" : fmt(t.cache_read) + " / " + fmt(t.cache_write), t.cache_read === null && t.cache_write === null ? "not recorded yet" : "read / write")),
      h("div", { class: "cols" },
        column("lead", "Planner", "work orders, tools, context · " + (cfg.lead.model || "?"), b.columns.lead),
        column("implement", "Implementer", "builds each part · " + (cfg.implement.tiers || []).join(" → "), b.columns.implement),
        column("rescue", "Rescue", "after " + (cfg.rescue.after_failures || "?") + " failed attempts · " + (cfg.rescue.model || "?"), b.columns.rescue),
        column("done", "Landed", landed.length + " landed" + (stopped.length ? ", " + stopped.length + " stopped" : ""), b.done)),
      h("div", { class: "board-more" },
        toggle("Tokens by role", function () { return showTokens; }, function (v) { showTokens = v; }, tokensBox),
        toggle("Tiers: LiteLLM's order, #1 first", function () { return showTiers; }, function (v) { showTiers = v; }, tiersBox)),
      tokensBox, tiersBox);
  }

  // ---------- review / apply ----------

  function diffView(text) {
    if (!text) return h("div", { class: "muted" }, "No changes.");
    return h("pre", { class: "diff" }, text.split("\n").map(function (l) {
      const cls = l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : "";
      return h("span", { class: cls }, l + "\n");
    }));
  }

  // A sheet over the page: the diff, the checks' result, Apply.
  function reviewSheet(r, onApply, extra) {
    const blocked = (r.problems || []).length > 0 || r.dirty || !r.diff;
    const sheet = h("div", { class: "sheet" });
    const close = function () { sheet.remove(); };
    const applyBtn = h("button", { class: "btn btn--primary", disabled: blocked, onclick: async function () {
      applyBtn.disabled = true; applyBtn.textContent = "Applying…";
      try { await onApply(); close(); } catch (e) { fail(e); applyBtn.disabled = false; applyBtn.textContent = "Apply"; }
    } }, "Apply");
    sheet.appendChild(h("div", { class: "sheet-card" },
      h("div", { class: "sheet-head" }, h("h2", null, "Review " + r.path), h("button", { class: "btn btn--ghost", onclick: close }, "Close")),
      (r.problems || []).length ? h("div", { class: "problems" }, h("strong", null, "Refused by Team's checks:"), h("ul", null, r.problems.map(function (p) { return h("li", null, p); }))) : null,
      r.dirty ? h("div", { class: "problems" }, r.path + " has uncommitted changes from someone else (another session?). Commit or discard them first, so this apply does not sweep them in.") : null,
      !blocked ? h("div", { class: "ok" }, "Team's checks pass. Apply writes it, " + (extra || "syncs what depends on it") + ", and commits it (undo from History).") : null,
      diffView(r.diff),
      h("div", { class: "actions" }, applyBtn, h("button", { class: "btn btn--ghost", onclick: close }, "Cancel"))));
    document.body.appendChild(sheet);
  }

  // ---------- Team page ----------

  function TeamPage(main) {
    const nav = h("div", { class: "subnav" });
    const view = h("section", { class: "editor" }, h("div", { class: "muted" }, "Loading…"));
    main.appendChild(h("div", { class: "page-head" }, h("h1", null, "Team"), h("div", { class: "muted" }, "Each role's instructions, tools and model, and how Team orchestrates them. Every change is reviewed, then committed.")));
    main.appendChild(nav); main.appendChild(view);
    let data = null, selected = sessionStorage.getItem("team-sel") || "settings";

    async function load() {
      data = await api("team");
      drawNav(); draw();
    }

    function pick(key) { selected = key; try { sessionStorage.setItem("team-sel", key); } catch (e) { /* private window */ } drawNav(); draw(); }

    function drawNav() {
      fill(nav,
        data.roles.map(function (r) {
          return h("button", { class: "pill" + (selected === r.name ? " on" : ""), onclick: function () { pick(r.name); } },
            r.title, r.problem ? h("span", { class: "warn" }, " !") : null);
        }),
        h("span", { class: "subnav-gap" }),
        h("button", { class: "pill" + (selected === "settings" ? " on" : ""), onclick: function () { pick("settings"); } }, "Settings"),
        h("button", { class: "pill" + (selected === "history" ? " on" : ""), onclick: function () { pick("history"); } }, "History & undo"));
    }

    function draw() {
      if (selected === "settings") return drawSettings();
      if (selected === "history") return drawHistory();
      const r = data.roles.find(function (x) { return x.name === selected; }) || data.roles[0];
      if (!r) return fill(view, h("div", { class: "muted" }, "No roles."));
      drawRole(r);
    }

    function kv(k, v) { return v === undefined || v === null || v === "" ? null : h("div", { class: "kv" }, h("span", { class: "muted" }, k), h("span", null, v)); }

    function drawRole(r) {
      const fileBox = h("div", { class: "file-box" });
      const tabs = h("div", { class: "tabs" }, r.files.map(function (f, i) {
        const label = f.split("/").slice(3).join("/");
        return h("button", { class: "tab", "data-path": f, onclick: function () { openFile(f); } }, label);
      }));
      async function openFile(path) {
        tabs.querySelectorAll(".tab").forEach(function (t) { t.classList.toggle("on", t.dataset.path === path); });
        fill(fileBox, h("div", { class: "muted" }, "Loading…"));
        try { fill(fileBox, fileEditor(await api("team/file?path=" + encodeURIComponent(path)), load)); } catch (e) { fail(e); }
      }
      fill(view, 
        h("div", { class: "role-head" },
          h("h2", null, r.title, " ", badge(r.status || "?", r.status === "active" ? "badge--live" : "badge--outline")),
          h("p", { class: "muted" }, r.does),
          r.problem ? h("div", { class: "problems" }, r.problem) : null,
          h("div", { class: "kvs" },
            kv("runs on", r.runs_on.harness + (r.runs_on.model ? " · " + r.runs_on.model : "")),
            r.models ? kv("free tiers", (r.models.candidates || []).join(" → ") + (r.models.pinned ? " (pinned " + r.models.pinned + ")" : "")) : null,
            r.escalation ? kv("rescue", (r.escalation.model || r.escalation.harness) + " after " + r.escalation.after_failures + " failures") : null,
            kv("tools", (r.tools || []).join(", ")),
            kv("budget", Object.keys(r.budget || {}).map(function (k) { return k + " " + r.budget[k]; }).join(" · ")))),
        tabs, fileBox);
      openFile(r.files[0]);
    }

    function drawSettings() {
      const s = data.settings, values = clone(s.values);
      const rows = s.fields.map(function (f) {
        const v = values[f.key];
        const input = f.type === "bool"
          ? h("input", { type: "checkbox", checked: !!v, onchange: function (e) { values[f.key] = e.target.checked; } })
          : h("input", { type: f.type === "int" ? "number" : "text", value: v, min: f.type === "int" ? "1" : null, onchange: function (e) {
            values[f.key] = f.type === "int" ? parseInt(e.target.value, 10) : e.target.value;
          } });
        return h("label", { class: "setting" }, h("div", null, h("code", null, f.key), v !== f.default ? badge("changed from " + f.default, "badge--outline") : null),
          h("div", { class: "muted" }, f.help), input);
      });
      fill(view, h("h2", null, "Settings"), h("p", { class: "muted" }, "How Team orchestrates: the loop, the planner, the boss, escalation. Saved in " + s.path + "."),
        s.problem ? h("div", { class: "problems" }, s.problem) : null,
        h("div", { class: "settings" }, rows),
        h("div", { class: "actions sticky" }, h("button", { class: "btn btn--primary", onclick: async function () {
          try {
            const draft = await api("team/settings", { values: values });
            const r = await api("team/review", { path: draft.path, text: draft.text });
            reviewSheet(r, async function () {
              await api("team/apply", { path: draft.path, text: draft.text, base: r.base });
              toast("Applied and committed."); await load();
            }, "takes effect on the next tick");
          } catch (e) { fail(e); }
        } }, "Review changes")));
    }

    async function drawHistory() {
      fill(view, h("h2", null, "History & undo"), h("div", { class: "muted" }, "Loading…"));
      try {
        const hist = await api("history");
        fill(view, h("h2", null, "History & undo"),
          h("p", { class: "muted" }, "Every change applied here is one git commit. Undo puts that file back as it was before, checked and committed the same way."),
          hist.changes.length ? h("div", { class: "history" }, hist.changes.map(function (c) {
            return h("div", { class: "history-row" }, h("code", null, c.commit.slice(0, 10)), h("span", null, c.subject.replace("Team app: ", "")),
              h("span", { class: "muted" }, new Date(c.at).toLocaleString()),
              h("button", { class: "btn btn--small", onclick: async function () {
                if (!confirm("Undo " + c.subject + "?")) return;
                try { await api("undo", { commit: c.commit }); toast("Undone and committed."); drawHistory(); } catch (e) { fail(e); }
              } }, "Undo"));
          })) : h("div", { class: "muted" }, "Nothing applied from this app yet."));
      } catch (e) { fail(e); }
    }

    load().catch(fail);
  }

  function fileEditor(file, after) {
    const area = h("textarea", { class: "code", spellcheck: "false", value: file.text });
    const status = h("span", { class: "muted" }, "");
    area.addEventListener("input", function () { status.textContent = area.value === file.text ? "" : "edited, not applied"; });
    return h("div", { class: "file-editor" }, area,
      h("div", { class: "actions" },
        h("button", { class: "btn btn--primary", onclick: async function () {
          try {
            const r = await api("team/review", { path: file.path, text: area.value });
            reviewSheet(r, async function () {
              const res = await api("team/apply", { path: file.path, text: area.value, base: file.base });
              file.text = area.value; file.base = res.base; status.textContent = "";
              toast("Applied and committed" + (res.synced && res.synced.length ? "; profile synced: " + res.synced.join(", ") : "") + ".");
              after();
            }, "syncs the role's Hermes profile");
          } catch (e) { fail(e); }
        } }, "Review changes"),
        h("button", { class: "btn btn--ghost", onclick: function () { area.value = file.text; status.textContent = ""; } }, "Discard edits"),
        status));
  }

  // ---------- the catalog draft shared by LiteLLM and Portkey ----------

  const gw = { saved: null, draft: null, base: null, overview: null };

  async function loadGateway(force) {
    if (!force && gw.draft) return;
    const o = await api("gateway");
    gw.overview = o; gw.saved = o.catalog; gw.draft = clone(o.catalog); gw.base = o.base;
  }
  function dirtyDraft() { return JSON.stringify(gw.draft) !== JSON.stringify(gw.saved); }

  function draftBar(redraw) {
    const changed = dirtyDraft();
    return h("div", { class: "draft-bar" + (changed ? " draft-bar--on" : "") },
      h("span", null, changed ? "Unsaved changes to catalog.json (shared by LiteLLM and Portkey)" : "catalog.json matches what is running"),
      h("button", { class: "btn btn--ghost", disabled: !changed, onclick: function () { gw.draft = clone(gw.saved); redraw(); } }, "Discard"),
      h("button", { class: "btn btn--primary", disabled: !changed, onclick: async function () {
        try {
          const r = await api("gateway/review", { catalog: gw.draft });
          const ch = r.summary.changes;
          r.problems = r.problems || [];
          reviewSheet(r, async function () {
            const res = await api("gateway/apply", { catalog: gw.draft, base: gw.base });
            toast("Applied: " + res.files + " gateway files rebuilt" + (res.restarted.length ? ", LiteLLM restarted" : "") + ", committed.");
            await loadGateway(true); redraw(); services();
          }, "rebuilds " + (ch.changed.length + ch.added.length + ch.removed.length) + " generated gateway file(s), restarts LiteLLM if its config changed");
        } catch (e) { fail(e); }
      } }, "Review changes"));
  }

  function serviceCard(name) {
    const s = gw.overview.services[name];
    return h("div", { class: "svc-card" },
      h("div", null, h("strong", null, name), " ", badge(s.healthy ? "healthy" : "not answering", s.healthy ? "badge--ok" : "badge--bad"),
        h("span", { class: "muted" }, " " + s.host + ":" + s.port + (s.launchd && s.launchd.pid ? " · pid " + s.launchd.pid : ""))),
      h("button", { class: "btn btn--small", onclick: async function (e) {
        e.target.disabled = true; e.target.textContent = "Restarting…";
        try { const r = await api("gateway/restart", { service: name }); toast(name + (r.healthy ? " is back in " + r.seconds + "s" : " did not come back; see its log"), !r.healthy); await loadGateway(true); services(); }
        catch (err) { fail(err); }
        e.target.disabled = false; e.target.textContent = "Restart";
      } }, "Restart"));
  }

  function policyFields(obj, redraw) {
    return h("div", { class: "settings" }, Object.keys(obj).filter(function (k) { return !/(_why|^about)$/.test(k) && k !== "about"; }).map(function (k) {
      const v = obj[k], why = obj[k + "_why"];
      let input;
      if (typeof v === "boolean") input = h("input", { type: "checkbox", checked: v, onchange: function (e) { obj[k] = e.target.checked; redraw(); } });
      else if (typeof v === "number") input = h("input", { type: "number", value: v, onchange: function (e) { obj[k] = Number(e.target.value); redraw(); } });
      else if (typeof v === "string" || v === null) input = h("input", { type: "text", value: v === null ? "" : v, placeholder: v === null ? "(default)" : null, onchange: function (e) { obj[k] = e.target.value === "" ? null : e.target.value; redraw(); } });
      else input = jsonInput(v, function (nv) { obj[k] = nv; redraw(); });
      return h("label", { class: "setting" }, h("code", null, k), why ? h("div", { class: "muted" }, why) : null, input);
    }));
  }

  function jsonInput(v, set) {
    const area = h("textarea", { class: "code code--small", spellcheck: "false", value: JSON.stringify(v, null, 1) });
    area.addEventListener("change", function () {
      try { set(JSON.parse(area.value)); area.classList.remove("bad"); } catch (e) { area.classList.add("bad"); toast("Not valid JSON: " + e.message, true); }
    });
    return area;
  }

  function rawCatalog(redraw) {
    let open = false;
    const box = h("div", { hidden: true });
    return h("div", { class: "raw" },
      h("button", { class: "linkish", onclick: function () {
        open = !open; box.hidden = !open;
        if (open) fill(box, jsonInput(gw.draft, function (nv) { gw.draft = nv; redraw(); }));
      } }, "▸ Edit the whole catalog.json as JSON (everything, including what has no form above)"),
      box);
  }

  // ---------- LiteLLM page ----------

  function LiteLLMPage(main) {
    async function draw() {
      await loadGateway(false);
      const cat = gw.draft, order = {};
      cat.tiers.forEach(function (t) { order[t] = []; });
      cat.models.forEach(function (m) { if (m.tier && order[m.tier]) order[m.tier].push(m); });
      Object.keys(order).forEach(function (t) { order[t].sort(function (a, b) { return (a.order || 99) - (b.order || 99); }); });
      const unrouted = cat.models.filter(function (m) { return !m.tier; });
      const served = function (m) { return (m.served_by || []).some(function (s) { return !s.unavailable; }); };

      function renumber(t) { order[t].forEach(function (m, i) { m.order = i + 1; }); }
      function move(t, i, d) { const a = order[t]; const j = i + d; if (j < 0 || j >= a.length) return; const x = a[i]; a[i] = a[j]; a[j] = x; renumber(t); draw(); }
      function setTier(m, t) {
        const from = m.tier;
        if (t) { m.tier = t; delete m.unrouted; m.order = order[t].filter(function (x) { return x !== m; }).length + 1; }
        else { m.tier = null; delete m.order; m.unrouted = m.unrouted || "taken off its tier in the Team app"; }
        if (from && order[from]) { order[from] = order[from].filter(function (x) { return x !== m; }); renumber(from); }
        draw();
      }
      function tierSelect(m) {
        return h("select", { onchange: function (e) { setTier(m, e.target.value || null); } },
          h("option", { value: "" }, "not routed"), cat.tiers.map(function (t) { return h("option", { value: t, selected: m.tier === t ? true : null }, t); }));
      }

      fill(main, 
        h("div", { class: "gw" },
          h("div", { class: "gw-head" }, h("h1", null, "LiteLLM"), h("p", { class: "muted" }, "Picks the model inside each tier. Order is LiteLLM's priority: #1 first, the next only while the ones above are failing or cooling down. Tier fallbacks are where a request goes when its whole tier fails."),
            serviceCard("litellm")),
          h("h3", null, "Tiers"),
          h("div", { class: "tier-edit" }, cat.tiers.map(function (t) {
            return h("div", { class: "tier" }, h("div", { class: "tier-name" }, t, h("span", { class: "muted" }, " · " + order[t].length + " models")),
              order[t].map(function (m, i) {
                return h("div", { class: "tier-row tier-row--edit" + (i === 0 ? " tier-row--top" : "") + (served(m) ? "" : " tier-row--gone") },
                  h("span", { class: "n" }, "#" + (i + 1)),
                  h("span", { class: "tier-model", title: (m.strength || "") + (served(m) ? "" : " · no provider serves it today") }, m.id),
                  h("button", { class: "icon", title: "Up", disabled: i === 0, onclick: function () { move(t, i, -1); } }, "↑"),
                  h("button", { class: "icon", title: "Down", disabled: i === order[t].length - 1, onclick: function () { move(t, i, 1); } }, "↓"),
                  tierSelect(m));
              }),
              h("label", { class: "fallback" }, h("span", { class: "muted" }, "falls back to "),
                h("input", { type: "text", value: (cat.tier_fallbacks[t] || []).join(", "), onchange: function (e) {
                  cat.tier_fallbacks[t] = e.target.value.split(",").map(function (x) { return x.trim(); }).filter(Boolean); draw();
                } })));
          })),
          h("h3", null, "Free models not on a tier (" + unrouted.length + ")"),
          h("div", { class: "unrouted" }, unrouted.map(function (m) {
            return h("div", { class: "tier-row tier-row--edit" + (served(m) ? "" : " tier-row--gone") },
              h("span", { class: "tier-model", title: m.strength || "" }, m.id),
              h("span", { class: "muted" }, m.unrouted || m.unavailable || (served(m) ? "" : "no provider serves it today")),
              tierSelect(m));
          })),
          h("h3", null, "Routing policy"),
          policyFields(cat.policy.litellm, draw),
          rawCatalog(draw)),
        draftBar(draw));
    }
    draw().catch(fail);
  }

  // ---------- Portkey page ----------

  function PortkeyPage(main) {
    async function draw() {
      await loadGateway(false);
      const cat = gw.draft, link = {};
      (gw.overview.linkage.providers || []).forEach(function (p) { link[p.name] = p; });
      const providerNames = Object.keys(cat.providers);

      function keyRow(name, p) {
        const l = link[name] || {};
        const input = h("input", { type: "password", placeholder: l.key_set ? "replace key (" + l.key_mask + ")" : "paste key", autocomplete: "off" });
        return h("div", { class: "key-row" },
          h("span", { class: "muted" }, p.key_env + " "), l.key_set ? badge("set " + l.key_mask, "badge--ok") : badge("missing", "badge--bad"),
          input,
          h("button", { class: "btn btn--small", onclick: async function () {
            if (!input.value.trim()) return;
            if (!confirm("Write " + p.key_env + " to Portkey's .env and rebuild the gateway configs?")) return;
            try { const r = await api("gateway/key", { file: "portkey", name: p.key_env, value: input.value }); input.value = ""; toast("Saved " + r.name + " (" + r.mask + ")" + (r.refused ? "; rebuild refused: " + r.refused : "")); await loadGateway(true); draw(); }
            catch (e) { fail(e); }
          } }, "Save key"),
          h("button", { class: "btn btn--small btn--ghost", title: "One request through this provider alone", onclick: async function (e) {
            e.target.disabled = true;
            try { const r = await api("gateway/probe", { provider: name }); toast(name + ": " + (r.ok ? "answered" + (r.answered_by ? " (" + r.answered_by + ")" : "") : "failed" + (r.status ? " " + r.status : "")) + " in " + r.seconds + "s" + (r.error ? " · " + String(r.error).slice(0, 200) : ""), !r.ok); }
            catch (err) { fail(err); }
            e.target.disabled = false;
          } }, "Probe"));
      }

      function servedRows(m) {
        const rows = m.served_by || [];
        function mv(i, d) { const j = i + d; if (j < 0 || j >= rows.length) return; const x = rows[i]; rows[i] = rows[j]; rows[j] = x; draw(); }
        return rows.map(function (s, i) {
          return h("div", { class: "served-row" + (s.unavailable ? " tier-row--gone" : "") },
            h("span", { class: "n" }, i + 1),
            h("span", { class: "tier-model" }, s.provider, h("span", { class: "muted" }, " " + s.provider_model_id)),
            badge(s.free, s.free === "yes" ? "badge--ok" : "badge--outline", "why it is free"),
            h("button", { class: "icon", disabled: i === 0, onclick: function () { mv(i, -1); } }, "↑"),
            h("button", { class: "icon", disabled: i === rows.length - 1, onclick: function () { mv(i, 1); } }, "↓"),
            s.unavailable
              ? h("button", { class: "btn btn--small btn--ghost", title: s.unavailable, onclick: function () { delete s.unavailable; draw(); } }, "mark available")
              : h("button", { class: "btn btn--small btn--ghost", onclick: function () {
                const why = prompt("Why is " + s.provider + " unavailable for " + m.id + "?", "down (Team app)");
                if (why) { s.unavailable = why; draw(); }
              } }, "mark unavailable"));
        });
      }

      const routed = cat.models.filter(function (m) { return m.tier; }).sort(function (a, b) { return a.tier < b.tier ? -1 : a.tier > b.tier ? 1 : (a.order || 99) - (b.order || 99); });

      fill(main, 
        h("div", { class: "gw" },
          h("div", { class: "gw-head" }, h("h1", null, "Portkey"), h("p", { class: "muted" }, "Picks the provider for each model: tries them in order and moves on when one fails. Holds the only copy of the provider keys; keys are never shown, only a mask."),
            serviceCard("portkey")),
          h("h3", null, "Providers"),
          h("div", { class: "providers" }, providerNames.map(function (name) {
            const p = cat.providers[name], l = link[name] || {};
            return h("div", { class: "provider" },
              h("div", { class: "provider-head" }, h("strong", null, name), h("span", { class: "muted" }, " via " + p.portkey + (p.custom_host ? " · " + p.custom_host : "")),
                (cat.blocked_providers || {})[name] ? badge("blocked", "badge--bad") : null),
              h("div", { class: "muted" }, "free: " + (p.free_basis || "?")),
              p.limits ? h("div", { class: "muted" }, "limits: " + p.limits) : null,
              l.routed_models ? h("div", { class: "muted" }, "serves " + l.routed_models.length + " routed models, first choice for " + (l.first_choice_for || []).length) : null,
              keyRow(name, p),
              h("label", { class: "inline" }, h("span", { class: "muted" }, "timeout ms "), h("input", { type: "number", value: p.request_timeout_ms || "", placeholder: String(cat.policy.portkey.default_timeout_ms || ""), onchange: function (e) {
                if (e.target.value) p.request_timeout_ms = Number(e.target.value); else delete p.request_timeout_ms; draw();
              } })));
          })),
          h("h3", null, "Who serves each routed model, in failover order"),
          h("div", { class: "served" }, routed.map(function (m) {
            return h("div", { class: "served-model" }, h("div", null, h("strong", null, m.id), h("span", { class: "muted" }, " · " + m.tier + " #" + m.order)), servedRows(m));
          })),
          h("h3", null, "Failover policy"),
          policyFields(cat.policy.portkey, draw),
          rawCatalog(draw)),
        draftBar(draw));
    }
    draw().catch(fail);
  }

  route();
})();
