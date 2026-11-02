# deepflow-smpp-plugin

An SMPP 3.4 protocol parser for [DeepFlow](https://github.com/deepflowio/deepflow)'s eBPF
agent, built as a Wasm plugin.

SMPP is the protocol that carries SMS between messaging platforms and telecom operators:
every OTP, bank alert and delivery notification travels over it. Most observability
tools don't understand it, so SMS pipelines show up as opaque TCP connections on
port 2775.

With this plugin, DeepFlow turns that traffic into request logs **without any change to
the SMS application**. The eBPF agent already sees the bytes on the socket; the plugin
decodes them:

- **Submit latency:** `submit_sm` → `submit_sm_resp`, paired by sequence number (works
  with SMPP windowing, where responses come back out of order)
- **Operator refusals:** `ESME_RTHROTTLED`, queue full, system errors, classified as
  operator-side or sender-side
- **Delivery receipts:** `deliver_sm` receipts with `DELIVRD` / `UNDELIV` and error code,
  linked to the original message by the operator's message ID
- **Message metadata:** sender ID, masked destination, data coding, concatenated-message
  parts, bind account

The plugin never reports message text (only its length), bind passwords, or full phone
numbers. Destinations are masked to the first 6 digits (`919800xxxxxx`).

## What you get

A report built from nothing but DeepFlow's records (`scripts/smpp-delivery`). This is
from the lab below, with 20 OTPs accepted:

| | |
|---|---|
| Delivered (`DELIVRD`) | 17 (85% of accepted) |
| Not delivered (`UNDELIV`) | 3 |
| Submit latency | avg 276 ms, p95 389 ms |
| Time to deliver | avg 3.4 s, p95 4.8 s, max 6.0 s |

These matched the fake SMSC's own log exactly.

## Layout

| Path | What it is |
|---|---|
| `smpp/` | The parser. Plain Go with no DeepFlow dependency. All SMPP knowledge lives here, with tests built from real captured bytes and a fuzz test. |
| `plugin/` | The thin DeepFlow wrapper: detection hook, parse hook, and the mapping to DeepFlow fields. |
| `cmd/smpp-decode/` | Paste a hex dump, see what the parser extracts. |
| `deploy/agent-group-config.yaml` | Agent settings that switch the plugin on and capture port 2775. |
| `lab/` | A fake SMSC (operator side) and an OTP sender, to generate realistic traffic locally. |
| `scripts/` | Build, setup, sanity-check and reporting helpers. |
| `FINDINGS.md` | DeepFlow plugin behaviours learned the hard way. Read this before writing your own plugin. |

## Quick start

### 1. Parser only (any machine with Go)

```sh
git clone https://github.com/MadhuAithal/deepflow-smpp-plugin && cd deepflow-smpp-plugin
make test     # unit tests on golden PDUs
make fuzz     # 30 s of random bytes; must never crash
echo 00000010000000150000000000000017 | go run ./cmd/smpp-decode
```

### 2. Full lab (Linux with BTF and Docker)

Tested on Ubuntu with DeepFlow v6.6 (Docker Compose server, agent installed as a
systemd service on the same host).

1. Install DeepFlow's Docker Compose server and the host agent, following DeepFlow's
   docs. Set `DEEPFLOW_VERSION=v6.6` for **both** server and agent (FINDINGS.md #6).
2. Build the plugin with the pinned toolchain (FINDINGS.md #4 explains why):
   ```sh
   bash scripts/build-plugin.sh
   ```
3. Optional but recommended on a laptop: put the server on a fixed local address
   (`10.233.233.1` on a dummy interface) so Wi-Fi changes don't break the agent. This
   also creates the agent group, applies the config and uploads the plugin:
   ```sh
   bash scripts/deepflow-fixed-ip.sh
   ```
   Or do it by hand on an existing setup:
   ```sh
   make upload NODE_IP=<server-ip>
   make config NODE_IP=<server-ip> GROUP_ID=<g-xxxxxxxxxx>
   sudo systemctl restart deepflow-agent
   ```
4. Generate traffic:
   ```sh
   python3 -m venv ~/smpp-venv && ~/smpp-venv/bin/pip install -r lab/requirements.txt
   python3 lab/fake_smsc.py                     # terminal 1: operator on 127.0.0.1:2775
   ~/smpp-venv/bin/python lab/send_otps.py 20   # terminal 2: 20 OTPs
   ```
5. Wait about a minute for the records to flush, then:
   ```sh
   scripts/smpp-counts 15        # records by command / type / capture side
   scripts/smpp-delivery 15      # the delivery report above
   scripts/smpp-delivery 15 --all
   ```

`scripts/sanity.sh` checks the whole chain after a reboot or OS upgrade.

### Grafana dashboard

`deploy/grafana-smpp-dashboard.json` is a ready-made "SMPP delivery" dashboard for
DeepFlow's Grafana: delivery rate, accepted vs refused submits, submit latency, time to
deliver, delivery outcomes, refusal reasons and a per-message table. It queries ClickHouse
directly through the "DeepFlow ClickHouse" datasource that DeepFlow's Compose deployment
provisions, and counts each message once even when several capture points recorded it.

```sh
bash scripts/import-dashboard.sh     # then open http://<deepflow-ip>:3000/d/smpp-delivery
```

To change panels, edit `scripts/make-dashboard.py` and regenerate with
`python3 scripts/make-dashboard.py`.

## Field mapping

| DeepFlow field | From the PDU |
|---|---|
| `request_type` | `submit_sm`, `deliver_sm`, `bind_transceiver` ... |
| `endpoint` | sender ID (e.g. `ACMEOTP`) |
| `request_resource` | masked destination, the bind account, or `receipt:DELIVRD` for receipts |
| `response_code` / `response_exception` | `command_status` and its name (`ESME_RTHROTTLED`) |
| `response_result` | operator message ID |
| `response_status` | OK, client error (sender's fault) or server error (operator side) |
| `response_duration` | request → response latency, paired by `sequence_number` |
| `attribute.smpp.*` | `message_id`, `receipt`, `receipt_stat`, `receipt_err`, `data_coding`, `sm_length`, `concat_*`, `truncated` ... |

## Known limitations

- **Split reads:** DeepFlow plugins see one socket read at a time and get no TCP
  reassembly. Clients that read the 4-byte length and then the body in separate calls
  (smpplib, many Java clients) can't be parsed on their own side. Capture on the
  operator or gateway side instead, where PDUs usually arrive in one read. Details in
  FINDINGS.md #1.
- **Cut-off PDUs:** when a read ends partway through a PDU, the plugin still reports it
  header-only (`smpp.truncated=true`), so it can be paired, but its body fields are
  missing. In the lab about 1 in 10 requests on the operator side looked like this.
- **Receipts aren't paired with their `deliver_sm_resp`:** a receipt and its
  acknowledgement end up as separate records, labelled by packet direction
  (FINDINGS.md #2). Receipts are linked to their message by message ID at query time.

## Status

This is a working prototype, validated end to end against a local fake SMSC, not yet
against production operator traffic. Issues and real-world captures are welcome. Please
redact numbers and message text first.

## License

Copyright 2026 Madhusudhan Aithal K. Licensed under Apache 2.0, see [LICENSE](LICENSE).
