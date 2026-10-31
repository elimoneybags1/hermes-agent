/**
 * Team tab: the goals you planned with the boss, and where each one is.
 *
 * Every plan is a group: its one-sentence headline, a check to accept it,
 * and its goals. Each goal is a card with a track (boss → lead → workers →
 * landed) lit up to the station it has reached; expand a goal for the lead's
 * plan of it and what each worker gets (model, tools, skills, files, reads),
 * not the full work order. Below, Team's other open decisions.
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
  const LABELS = { boss: "Boss", lead: "Lead", workers: "Workers", landed: "Landed" };

  function Track(props) {
    const at = props.stations.indexOf(props.station);
    const off = props.station === "stopped" || props.station === "dismissed";
    return h("div", { className: "team-track" + (off ? " team-track--off" : "") },
      props.stations.map(function (s, i) {
        const cls = i < at ? "done" : i === at ? "here" : "todo";
        return h(React.Fragment, { key: s },
          i ? h("span", { className: "team-wire team-wire--" + (i <= at ? "done" : "todo") }) : null,
          h("span", { className: "team-stop team-stop--" + cls },
            h("span", { className: "team-dot" }), LABELS[s]));
      }),
      off ? h("span", { className: "team-stopped" }, props.station) : null);
  }

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

  function Goal(props) {
    const g = props.goal;
    const [open, setOpen] = useState(false);
    return h("div", { className: "team-goal" },
      h("div", { className: "team-goal-head" },
        h("button", {
          className: "team-expand", title: open ? "Collapse" : "Expand",
          onClick: function () { setOpen(!open); },
        }, open ? "▾" : "▸"),
        h("div", { className: "team-goal-text" },
          h("span", { className: "team-n" }, g.n), g.goal,
          g.after.length ? h("span", { className: "team-muted" }, "  after " + g.after.join(", ")) : null),
        h(Track, { stations: props.stations, station: g.station })),
      open ? h("div", { className: "team-goal-body" },
        h("div", { className: "team-summary" }, g.plan),
        g.parts.map(function (p, i) { return h(Part, { key: i, part: p }); })) : null);
  }

  function Plan(props) {
    const c = props.plan;
    const [open, setOpen] = useState(false);
    const waiting = c.status === "waiting";
    return h("div", { className: "team-plan" + (waiting ? " team-plan--waiting" : "") },
      h("div", { className: "team-plan-head" },
        waiting ? h("button", {
          className: "team-check", title: "Accept: send these goals to the lead and the workers",
          onClick: function () { props.act("plans/" + c.id + "/approve"); },
        }, "✓") : h("span", { className: "team-check team-check--done" }, c.status === "dismissed" ? "×" : "✓"),
        h("button", { className: "team-headline", onClick: function () { setOpen(!open); } }, c.headline),
        h(Badge, { variant: waiting ? "default" : "outline" }, waiting ? "needs you" : c.status),
        waiting ? h("button", {
          className: "team-dismiss", title: "Dismiss this plan",
          onClick: function () { props.act("plans/" + c.id + "/dismiss"); },
        }, "Dismiss") : null),
      open ? h("div", { className: "team-summary team-plan-summary" }, c.summary) : null,
      c.error ? h("div", { className: "team-error" }, c.error) : null,
      c.goals.map(function (g) { return h(Goal, { key: g.n, goal: g, stations: props.stations }); }));
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
    return h("div", { className: "team-page" },
      error ? h("div", { className: "team-error" }, error) : null,
      flow.plans.length ? flow.plans.map(function (p) {
        return h(Plan, { key: p.id, plan: p, stations: flow.stations, act: act });
      }) : h("div", { className: "team-muted" }, "No plans yet. Talk to the boss in its pane; its plans appear here."),
      inbox.decisions.length ? h("h2", null, "Other decisions (" + inbox.decisions.length + ")") : null,
      inbox.decisions.map(function (d) { return h(Decision, { key: d.id, decision: d, act: act }); }));
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("team", TeamPage);
  }
})();
