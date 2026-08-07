# 指標（Metrics）

[English](metrics.md) | 繁體中文

Talyx 在 `/metrics`（預設埠 `9464`）暴露 Prometheus 指標。它**不記錄任何 tool 參數**、
不記錄訊息內容——只取 method/tool 名稱、結果、時間與循環結構。它唯一需要的關聯鍵
（密封的 `requestState` token）**在記憶體內雜湊，絕不寫入任何 label、log 或 trace**。

## 核心可靠性

| 指標 | 型別 | Labels | 意義 |
|---|---|---|---|
| `talyx_requests_total` | counter | `method`、`server`、`status` | 完成的請求數，依 method、server、`ok`/`error` 分。 |
| `talyx_request_duration_seconds` | histogram | `method`、`server` | 往返延遲，從請求到終端回應。 |
| `talyx_errors_total` | counter | `method`、`server`、`error_code` | 失敗請求數，依 JSON-RPC error code 分（tool 層 `isError` 記為 `app_error`）。 |

## Multi Round-Trip 循環（招牌指標）

一個 `2026-07-28` server 可以用 `InputRequiredResult`（`resultType: "input_required"`）
回應 `tools/call`，而不是回終端結果；client 接著帶著 server 的 `requestState` token 重送。
這個 elicitation 循環正是差異化所在——通用 APM 看到的是兩個不相干的請求，Talyx 看到
的是一個循環。

| 指標 | 型別 | Labels | 意義 |
|---|---|---|---|
| `talyx_input_required_total` | counter | `method`、`server`、`wait_type` | 進入的循環腿數（每個 `InputRequiredResult`）。 |
| `talyx_round_trips_per_request` | histogram | `wait_type` | 一個邏輯請求走了幾次往返。 |
| `talyx_round_trip_cycle_duration_seconds` | histogram | `wait_type` | 整段循環延遲，從首次請求到終端。 |
| `talyx_input_wait_duration_seconds` | histogram | `server`、`wait_type` | 每個循環花在等後續請求的時間。 |
| `talyx_abandoned_cycles_total` | counter | `server`、`wait_type` | 給了 `InputRequired` 卻在時限內沒有後續的循環。 |
| `talyx_request_state_rejected_total` | counter | `server` | 拒絕了某個 `requestState` 的回應。 |

`wait_type` 是 `input`（該腿帶 `inputRequests`——在等人/client）或 `poll`（只有 state
——server 要求 client 稍後重試）。

## 基礎 liveness 與自身指標

| 指標 | 型別 | Labels | 意義 |
|---|---|---|---|
| `talyx_server_up` | gauge | — | 被包住的 server 子行程活著時為 `1`，否則 `0`。 |
| `talyx_inflight_requests_lost_total` | counter | `server` | 子行程掛掉時，還在進行中而遺失的請求數。 |
| `talyx_proxy_overhead_seconds` | histogram | — | talyx 觀測每個轉發 chunk 所花的實際時間。 |

## Label 基數（cardinality）

`method`、`server`、`wait_type` 都是低基數且有界的。Talyx 從不把 session id、
conversation id、tool 參數或原始 `requestState` 當 label——那些是無界的，會把
Prometheus 撐爆。（自 `2026-07-28` 起，協定已無 session 概念。）

## Status 與 error 語意

一個請求會被算成 `status="error"`，只要回應帶了 JSON-RPC `error`，或它的 `result` 有
`isError: true`（tool 層失敗，但以成功的 JSON-RPC 回應形式回來）。`error_code` 是
JSON-RPC 的 `error.code`；tool 層 `isError` 沒有 code，會落在 `app_error`。

中間的 `input_required` 腿**不**計入 `talyx_requests_total`——只有終端腿計入，且用該腿
的時長——所以核心延遲不會被人類等待時間污染。

`talyx_request_state_rejected_total` 的判定是看 `error.data.reason ==
"invalid_request_state"`，**不是**看 code（code 是通用的 `-32602`）。沒有 `reason`
label：具體原因（過期／竄改／audience）只進 server log。

## 查詢範例

Error rate（5 分鐘內失敗請求的比例）：

```promql
sum(rate(talyx_errors_total[5m])) / sum(rate(talyx_requests_total[5m]))
```

依 method 的 p95 請求延遲：

```promql
histogram_quantile(0.95, sum(rate(talyx_request_duration_seconds_bucket[5m])) by (le, method))
```

Abandoned 循環率（招牌的「agent 靜默卡死」訊號）：

```promql
sum(rate(talyx_abandoned_cycles_total[5m]))
```

失控循環——超過 `le=10` 桶的都是撞破 client 預設上限的：

```promql
sum(rate(talyx_round_trips_per_request_bucket{le="+Inf"}[5m]))
  - sum(rate(talyx_round_trips_per_request_bucket{le="10.0"}[5m]))
```

Server 端循環時間——**扣掉人類等待**，這樣慢的使用者不會 call 醒你；對此指標告警，而非
原始的 `cycle_duration`：

```promql
(
  sum(rate(talyx_round_trip_cycle_duration_seconds_sum[5m]))
    - sum(rate(talyx_input_wait_duration_seconds_sum[5m]))
) / sum(rate(talyx_round_trip_cycle_duration_seconds_count[5m]))
```

Proxy 額外開銷 p95：

```promql
histogram_quantile(0.95, sum(rate(talyx_proxy_overhead_seconds_bucket[5m])) by (le))
```
