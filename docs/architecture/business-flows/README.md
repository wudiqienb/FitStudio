# 核心业务流程 · Business flows

图中写事务对应 Django `transaction.atomic()` 和 SQLite `IMMEDIATE`。同一事务内发生校验或写入异常时按原服务回滚。箭头表示业务动作与状态，数据库结构见 [ER 图](../database-er.md)。

1. [新会员开通与首次购课](01-member-onboarding.md)
2. [老会员续课](02-renewal.md)
3. [会员自主预约与改期](03-self-booking.md)
4. [正常取消](04-normal-cancellation.md)
5. [迟取消与爽约](05-late-cancel-no-show.md)
6. [教练取消与补课](06-coach-cancel-makeup.md)
7. [课时扣减与冲正](07-deduction-reversal.md)
