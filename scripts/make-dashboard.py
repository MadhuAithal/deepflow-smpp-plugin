#!/usr/bin/env python3
"""Generates deploy/grafana-smpp-dashboard.json (SMPP delivery dashboard for DeepFlow's Grafana).

Queries go straight to ClickHouse through the "DeepFlow ClickHouse" datasource that
DeepFlow's Docker Compose deployment provisions. Every query de-duplicates by the
operator message ID, because one exchange is usually recorded at several observation
points (client process, server process, ...). See FINDINGS.md #8.
"""
import json
import sys

DS = {"type": "grafana-clickhouse-datasource", "uid": "DeepFlow ClickHouse"}
T = "flow_log.l7_flow_log"
PORT = "(server_port = ${smpp_port} OR client_port = ${smpp_port})"


def attr(name):
    return f"attribute_values[indexOf(attribute_names, '{name}')]"


# accepted submits: one row per operator message ID
ACCEPTED = f"""
  SELECT response_result AS id,
         min(start_time) AS submitted,
         maxIf(response_duration, type = 2) AS latency_us,
         any({attr('smpp.dest_prefix')}) AS dest
  FROM {T}
  WHERE {PORT} AND request_type = 'submit_sm' AND type IN (1, 2)
    AND response_code = 0 AND response_result != ''
    AND $__timeFilter(time)
  GROUP BY id"""

# delivery receipts: one row per message ID (receipts arrive after the submit, so no upper time bound)
RECEIPTS = f"""
  SELECT {attr('smpp.message_id')} AS id,
         min(start_time) AS received,
         argMin({attr('smpp.receipt_stat')}, start_time) AS stat,
         argMin({attr('smpp.receipt_err')}, start_time) AS err,
         any({attr('smpp.dest_prefix')}) AS rdest
  FROM {T}
  WHERE {PORT} AND request_type = 'deliver_sm'
    AND {attr('smpp.command')} = 'deliver_sm' AND {attr('smpp.receipt')} = 'true'
    AND time >= $__fromTime
  GROUP BY id"""

# refused submits (throttled, rejected): count per observation point, keep the best-covered one
REFUSED_ROWS = f"""
  FROM {T}
  WHERE {PORT} AND request_type = 'submit_sm' AND type IN (1, 2)
    AND response_code IS NOT NULL AND response_code != 0
    AND $__timeFilter(time)"""

SECS = "(toUnixTimestamp64Micro(r.received) - toUnixTimestamp64Micro(a.submitted)) / 1e6"

Q = {
    "accepted": f"SELECT count() AS accepted FROM ({ACCEPTED})",
    "delivered_pct": f"""
SELECT if(count() = 0, NULL, countIf(r.stat = 'DELIVRD') / count()) AS delivered
FROM ({ACCEPTED}) AS a
LEFT JOIN ({RECEIPTS}) AS r ON a.id = r.id""",
    "refused": f"""
SELECT ifNull(max(c), 0) AS refused FROM (SELECT observation_point, count() AS c {REFUSED_ROWS} GROUP BY observation_point)""",
    "pending": f"""
SELECT countIf(r.id IS NULL) AS no_receipt
FROM ({ACCEPTED}) AS a
LEFT JOIN ({RECEIPTS}) AS r ON a.id = r.id
SETTINGS join_use_nulls = 1""",
    "latency_p95": f"""
SELECT quantile(0.95)(latency_us) / 1000 AS p95_ms FROM ({ACCEPTED}) WHERE latency_us > 0""",
    "ttd_p95": f"""
SELECT quantile(0.95)({SECS}) AS p95_s
FROM ({ACCEPTED}) AS a
INNER JOIN ({RECEIPTS}) AS r ON a.id = r.id
WHERE r.stat = 'DELIVRD'""",
    "ts_accepted": f"""
SELECT $__timeInterval(submitted) AS t, count() AS accepted
FROM ({ACCEPTED})
GROUP BY t ORDER BY t""",
    "ts_refused": f"""
SELECT t, max(c) AS refused FROM (
  SELECT $__timeInterval(time) AS t, observation_point, count() AS c {REFUSED_ROWS}
  GROUP BY t, observation_point)
GROUP BY t ORDER BY t""",
    "ts_latency": f"""
SELECT $__timeInterval(submitted) AS t,
       avg(latency_us) / 1000 AS avg_ms,
       quantile(0.95)(latency_us) / 1000 AS p95_ms
FROM ({ACCEPTED})
WHERE latency_us > 0
GROUP BY t ORDER BY t""",
    "ts_ttd": f"""
SELECT $__timeInterval(a.submitted) AS t,
       avg({SECS}) AS avg_s,
       quantile(0.95)({SECS}) AS p95_s
FROM ({ACCEPTED}) AS a
INNER JOIN ({RECEIPTS}) AS r ON a.id = r.id
WHERE r.stat = 'DELIVRD'
GROUP BY t ORDER BY t""",
    "outcomes": f"""
SELECT if(r.id IS NULL, 'NO RECEIPT YET', r.stat) AS outcome, count() AS messages
FROM ({ACCEPTED}) AS a
LEFT JOIN ({RECEIPTS}) AS r ON a.id = r.id
GROUP BY outcome ORDER BY messages DESC
SETTINGS join_use_nulls = 1""",
    "refusal_reasons": f"""
SELECT reason, max(c) AS submits FROM (
  SELECT observation_point, if(response_exception = '', toString(response_code), response_exception) AS reason, count() AS c
  {REFUSED_ROWS}
  GROUP BY observation_point, reason)
GROUP BY reason ORDER BY submits DESC""",
    "messages": f"""
SELECT a.submitted AS submitted,
       a.id AS message_id,
       if(a.dest != '', a.dest, ifNull(r.rdest, '')) AS destination,
       if(a.latency_us > 0, a.latency_us / 1000, NULL) AS submit_latency_ms,
       if(r.id IS NULL, NULL, {SECS}) AS delivered_in_s,
       if(r.id IS NULL, 'no receipt yet', r.stat) AS outcome,
       if(r.err IN ('', '000') OR r.err IS NULL, '', r.err) AS error
FROM ({ACCEPTED}) AS a
LEFT JOIN ({RECEIPTS}) AS r ON a.id = r.id
ORDER BY submitted DESC
LIMIT 200
SETTINGS join_use_nulls = 1""",
}

