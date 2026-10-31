/**
 * Team tab: the pipeline, top to bottom, as boxes joined by arrows:
 * Boss -> (your check) -> Planner (the lead) -> Implementer -> Landed.
 * Each goal you planned with the boss sits in the box of the stage it has
 * reached, under its plan's one-line headline. A goal waiting at the boss
 * has a check that accepts its plan; any goal expands one level: the
 * planner's plan of it and what each implementer gets (model, tools, skills,
 * files, reads), not the full work order. Decisions for Eli come last.
 *
 * Plain IIFE, no build step, like the kanban plugin: React and components
 * come from window.__HERMES_PLUGIN_SDK__.
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
  
  function Part(props) {
    const p = props.part;
    return h("div", { className: "team-part" },
      h("div", null, h("strong", null, p.title), " ",
        h("span", { className: "team-muted" }, p.model + " · " + p.steps + " steps" + (p.state ? " · " + p.state : ""))),
      h("div", { className: "team-tags" },
        p.toolsets.map(function (t) { return h(Badge, { key: "t" + t }, t); }),
        p.skills.map(function (s) { return h(Badge, { key: "s" + s, variant: "outline" }, s); })),
      h("div", { className: "team-muted" }, "owns " + p.owned_paths.join(", ")),
      p.context.length ? h("div", { className: "team-muted" }, "reads " + p.context.join(", ")) : null);
  }

  function Decision(props) {
    const d = props.decision;
    const [open, setOpen] = useState(false);
    return h("div", { className: "team-plan" },
      h("div", { className: "team-plan-head" },
        h("button", { className: "team-expand", onClick: function () { setOpen(!open); } }, open ? "▾" : "▸"),
        h("span", { className: "team-headline" }, d.question),
        h(Badge, { variant: "outline" }, d.source)),
      open ? h("div", { className: "team-goal-body" },
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

  const NODES = [
    { key: "boss", title: "Boss", sub: "plans with you · Claude Sonnet" },
    { key: "lead", title: "Planner", sub: "work orders, tools, context · Claude" },
    { key: "workers", title: "Implementer", sub: "builds each part · free models" },
    { key: "landed", title: "Landed", sub: "check passed, merged" },
  ];

  function Chip(props) {
    const g = props.goal, c = props.plan;
    const [open, setOpen] = useState(false);
    const waiting = g.station === "boss";
    const state = g.parts.map(function (p) { return p.state; }).filter(Boolean).join(" · ");
    return h("div", { className: "team-chip" + (g.station === "stopped" ? " team-chip--stopped" : "") },
      h("div", { className: "team-chip-head" },
        h("button", { className: "team-expand", title: open ? "Collapse" : "Expand", onClick: function () { setOpen(!open); } },
          open ? "▾" : "▸"),
        h("span", { className: "team-chip-text" }, g.goal),
        g.after.length ? h("span", { className: "team-muted" }, "after " + g.after.join(", ")) : null),
      !open && state && !waiting ? h("div", { className: "team-muted team-chip-state" }, state) : null,
      open ? h("div", { className: "team-goal-body" },
        h("div", { className: "team-summary" }, g.plan),
        g.parts.map(function (p, i) { return h(Part, { key: i, part: p }); })) : null);
  }

  function GroupHead(props) {
    const c = props.plan;
    const [open, setOpen] = useState(false);
    return h("div", null,
      h("div", { className: "team-group-head" },
        props.waiting ? h("button", {
          className: "team-check",
          title: "Accept this plan: its " + c.goals.length + " goals go to the planner and the implementers",
          onClick: function () { props.act("plans/" + c.id + "/approve"); },
        }, "✓") : null,
        h("button", { className: "team-group-title", title: "Show the plan's summary", onClick: function () { setOpen(!open); } },
          c.headline),
        props.waiting ? h("button", {
          className: "team-dismiss", onClick: function () { props.act("plans/" + c.id + "/dismiss"); },
        }, "Dismiss") : null),
      open ? h("div", { className: "team-summary team-group-summary" }, c.summary) : null);
  }

  function Node(props) {
    const n = props.node, items = props.items;
    const plans = [];
    items.forEach(function (it) {
      let group = plans.find(function (x) { return x.plan.id === it.plan.id; });
      if (!group) { group = { plan: it.plan, goals: [] }; plans.push(group); }
      group.goals.push(it.goal);
    });
    return h("div", { className: "team-node team-node--" + n.key + (items.length ? " team-node--busy" : "") },
      h("div", { className: "team-node-head" },
        h("span", { className: "team-node-title" }, n.title),
        h("span", { className: "team-muted" }, n.sub),
        h(Badge, { variant: items.length ? "default" : "outline" }, String(items.length))),
      plans.map(function (grp) {
        return h("div", { key: grp.plan.id, className: "team-group" },
          h(GroupHead, { plan: grp.plan, waiting: n.key === "boss", act: props.act }),
          grp.goals.map(function (g) { return h(Chip, { key: g.n, goal: g, plan: grp.plan, act: props.act }); }));
      }));
  }

  function TeamPage() {
    const [flow, setFlow] = useState(null);
    const [inbox, setInbox] = useState({ decisions: [] });
    const [error, setError] = useState(null);
    const load = useCallback(function () {
      Promise.all([SDK.fetchJSON(API + "/flow"), SDK.fetchJSON(API + "/inbox")])
        .then(function (r) { setFlow(r[0]); setInbox(r[1]); setError(null); })
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
    if (!flow) return h("div", { className: "team-page" }, error ? h("div", { className: "team-error" }, error) : "Loading…");
    const at = {}, off = [];
    flow.plans.forEach(function (plan) {
      plan.goals.forEach(function (goal) {
        const item = { plan: plan, goal: goal };
        if (at[goal.station]) at[goal.station].push(item);
        else if (NODES.some(function (n) { return n.key === goal.station; })) at[goal.station] = [item];
        else if (goal.station === "stopped") off.push(item);
      });
    });
    return h("div", { className: "team-page" },
      error ? h("div", { className: "team-error" }, error) : null,
      h("div", { className: "team-pipeline" },
        NODES.map(function (n, i) {
          return h(React.Fragment, { key: n.key },
            i ? h("div", { className: "team-arrow" + ((at[n.key] || []).length ? " team-arrow--live" : "") },
              i === 1 ? h("span", { className: "team-arrow-label" }, "you ✓") : null) : null,
            h(Node, { node: n, items: at[n.key] || [], act: act }));
        })),
      off.length ? h(Node, { node: { key: "stopped", title: "Stopped", sub: "a part failed; see its decision" }, items: off, act: act }) : null,
      inbox.decisions.length ? h("h2", null, "Decisions for you (" + inbox.decisions.length + ")") : null,
      inbox.decisions.map(function (d) { return h(Decision, { key: d.id, decision: d, act: act }); }));
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("team", TeamPage);
  }
})();
