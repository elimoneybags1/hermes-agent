# Gateway dashboard

A local web page for configuring Team's LiteLLM and Portkey: http://127.0.0.1:8791/

It edits the same files you would by hand, with the same gates:

- **catalog.json** (providers, models and their failover order, tiers, fallbacks,
  `policy`, ports). Edits are a draft until saved. **Check** runs `build.py` on the
  draft and shows its refusal or the generated files that would change. **Save &
  apply** writes `catalog.json`, rebuilds every output, and restarts LiteLLM when
  its `config.yaml` changed. Portkey re-reads its configs by mtime, no restart.
  A save is refused if `catalog.json` changed on disk since the page loaded.
- **Keys**: provider keys in `$HOME/state/team/portkey/.env`, the master key in
  `$HOME/state/team/litellm/.env` (nothing else may go there). Keys are never
  sent back to the page, only a mask. Saving a key rebuilds the configs.
  **Probe** sends one request through that provider alone (`team-probe-<provider>`).
- **Linkage checks**: missing keys, keys no provider uses, keys in the wrong file,
  providers no routed model uses, the master key against each Hermes profile's copy.
- **Allowance**: a provider paid per token out of an included credit (Anthropic, on the
  Claude Max plan's $100/month) shows estimated spend this cycle against its credit and
  stop, from `integrations/portkey/spend.py`. LiteLLM's `spend_guard` stops routing to it at
  the stop; the dashboard refuses direct tests of it too.
- **Test**, **Traffic** (`requests.jsonl`, `usage.db`), **Logs** (keys masked).

Catalog edits are not committed; commit `integrations/portkey/catalog.json` yourself.

    install-launchd.sh   always-on agent com.fullmoney.team-dashboard (log: $HOME/state/team/dashboard/dashboard.log)
    run.sh               run it in the foreground

Loopback only. Every `/api` request needs the token the page was served with
(new per start: reload the page after a restart) and the right `Host`.
Standard library only, on the system `python3`, like `build.py`.
