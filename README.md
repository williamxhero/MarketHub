# MarketHub 是个什么东西？

**MarketHub 是给底层的超级引擎 `QuoteMux` 套上了一层好用的 HTTP API 接口、超详细的文档 和 管理界面**

如果你还不了解底层的 [QuoteMux](https://github.com/williamxhero/QuoteMux) 是干嘛的，一句话概括：它是一个**金融行情数据的超级聚合器**。它没有造什么新轮子，而是把 `Tushare`、`AkShare`、`eFinance`、`OpenTdx` 这些你平时常用的底层数据源**全部打包整合在了一起**，并且加上了**可配置的本地缓存**。

**为什么要这么折腾？主要是为了解决直接搞这些数据源时的一堆破事：**

- **极其不稳定：** 单一数据源经常报错，或者某些特定数据总是缺失。
- **接口乱七八糟：** 换个数据源就要重写一遍对接代码，依赖包还会互相冲突。
- **容易被封 IP：** 很多底层库不带缓存，你稍微多调几次，就被限制调用频率了。

`QuoteMux` 帮你在这些底层库之上垫了一层。你的业务代码、HTTP API 或是管理界面，只需要和 `QuoteMux` 的**一套稳定接口**打交道，彻底把系统和特定的数据源解绑。




## 安装

请使用 AI 安装并跑通本项目，提示词示例：“阅读 https://github.com/williamxhero/MarketHub/AIREADME.md 并在 本机 D:\MarketHub\ 目录中安装这个项目”