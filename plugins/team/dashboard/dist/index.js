/**
 * Team tab: the boss's plans, one line each. Open one for the deeper summary
 * and what every worker gets (model, toolsets, skills, files, what it reads);
 * approve or dismiss. Below, Team's other open decisions.
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

  function Row(props) {
    const [open, setOpen] = useState(false);
    return h("div", { className: "team-row" },
      h("button", { className: "team-head", onClick: function () { setOpen(!open); } },
        h("span", { className: "team-chev" }, open ? "▾" : "▸"),
        h("span", { className: "team-line" }, props.line),
        props.badge),
      open ? h("div", { className: "team-body" }, props.children) : null);
  }

  function Part(props) {
    const p = props.part;
    return h("div", { className: "team-part" },
      h("div", null, h("strong", null, p.title), " ",
        h("span", { className: "team-muted" }, p.role + " on " + p.model + " · " + p.steps + " steps")),
      h("div", { className: "team-tags" },
        p.toolsets.map(function (t) { return h(Badge, { key: "t" + t }, t); }),
        p.skills.map(function (s) { return h(Badge, { key: "s" + s, variant: "outline" }, s); })),
      h("div", { className: "team-muted" }, "owns " + p.owned_paths.join(", ")),
      p.context.length ? h("div", { className: "team-muted" }, "reads " + p.context.join(", ")) : null);
  }

  function Plan(props) {
    const c = props.plan;
    const waiting = c.status === "waiting";
    const parts = c.goals.reduce(function (n, g) { return n + g.parts.length; }, 0);
    return h(Row, {
      line: c.headline,
      badge: h(Badge, { variant: waiting ? "default" : "outline" },
        waiting ? "needs you" : c.status),
    },
      h("div", { className: "team-summary" }, c.summary),
      h("div", { className: "team-muted" },
        c.target + " · " + c.goals.length + " goals · " + parts + " worker parts"),
      c.goals.map(function (g) {
        return h("div", { key: g.n, className: "team-goal" },
          h("div", null, h("strong", null, g.n + ". "), g.goal,
            g.after.length ? h("span", { className: "team-muted" }, " (after " + g.after.join(", ") + ")") : null),
          h("div", { className: "team-muted" }, g.plan),
          g.parts.map(function (p, i) { return h(Part, { key: i, part: p }); }));
      }),
      waiting ? h("div", { className: "team-actions" },
        h(Button, { onClick: function () { props.act("plans/" + c.id + "/approve"); } }, "Approve"),
        h(Button, { variant: "outline", onClick: function () { props.act("plans/" + c.id + "/dismiss"); } }, "Dismiss"))
        : null);
  }

  function Decision(props) {
    const d = props.decision;
    return h(Row, { line: d.question, badge: h(Badge, { variant: "outline" }, d.source) },
      h("div", { className: "team-summary" }, d.context),
      d.recommendation ? h("div", { className: "team-muted" }, "recommended: " + d.recommendation) : null,
      h("div", { className: "team-actions" },
        d.options.map(function (o) {
          return h(Button, {
            key: o, variant: o === d.recommendation ? "default" : "outline",
            onClick: function () {
              props.act("decisions/" + d.id + "/answer", { answer: o });
            },
          }, o);
        }),
        h(Button, { variant: "ghost", onClick: function () { props.act("decisions/" + d.id + "/dismiss"); } }, "Dismiss")));
  }

  function TeamPage() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const load = useCallback(function () {
      SDK.fetchJSON(API + "/inbox").then(setData).catch(function (e) { setError(String(e)); });
    }, []);
    useEffect(function () {
      load();
      const id = setInterval(load, 15000);
      return function () { clearInterval(id); };
    }, [load]);
    const act = function (path, body) {
      SDK.fetchJSON(API + "/" + path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      }).then(load).catch(function (e) { setError(String(e)); });
    };
    if (!data) return h("div", { className: "team-inbox" }, error ? h("div", { className: "team-error" }, error) : "Loading…");
    const waiting = data.plans.filter(function (p) { return p.status === "waiting"; });
    const rest = data.plans.filter(function (p) { return p.status !== "waiting"; });
    return h("div", { className: "team-inbox" },
      error ? h("div", { className: "team-error" }, error) : null,
      h("h2", null, "Plans waiting for you (" + waiting.length + ")"),
      waiting.length ? waiting.map(function (p) { return h(Plan, { key: p.id, plan: p, act: act }); })
        : h("div", { className: "team-muted" }, "Nothing waiting. Talk to the boss in its pane."),
      h("h2", null, "Other decisions (" + data.decisions.length + ")"),
      data.decisions.map(function (d) { return h(Decision, { key: d.id, decision: d, act: act }); }),
      rest.length ? h("h2", null, "Earlier plans") : null,
      rest.map(function (p) { return h(Plan, { key: p.id, plan: p, act: act }); }));
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("team", TeamPage);
  }
})();
