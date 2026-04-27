# /api/stocks/{code}/factors/technical

`GET` 返回单只股票的技术指标序列。

## 路径参数

- `code`（类型：`str`）：股票代码。

## 查询参数

- `trade_date`（类型：`str`）：交易日期，格式 `YYYY-MM-DD`。
- `start_date`（类型：`str`）：起始日期，格式 `YYYY-MM-DD`。
- `end_date`（类型：`str`）：结束日期，格式 `YYYY-MM-DD`。
- `adjust`（类型：`str`；默认：`none`）：复权方式。
- `fields`（类型：`str`）：按逗号指定返回字段，不传返回全部字段。

## 返回类型

顶层返回 `list[TechnicalFactorItem]`。

## 返回字段

- `code`（`str`）：股票代码。
- `trade_date`（`str`）：交易日期。
- `adjust`（`str`）：复权方式。
- `ma5`（`float | None`）：5 日均线。
- `ma10`（`float | None`）：10 日均线。
- `ma20`（`float | None`）：20 日均线。
- `ma60`（`float | None`）：60 日均线。
- `ema12`（`float | None`）：12 日 EMA。
- `ema26`（`float | None`）：26 日 EMA。
- `dif`（`float | None`）：MACD 的 DIF 值。
- `dea`（`float | None`）：MACD 的 DEA 值。
- `macd`（`float | None`）：MACD 柱值。
- `rsi6`（`float | None`）：6 日 RSI。
- `rsi12`（`float | None`）：12 日 RSI。
- `rsi24`（`float | None`）：24 日 RSI。
- `kdj_k`（`float | None`）：KDJ 的 K 值。
- `kdj_d`（`float | None`）：KDJ 的 D 值。
- `kdj_j`（`float | None`）：KDJ 的 J 值。
- `boll_upper`（`float | None`）：布林带上轨。
- `boll_mid`（`float | None`）：布林带中轨。
- `boll_lower`（`float | None`）：布林带下轨。

## 补充说明

- `trade_date` 适合单日查询，`start_date` 与 `end_date` 用于区间筛选。
- 传入 `fields` 后，响应中的每条记录只保留所选字段。
