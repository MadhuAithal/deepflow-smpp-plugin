# Two-hop lab with the Jasmin SMS gateway

The basic lab has the OTP sender talk straight to a fake operator. Real platforms put a
gateway in between, and this lab adds one: [Jasmin](https://github.com/jookies/jasmin), an
open-source SMPP gateway used in production.

```
send_otps.py  --SMPP :2775-->  Jasmin  --SMPP :2776-->  fake_smsc.py
 (platform)                   (gateway)                  (operator)
```

The plugin sees both hops, so you can tell whether a delay or failure came from the gateway
or from the operator. Each hop has its own message IDs: Jasmin gives the platform a UUID and
gets a different ID from the operator, so look at one hop at a time (dashboard "SMPP port"
box, or `SMPP_PORT=2776 scripts/smpp-delivery`).

It also tests the parser against SMPP written by someone else: Jasmin's receipts, optional
fields and read patterns differ from our own fake operator's.

## Run

Needs about 1 GB of free memory (RabbitMQ is the largest part). Stop any fake_smsc.py
already listening on 2775 first. Redis runs on 6380, not 6379, so it doesn't clash with a
Redis you may already have.

```sh
cd lab/jasmin
docker compose up -d                      # Redis, RabbitMQ, Jasmin (first start ~1 min)
SMSC_PORT=2776 python3 ../fake_smsc.py    # terminal 2: the operator, now on 2776
python3 setup_jasmin.py                   # once: user, operator connector, route
WAIT=150 ~/smpp-venv/bin/python ../send_otps.py 30   # terminal 3: OTPs through Jasmin
```

`WAIT=150` keeps the sender connected long enough for late receipts: Jasmin retries a
throttled message after 120 s. The agent also needs `processors.request_log.tunning.payload_truncation`
raised (it is, in `deploy/agent-group-config.yaml`): see FINDINGS.md #9.

`setup_jasmin.py` should end with the `operator` connector `started` and `BOUND_TRX`.
The agent must capture both ports: `deploy/agent-group-config.yaml` whitelists `2775,2776`
(apply it with `make config`, then restart the agent).

Stop with `docker compose down`. Jasmin's configuration is kept in a Docker volume;
`docker compose down -v` removes it too.

The jCli login (`jcliadmin` / `jclipwd`) and the `acme` / `secret` account are lab defaults.
Everything listens on this machine only, apart from Jasmin's ports 2775, 8990 and 1401.
Don't run this on a network you don't trust.

## What it showed (first run, 30 OTPs)

| | Hop 1: platform → Jasmin (2775) | Hop 2: Jasmin → operator (2776) |
|---|---|---|
| Submit latency | avg 14 ms | avg 230 ms, p95 394 ms |
| Refused | 0 | 5 × `ESME_RTHROTTLED` (Jasmin retried them) |
| Time to deliver | avg 78 s, max 129 s | avg 4 s |

Jasmin acknowledges the platform at once, so on the platform's own connection everything
looks instant and nothing is ever refused. The operator's latency and throttling only show on
hop 2, and the throttled OTPs reached the handset about two minutes late, because Jasmin waits
120 s before retrying. Capture matched ground truth on both hops (the sender's own log and
Jasmin's `messages.log`).