TABLE, TS = 1, 0


def target(sql, fmt, ref="A"):
    return {"datasource": DS, "refId": ref, "queryType": "sql", "editorType": "sql",
            "format": fmt, "rawSql": sql.strip()}


_id = [0]


def panel(kind, title, grid, targets, desc="", **extra):
    _id[0] += 1
    p = {"id": _id[0], "type": kind, "title": title, "description": desc, "datasource": DS,
         "gridPos": dict(zip("xywh", grid)), "targets": targets}
    p.update(extra)
    return p


def stat(title, grid, sql, unit, desc, decimals=None, thresholds=None, color_mode="value"):
    defaults = {"unit": unit, "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute",
                               "steps": thresholds or [{"color": "text", "value": None}]}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return panel("stat", title, grid, [target(sql, TABLE)], desc,
                 fieldConfig={"defaults": defaults, "overrides": []},
                 options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                          "colorMode": color_mode, "graphMode": "none", "textMode": "value",
                          "justifyMode": "center", "orientation": "auto"})


def timeseries(title, grid, targets, unit, desc, bars=False):
    custom = {"drawStyle": "bars" if bars else "line", "lineWidth": 2, "fillOpacity": 60 if bars else 10,
              "pointSize": 5, "showPoints": "auto", "spanNulls": False,
              "stacking": {"mode": "normal" if bars else "none", "group": "A"}}
    return panel("timeseries", title, grid, targets, desc,
                 fieldConfig={"defaults": {"unit": unit, "min": 0, "custom": custom}, "overrides": []},
                 options={"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                          "tooltip": {"mode": "multi", "sort": "none"}})


def ref_override(ref, name, color):
    """Names and colours the series of one query (otherwise Grafana shows "A accepted")."""
    return {"matcher": {"id": "byFrameRefID", "options": ref},
            "properties": [{"id": "displayName", "value": name},
                           {"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}


def color_override(name, color):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}


panels = [
    stat("Accepted", (0, 0, 4, 4), Q["accepted"], "none",
         "Submits the operator accepted (it returned a message ID). Each message counted once."),
    stat("Delivered", (4, 0, 4, 4), Q["delivered_pct"], "percentunit",
         "Share of accepted messages with a DELIVRD receipt.", 1,
         [{"color": "red", "value": None}, {"color": "orange", "value": 0.8}, {"color": "green", "value": 0.95}]),
    stat("Refused", (8, 0, 4, 4), Q["refused"], "none",
         "Submits the operator refused (throttled, rejected, system error).", None,
         [{"color": "green", "value": None}, {"color": "orange", "value": 1}]),
    stat("No receipt yet", (12, 0, 4, 4), Q["pending"], "none",
         "Accepted messages with no delivery receipt so far."),
    stat("Latency p95", (16, 0, 4, 4), Q["latency_p95"], "ms",
         "submit_sm to submit_sm_resp, 95th percentile.", 0),
    stat("Deliver p95", (20, 0, 4, 4), Q["ttd_p95"], "s",
         "From the operator accepting a message to its DELIVRD receipt, 95th percentile.", 1),
    timeseries("Submits: accepted vs refused", (0, 4, 12, 8),
               [target(Q["ts_accepted"], TS, "A"), target(Q["ts_refused"], TS, "B")], "none",
               "Accepted and refused submits per interval.", bars=True)
    | {"fieldConfig": {"defaults": {"unit": "none", "min": 0, "decimals": 0,
                                    "custom": {"drawStyle": "bars", "fillOpacity": 70, "lineWidth": 1,
                                               "stacking": {"mode": "normal", "group": "A"}}},
                       "overrides": [ref_override("A", "accepted", "green"), ref_override("B", "refused", "red")]}},
    timeseries("Submit latency", (12, 4, 12, 8), [target(Q["ts_latency"], TS)], "ms",
               "How long the operator took to accept each message (submit_sm to submit_sm_resp)."),
    timeseries("Time to deliver", (0, 12, 12, 8), [target(Q["ts_ttd"], TS)], "s",
               "From the operator accepting a message to its DELIVRD receipt, by submit time."),
    panel("piechart", "Delivery outcomes", (12, 12, 6, 8), [target(Q["outcomes"], TABLE)],
          "Receipt status of accepted messages.",
          fieldConfig={"defaults": {"unit": "none"},
                       "overrides": [color_override("DELIVRD", "green"), color_override("UNDELIV", "red"),
                                     color_override("NO RECEIPT YET", "gray")]},
          options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True},
                   "pieType": "donut", "displayLabels": ["name", "value"],
                   "legend": {"displayMode": "list", "placement": "right", "showLegend": True,
                              "values": ["value", "percent"]}}),
    panel("bargauge", "Refusal reasons", (18, 12, 6, 8), [target(Q["refusal_reasons"], TABLE)],
          "Why the operator refused submits (SMPP command_status).",
          fieldConfig={"defaults": {"unit": "none", "color": {"mode": "fixed", "fixedColor": "red"}}, "overrides": []},
          options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True},
                   "orientation": "horizontal", "displayMode": "basic", "showUnfilled": True}),
    panel("table", "Messages", (0, 20, 24, 10), [target(Q["messages"], TABLE)],
          "Every accepted message: when, where to (masked), latency, delivery time and outcome.",
          fieldConfig={"defaults": {"custom": {"align": "auto"}},
                       "overrides": [
                           {"matcher": {"id": "byName", "options": "submit_latency_ms"},
                            "properties": [{"id": "unit", "value": "ms"}, {"id": "decimals", "value": 0},
                                           {"id": "displayName", "value": "submit latency"}]},
                           {"matcher": {"id": "byName", "options": "delivered_in_s"},
                            "properties": [{"id": "unit", "value": "s"}, {"id": "decimals", "value": 1},
                                           {"id": "displayName", "value": "delivered in"}]},
                           {"matcher": {"id": "byName", "options": "outcome"},
                            "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                           {"id": "mappings", "value": [{"type": "value", "options": {
                                               "DELIVRD": {"color": "green", "index": 0},
                                               "UNDELIV": {"color": "red", "index": 1},
                                               "no receipt yet": {"color": "gray", "index": 2}}}]}]}]},
          options={"showHeader": True, "cellHeight": "sm",
                   "sortBy": [{"displayName": "submitted", "desc": True}]}),
]

dashboard = {
    "uid": "smpp-delivery",
    "title": "SMPP delivery",
    "description": "SMS delivery observed by DeepFlow's eBPF agent with the SMPP plugin. "
                   "https://github.com/MadhuAithal/deepflow-smpp-plugin",
    "tags": ["smpp", "sms", "deepflow"],
    "timezone": "browser",
    "schemaVersion": 38,
    "version": 2,
    "editable": True,
    "refresh": "30s",
    "time": {"from": "now-1h", "to": "now"},
    "templating": {"list": [{
        "name": "smpp_port", "label": "SMPP port", "type": "textbox", "query": "2775",
        "current": {"text": "2775", "value": "2775"}, "hide": 0}]},
    "panels": panels,
}

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "deploy/grafana-smpp-dashboard.json"
    with open(out, "w") as f:
        json.dump(dashboard, f, indent=2)
    print(f"wrote {out}: {len(panels)} panels")
