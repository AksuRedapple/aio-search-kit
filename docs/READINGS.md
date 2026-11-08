# 想懂"为什么"，读这几篇

> 本仓是**操作层**（照着做）；下面三篇是**现场层**（为什么会长成这样）。
> 顺序建议：先看一篇现场，再回来读对应的文档，最后跑 `tests/test_offline.py`。

## 对照表

| 你手上的问题 | 读这篇（现场） | 再看这份文档（操作） |
|---|---|---|
| 为什么缓存命中率这么低？ | 《给搜索装刹车：收敛比限额难》 | [`journal-fields.md`](journal-fields.md)（cacheHit 字段那一行） |
| 为什么不能只看"有结果"？ | 《花钱的路径必须先有 mock》 | [`eval-protocol.md`](eval-protocol.md)（三判据） |
| 为什么评测跑了却没闭环？ | 《评测在左口袋，运行时在右口袋》 | [`backbone-design.md`](backbone-design.md)（记账与出处层） |
| 为什么实现里那些字段不可信？ | 《四种假：假红、假绿、假通过、假故事》 | [`architecture-study.md`](architecture-study.md)（35 条静默失败点） |
| 为什么基准数字会被污染？ | 《基准测试的三个陷阱：负载、单位、宿主》 | [`regress.sh.example`](regress.sh.example)（阈值按题量取粒度） |
| 为什么公共面选 MCP？ | 《数据面不该跑在命令通道上》 | [`backbone-design.md`](backbone-design.md)（为什么是 MCP） |

## 三层各管什么

```
primer（知识文）   学会：概念与机制，能在新场景里推演
essay（实践篇）    看见：一次真实事故 + 数字 + 判据怎么来的
repo/docs（操作层） 照做：字段表、判据、脚本、边界
```

**判据：如果一篇 essay 读完，你只知道"发生过什么"，那它是故事；只有当它能指向一份可执行的文档，它才是资产。**
