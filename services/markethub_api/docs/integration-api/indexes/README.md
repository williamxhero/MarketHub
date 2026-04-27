# indexes

指数接口保持 MarketHub 统一口径，属于当前 A 股股票市场基础数据范围。

## 当前范围

- 提供指数目录、画像、日线行情和成分。
- 不暴露底层 provider 名称给调用方。
- 当前能力链路是 `static_core / Store -> Tushare/OpenTDX -> efinance -> mootdx -> akshare`。

## 入口文档

- `/docs/indexes/catalog`
- `/docs/indexes/profile`
- `/docs/indexes/quotes`
- `/docs/indexes/members`
