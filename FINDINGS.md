# Findings: writing a DeepFlow Wasm protocol plugin

These are notes from building this plugin against DeepFlow v6.6. Most of these behaviours
aren't in the plugin docs; we worked them out from the agent source
(`agent/src/flow_generator/protocol_logs/`, `agent/src/plugin/wasm/`) and from watching
what actually landed in ClickHouse. They should apply to any custom TCP protocol, not
just SMPP.

## 1. Plugins parse one socket read at a time, with no reassembly

`OnParsePayload` receives the bytes of a single `read()`/`write()` syscall as captured
by the eBPF socket tracer. Plugins get no TCP stream reassembly.

For length-prefixed binary protocols, that means:

- **Several PDUs in one read:** common on the sending side under load. Loop and emit
  one `L7ProtocolInfo` per PDU. `ParseActionAbortWithL7Info` accepts a slice.
- **One PDU split across reads:** common on the **receiving** side, because many client
  libraries read the 4-byte length first, then the body (Python `smpplib`, and the usual
  Java `DataInputStream` pattern). The plugin sees a 4-byte read followed by a
  headerless body, and neither can be parsed alone.
- **A PDU cut off at the end of a read:** the next read starts mid-PDU.

What this plugin does:

- Detection (`OnCheckPayload`) requires one complete, strictly valid PDU, so a fragment
  never claims a flow.
- If the last PDU in a read is cut off but its 16-byte header is complete, the plugin
  still emits it header-only (`smpp.truncated=true`). That keeps request/response
  pairing working and never guesses body fields.

The practical consequence is that you should capture on the side whose stack hands over
whole PDUs. For SMPP that is usually the operator or gateway side. Fixing this properly
needs per-flow reassembly state in the agent (or plugin-side state across calls), which
is worth raising upstream.

## 2. Direction, `IsReversed` and `msg_type`

The agent assigns `msg_type` (request vs response) from the **packet direction**
relative to the connection's client and server, not from what the plugin says the PDU
is. In SMPP both sides send requests: the operator sends `deliver_sm` (receipts) to the
platform, which answers with `deliver_sm_resp`.

- Setting `IsReversed=true` on a server-to-client request makes the agent call
  `base_info.reverse()`, which **swaps client and server ports**. Receipts therefore end
  up with `client_port=2775`, not `server_port`. Query with
  `(server_port=2775 OR client_port=2775)` or you'll think receipts are missing.
- Even with `IsReversed`, a receipt can still be stored as `type=1` (response) and its
  `deliver_sm_resp` as `type=0` (request), because `msg_type` follows direction. Don't
  rely on `type` for operator-initiated messages. Use your own attribute (here
  `smpp.command` and `smpp.receipt`).

## 3. `IsAsync=true` means "never pair"

It sounds like the right flag for a protocol with out-of-order responses (SMPP
windowing). It isn't. In the agent, `needs_session_aggregation()` returns false for
async records, so requests and responses are stored separately and you get no latency.

Leave `IsAsync` unset and set `RequestID` to the protocol's correlation ID (the SMPP
`sequence_number`). The agent's session aggregation matches on `RequestID`, so
out-of-order responses pair correctly.

## 4. Plugins of 1 MiB or more can't be delivered to the agent

The agent fetches plugins over gRPC from the server. Before it has received its own
configuration, its gRPC client uses a **1 MiB** maximum message size, so a 1 MiB or
larger `.wasm` fails with:

```
message length too large: found 1048628 bytes, the limit is: 1048576 bytes
```

Raising `global.communication.grpc_buffer_size` in the agent group config helps
afterwards, but the very first fetch can still hit the default.

Size depends heavily on toolchain:

