/**
 * Team tab, drawn like an org chart: the Boss on top, a bar under it, and one
 * column per role (Planner, Implementer, Rescue). Each column holds the goals
 * at that role now; a goal shows the files it owns, its check, and for every
 * attempt the tier asked for -> the model that answered and that model's rank
 * in its tier (#1 is the tier's first choice). Plans the planner drafted wait
 * in the Planner column for your check. Below: what landed or stopped, every
 * token by zone, and each tier's ranked models as LiteLLM served them.
 *
 * Plain IIFE, no build step, like the kanban plugin: React and components
 * come from window.__HERMES_PLUGIN_SDK__. Data: GET /api/plugins/team/board.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;
  const { React } = SDK;
  const h = React.createElement;
  const { Badge, Button } = SDK.components;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const API = "/api/plugins/team";

  function fmt(n) {
    if (n === null || n === undefined) return "—";
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e4) return Math.round(n / 1e3) + "k";
    return String(n);
  }

  function Num(props) {
    const n = props.n;
    return h("span", { className: "team-num", title: n === null || n === undefined ? "not recorded" : n.toLocaleString() }, fmt(n));
  }

  function Tokens(props) {
    const t = props.t || {};
    return h("div", { className: "team-tokens" },
      h("span", null, "in ", h(Num, { n: t.tokens_in })),
      h("span", null, "out ", h(Num, { n: t.tokens_out })),
      h("span", { title: "cache read / cache write" }, "cache ",
        t.cache_read === null && t.cache_write === null
          ? h("span", { className: "team-muted" }, "not recorded")
          : h(React.Fragment, null, h(Num, { n: t.cache_read }), " / ", h(Num, { n: t.cache_write }))),
      props.calls === false ? null : h("span", { className: "team-muted" }, (t.calls || 0) + " calls"));
  }

  // "#1 of 8": green when the tier's first choice answered, amber otherwise.
  function Rank(props) {
    const m = props.m;
    if (!m.rank) return null;
    return h("span", {
      className: "team-rank" + (m.rank === 1 ? " team-rank--top" : " team-rank--down"),
      title: m.rank === 1 ? "the tier's first choice" : "not the tier's first choice",
    }, "#" + m.rank + (m.of ? " of " + m.of : ""));
  }

  function Route(props) {
    const a = props.a, r = a.route || {};
    const models = r.models || [];
    return h("div", { className: "team-route" },
      h("span", { className: "team-muted" }, a.asked || "?"),
      h("span", { className: "team-muted" }, " → "),
      models.length
        ? models.map(function (m, i) {
          return h("span", { key: i, className: "team-model" }, m.model, " ", h(Rank, { m: m }));
        })
        : h("span", { className: "team-muted" }, a.outcome ? "no answer recorded" : "waiting"),
      r.top_tried === false && models.some(function (m) { return m.rank > 1; })
        ? h("span", { className: "team-warn", title: "LiteLLM never tried the tier's #1 during this attempt" }, " #1 not tried") : null);
  }

  function Outcome(props) {
    const o = props.o;
    if (!o) return h(Badge, { variant: "outline" }, "running");
    return h("span", { className: "team-outcome team-outcome--" + o }, o === "pass" ? "✓ pass" : "✗ " + o);
  }

  function Attempt(props) {
    const a = props.a, r = a.route || {};
    return h("div", { className: "team-attempt" },
      h("div", { className: "team-attempt-head" },
        h("span", { className: "team-n" }, a.zone === "lead" ? "plan" : "#" + a.attempt),
        h(Route, { a: a }),
        h(Outcome, { o: a.outcome })),
      (r.tried || []).length > 1 ? h("div", { className: "team-muted" }, "tried: ",
        r.tried.map(function (t) { return (t.ok ? "✓ " : "✗ ") + t.model + (t.rank ? " #" + t.rank : ""); }).join("  →  ")) : null,
      (r.passed_over || []).length ? h("div", { className: "team-muted" }, "passed over: ",
        r.passed_over.slice(0, 3).map(function (p) { return "#" + p.rank + " " + p.model + " (" + p.why + ")"; }).join("; ")) : null,
      r.by === "time" ? h("div", { className: "team-muted", title: "no session id matched LiteLLM's log; matched by tier and time" }, "model matched by time") : null,
      a.detail ? h("div", { className: "team-detail" }, a.detail) : null,
      h(Tokens, { t: a.tokens }));
  }

  function GoalCard(props) {
    const g = props.g;
    const [open, setOpen] = useState(false);
    const last = g.attempts[g.attempts.length - 1];
    return h("div", { className: "team-card team-card--" + g.station },
      h("div", { className: "team-card-head" },
        h("button", { className: "team-expand", onClick: function () { setOpen(!open); } }, open ? "▾" : "▸"),
        h("span", { className: "team-card-text" + (open ? "" : " team-clamp"), title: open ? null : g.goal }, h("span", { className: "team-n" }, g.id), g.goal),
        g.outcome ? h(Outcome, { o: g.outcome }) : null),
      h("div", { className: "team-card-meta" },
        g.owns.length ? h("div", null, h("span", { className: "team-muted" }, "owns "), h("code", null, g.owns.join(", "))) : null,
        last ? h(Route, { a: last }) : h("div", { className: "team-muted" }, "waiting for its dependencies")),
      open ? h("div", { className: "team-card-body" },
        h("div", null, h("span", { className: "team-muted" }, "check "), h("code", null, g.check)),
        h("div", null, h("span", { className: "team-muted" }, "branch "), h("code", null, g.branch)),
        g.order_bytes ? h("div", { className: "team-muted" }, "work order v" + g.order_version + ", " + fmt(g.order_bytes) + " bytes") : null,
        g.planned ? h(Attempt, { a: g.planned }) : null,
        g.attempts.map(function (a) { return h(Attempt, { key: a.job, a: a }); }),
        g.detail ? h("div", { className: "team-detail" }, g.detail) : null) : null);
  }

  function Part(props) {
    const p = props.part;
    return h("div", { className: "team-part" },
      h("div", null, h("strong", null, p.title), " ",
        h("span", { className: "team-muted" }, p.model + " · " + p.steps + " steps")),
      h("div", { className: "team-tags" },
        p.toolsets.map(function (t) { return h(Badge, { key: "t" + t }, t); }),
        p.skills.map(function (s) { return h(Badge, { key: "s" + s, variant: "outline" }, s); })),
      h("div", { className: "team-muted" }, "owns " + p.owned_paths.join(", ")),
      p.context.length ? h("div", { className: "team-muted" }, "reads " + p.context.join(", ")) : null);
  }

  // A plan the planner drafted from the boss's goals, waiting for your check.
  function Waiting(props) {
    const c = props.plan;
    const [open, setOpen] = useState(false);
    return h("div", { className: "team-card team-card--waiting" },
      h("div", { className: "team-card-head" },
        h("button", {
          className: "team-check", title: "Accept: its " + c.goals.length + " goals go to the implementers",
          onClick: function () { props.act("plans/" + c.id + "/approve"); },
        }, "✓"),
        h("button", { className: "team-card-text team-left", onClick: function () { setOpen(!open); } }, c.headline),
        h("button", { className: "team-dismiss", onClick: function () { props.act("plans/" + c.id + "/dismiss"); } }, "Dismiss")),
      open ? h("div", { className: "team-card-body" },
        h("div", { className: "team-summary" }, c.summary),
        c.goals.map(function (g) {
          return h("div", { key: g.n, className: "team-goal" },
            h("div", null, h("span", { className: "team-n" }, g.n), g.goal),
            h("div", { className: "team-muted" }, g.plan),
            g.parts.map(function (p, i) { return h(Part, { key: i, part: p }); }));
        })) : null);
  }

  function Column(props) {
    const items = props.items, extra = props.extra || [];
    const n = items.length + extra.length;
    return h("div", { className: "team-col" + (n ? " team-col--busy" : "") },
      h("div", { className: "team-drop" }),
      h("div", { className: "team-col-head" },
        h("div", { className: "team-col-title" }, props.title, " ", h(Badge, { variant: n ? "default" : "outline" }, String(n))),
        h("div", { className: "team-muted" }, props.sub),
        h(Tokens, { t: props.tokens })),
      h("div", { className: "team-col-body" },
        extra,
        items.map(function (g) { return h(GoalCard, { key: g.id, g: g }); }),
        n ? null : h("div", { className: "team-muted team-empty" }, "nothing here now")));
  }

  function Tiers(props) {
    return h("div", { className: "team-tiers" },
      props.tiers.map(function (t) {
        return h("div", { key: t.tier, className: "team-tier" },
          h("div", { className: "team-tier-name" }, t.tier),
          t.models.map(function (m) {
            const failing = m.last_fail && m.last && m.last_fail === m.last;
            return h("div", { key: m.model, className: "team-tier-row" + (m.rank === 1 ? " team-tier-row--top" : "") },
              h("span", { className: "team-n" }, "#" + m.rank),
              h("span", { className: "team-tier-model" }, m.model),
              h("span", { className: "team-muted", title: "successful calls / calls in LiteLLM's log" },
                m.calls ? (m.ok || 0) + "/" + m.calls : "unused"),
              m.rl ? h("span", { className: "team-warn" }, m.rl + " rate-limited") : null,
              failing ? h("span", { className: "team-warn", title: m.last_fail }, "last call failed") : null);
          }));
      }));
  }

  const ZONE_NAMES = { boss: "Boss", lead: "Planner", implement: "Implementer", rescue: "Rescue", other: "Not attributed" };

  function Decision(props) {
    const d = props.decision;
    const [open, setOpen] = useState(false);
    return h("div", { className: "team-card" },
      h("div", { className: "team-card-head" },
        h("button", { className: "team-expand", onClick: function () { setOpen(!open); } }, open ? "▾" : "▸"),
        h("span", { className: "team-card-text" }, d.question),
        h(Badge, { variant: "outline" }, d.source)),
      open ? h("div", { className: "team-card-body" },
        h("div", { className: "team-summary" }, d.context),
        h("div", { className: "team-actions" },
          d.options.map(function (o) {
            return h(Button, {
              key: o, size: "sm", variant: o === d.recommendation ? "default" : "outline",
              onClick: function () { props.act("decisions/" + d.id + "/answer", { answer: o }); },
            }, o);
          }),
          h(Button, { size: "sm", variant: "ghost", onClick: function () { props.act("decisions/" + d.id + "/dismiss"); } }, "Dismiss"))) : null);
  }

  function TeamPage() {
    const [b, setB] = useState(null);
    const [inbox, setInbox] = useState({ decisions: [] });
    const [error, setError] = useState(null);
    const load = useCallback(function () {
      Promise.all([SDK.fetchJSON(API + "/board"), SDK.fetchJSON(API + "/inbox")])
        .then(function (r) { setB(r[0]); setInbox(r[1]); setError(null); })
        .catch(function (e) { setError(String(e)); });
    }, []);
    useEffect(function () {
      load();
      const id = setInterval(load, 10000);
      return function () { clearInterval(id); };
    }, [load]);
    const act = function (path, body) {
      SDK.fetchJSON(API + "/" + path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      }).then(load).catch(function (e) { setError(String(e)); });
    };
    if (!b) return h("div", { className: "team-page" }, error ? h("div", { className: "team-error" }, error) : "Loading…");
    const cfg = b.configured, z = b.tokens.zones;
    const waiting = b.waiting.map(function (p) { return h(Waiting, { key: "p" + p.id, plan: p, act: act }); });
    return h("div", { className: "team-page" },
      error ? h("div", { className: "team-error" }, error) : null,
      h("div", { className: "team-chart" },
        h("div", { className: "team-boss" },
          h("div", { className: "team-col-title" }, "Boss"),
          h("div", { className: "team-muted" }, "plans with you · " + (cfg.boss.model || "?")),
          h(Tokens, { t: z.boss }),
          b.waiting.length ? h("div", { className: "team-boss-note" }, b.waiting.length + " plan" + (b.waiting.length > 1 ? "s" : "") + " waiting for your ✓ under Planner") : null),
        h("div", { className: "team-stem" }),
        h("div", { className: "team-cols" },
          h(Column, { title: "Planner", sub: "work orders, tools, context · " + (cfg.lead.model || "?"), tokens: z.lead, items: b.columns.lead, extra: waiting }),
          h(Column, { title: "Implementer", sub: "builds each part · " + (cfg.implement.tiers || []).join(" → "), tokens: z.implement, items: b.columns.implement }),
          h(Column, { title: "Rescue", sub: "after " + (cfg.rescue.after_failures || "?") + " failed attempts · " + (cfg.rescue.model || "?"), tokens: z.rescue, items: b.columns.rescue }))),
      b.done.length ? h("h2", null, "Landed and stopped") : null,
      h("div", { className: "team-done" }, b.done.map(function (g) { return h(GoalCard, { key: g.id, g: g }); })),
      h("h2", null, "Tokens" + (b.tokens.since ? " since " + b.tokens.since.slice(0, 10) : "")),
      h("div", { className: "team-token-table" },
        h("div", { className: "team-token-row team-token-row--total" }, h("span", null, "All"), h(Tokens, { t: b.tokens.total })),
        Object.keys(ZONE_NAMES).map(function (k) {
          return h("div", { key: k, className: "team-token-row" }, h("span", null, ZONE_NAMES[k]), h(Tokens, { t: z[k] }));
        })),
      h("h2", null, "Tiers: LiteLLM's order, #1 first"),
      h(Tiers, { tiers: b.tiers }),
      inbox.decisions.length ? h("h2", null, "Decisions for you (" + inbox.decisions.length + ")") : null,
      inbox.decisions.map(function (d) { return h(Decision, { key: d.id, decision: d, act: act }); }));
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("team", TeamPage);
  }
})();
