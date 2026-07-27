# Qpulse → Qsight integration

Qpulse alerts appear on the Qsight dashboard. Qpulse stays an **on-demand**
process — a laptop run, a one-off container, whatever — and pushes alerts over
HTTPS to an authenticated endpoint on the already-running Qsight backend. No new
always-on infrastructure, and therefore no additional monthly cost.

```
Qpulse (wherever, whenever)                    Qsight (already running)
  detector → Bus → WebhookSink                   POST /qpulse/ingest
       └── HTTPS + X-Qpulse-Key ───────────────►  ├─ match active anomaly AlertRules by ticker
                                                  ├─ apply per-rule cooldown
                                                  └─ persist triggered AlertEvent
                                                        └─► dashboard Anomaly Feed (60 s poll)
```

## One-time setup

**1. Generate a shared secret**

```bash
openssl rand -hex 32
```

**2. Give it to the Qsight backend.** Store it in SSM alongside the other
secrets, then roll the task definition:

```bash
aws ssm put-parameter --name /qsight/prod/QPULSE_INGEST_KEY \
    --type SecureString --value "<the secret>" --region eu-north-1
```

`scripts/deploy-ecs-fargate.sh` already references that parameter, so a normal
deploy picks it up. The endpoint reads the value at import time, so the task
needs a **new revision** — `force-new-deployment` alone reuses the old one and
will not see it.

Until the parameter exists the endpoint returns **503** and ignores every
request, so deploying the code ahead of the key is safe.

**3. Create an alert rule.** In the Qsight UI: Create Alert → Alert Type →
*Anomaly (Qpulse)* → pick the ticker. Use Qsight's canonical symbols (`BTC-USD`,
not `BTC/USD` — the endpoint normalises the slash form for you).

**4. Run Qpulse pointed at it.**

```bash
cd services/hft_anomaly_service
set -a; source ../../configs/crypto.env; set +a     # profile files are not auto-loaded
HFT_WEBHOOK_URL=https://qsight-api.qmetrum.io/qpulse/ingest \
HFT_WEBHOOK_KEY=<the secret> \
HFT_WEBHOOK_MIN_SCORE=6.0 \
python -m app.live --source alpaca
```

Startup prints `[webhook] forwarding alerts → … (authenticated)`. If the key is
wrong you get `[webhook] auth rejected (401)` and the batch is dropped rather
than retried.

## Behaviour worth knowing

- **Opting in is required, in both directions.** Ingest writes only to rules with
  `extra_config.detector = "qpulse"`, and those rules are in turn skipped by
  Qsight's built-in z-score evaluator (before it fetches any price data). A rule
  without the flag stays owned by the built-in detector and receives nothing from
  Qpulse — otherwise one rule would have two writers sharing a cooldown, each
  silently suppressing the other.
- **Cooldown is per rule, not per alert kind.** Default 900 s (override with
  `extra_config.cooldown_seconds`). A `cusum` alert suppresses a `robust_z` on the
  same rule inside the window, *including within a single batch* — the first
  alert for a rule wins and the rest are reported in the response's `suppressed`
  array rather than vanishing. Note this is first-wins, not highest-severity-wins.
- **Only triggered events are stored.** No evaluation-noise rows.
- **The response is diagnostic**: `{received, matched_rules, events_persisted,
  suppressed, unmatched_symbols}` — `unmatched_symbols` is the usual reason an
  alert seems to go missing (no active anomaly rule for that ticker).

## What the UI does and does not claim

The feed shows the alert kind, score, severity, narrative, and **the name of the
reference catalog that kind was scored against** — never a recall or
false-positive number.

That restraint is deliberate. Those figures were measured on **historical daily
bars** against small labeled catalogs (see `README.md` and
`gate/frozen/V2_BASELINE.md`). A live tick stream is a different regime, and
detection performance there has not been measured. Kinds with no gate for their
asset class are labeled **experimental**.

The gated set lives in `QPULSE_REFERENCE_GATES` in the Qsight backend, keyed on
`(kind, symbol)` — the exact symbol, not an asset class. The crypto gate was
measured on BTC alone, so an ETH-USD or SOL-USD alert does **not** inherit it;
the macro entry keys on the universe string the batch detector emits as its
symbol, so changing `HFT_BATCH_UNIVERSE` correctly drops the claim.
`spread_widen` is deliberately absent: the frozen v1 artifact contains zero
`spread_widen` alerts, so nothing about it was gated.

Alerts from the `synthetic` or `csv` feeds are badged as such in the UI so they
cannot be mistaken for live-market alerts.

## Email and explanations

Ingested alerts also reach the user outside the dashboard:

- **Email** — sent to the rule owner for every alert that persists (so the
  per-rule cooldown throttles delivery). Off until SES is configured on the
  Qsight side; see `services/forecasting_service_py/docs/alert-email-setup.md`
  in the Qsight repo. The body always states the gating status and the feed.
- **Explanation** — Qsight's `alert_explainer` agent understands the Qpulse
  payload and combines it with the user's actual portfolio weights, so an alert
  becomes "NVDA moved 7σ, and it is 12% of this portfolio" rather than a bare
  score. It is told never to imply an accuracy or track record, and to say so
  when a detector is ungated or the feed is not live.

Measuring live-stream performance is roadmap phase P5.