| Toolchain | Flags | Size |
|---|---|---|
| Go 1.21.13 + TinyGo 0.30.0 | `-gc=custom -tags=custommalloc -scheduler=none` (DeepFlow's documented flags, `nottinygc` allocator) | **~830 KB**, works |
| Go 1.27 + TinyGo 0.43 | the SDK fails with "goroutine without scheduler" under `-scheduler=none`; dropping it to the default scheduler | ~1.4 MB, fails |

`scripts/build-plugin.sh` pins the working toolchain in `~/sdk` without touching the
system Go. The `Makefile` refuses to produce a plugin over ~1,040,000 bytes.

## 5. The eBPF tracer can lose the port whitelist after a restart

The port whitelist (`inputs.ebpf.socket.kprobe.whitelist.ports`) is applied when the
socket tracer starts. After a reboot we saw the tracer start before the group config
arrived, so port 2775 wasn't whitelisted and no SMPP records appeared, with no error.
A single `systemctl restart deepflow-agent` once the agent has registered fixed it.
`scripts/sanity.sh` reminds you.

## 6. Server and agent versions must match

The Compose `.env` default pulled server `v6.6.1`-tagged images that rejected the
`v6.6` agent and `deepflow-ctl` (404s from `deepflow-ctl`, "unknown service
agent.Synchronizer" in the agent log). Pin `DEEPFLOW_VERSION=v6.6` for everything.

## 7. A plain Linux host needs `workload_resource_sync_enabled`

For an agent on a host with no Kubernetes and no cloud domain (DeepFlow calls this a
"legacy host"), registration only completed after creating an `agent_sync` domain, an
agent group, and setting `inputs.resources.workload_resource_sync_enabled: true` in that
group's config, then restarting the agent. Without it the server logs "record not
found" and the agent never becomes `NORMAL`.

Related: the server identifies agents by control IP + MAC. If the host's IP changes
(Wi-Fi, hotspot) or a Docker bridge network overlaps the agent's address, the agent
re-registers as a new one and loses its group. `scripts/deepflow-fixed-ip.sh` avoids
this with a dummy interface on a fixed address.

## 8. Querying the results

- The querier SQL API (`:20416/v1/query/`, `db=flow_log`) has no `now()`. Compute
  `time >= <unix seconds>` in the shell.
- Plugin `Kv` fields land in `attribute_names` / `attribute_values` (arrays) and can be
  filtered as `attribute.<key>`.
- `type`: 0 = request only, 1 = response only, 2 = paired session (has
  `response_duration`, in microseconds).
- The same exchange is often recorded at several capture points (`tap_side` `c-p`,
  `s-p`, `local`). Deduplicate by a business key (here the operator message ID) rather
  than counting rows.
- The Compose server's clock and timestamps are UTC+8. Convert before comparing with
  local logs.

## 9. The default 1 KB payload limit drops traffic behind batching gateways

By default the eBPF tracer keeps only the first **1024 bytes of each socket read or write**
(`processors.request_log.tunning.payload_truncation`; the kernel side uses a 1 KB buffer unless
this is raised). An SMPP client that writes one PDU per call is unaffected. A gateway is not:
Jasmin (Twisted) writes many PDUs in a single call, and every PDU past the first kilobyte is
silently dropped, with no error anywhere.

Measured in the two-hop Jasmin lab (`lab/jasmin`), against ground truth from the sender's own
log and Jasmin's `messages.log`:

| Hop 1 (platform ↔ Jasmin) | 1024 bytes (default) | 16384 bytes |
|---|---|---|
| `submit_sm_resp` captured | 26 / 30 | 30 / 30 |
| receipts captured | 17 / 23 | 15 / 15 |

Hop 2 (Jasmin ↔ operator) was complete at 16384: 32 accepted + 5 throttled submits, 32 receipts.

Set it to the eBPF maximum for SMPP:

```yaml
processors:
  request_log:
    tunning:
      payload_truncation: 16384
```

Larger values cost memory per captured call, so this is a per-group setting rather than a
default to change everywhere. It is `hot_update` in the config schema, but restart the agent
and wait until the log shows the plugin loaded again before testing (about a minute): traffic
sent before that is not parsed.
