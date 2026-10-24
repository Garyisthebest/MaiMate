# 内部接口与真实能力

内部名称只负责路由，不自动改写arguments。使用官方字段，先读取 [official-integration.md](official-integration.md)，再以实时 `tools/list.inputSchema` 校验。

| 内部接口 | 官方工具 | 说明 |
|---|---|---|
| store.search | query-nearby-stores | 到店/得来速门店 |
| store.delivery | delivery-query-stores | 外送门店 |
| menu.get | query-meals | 当前菜单 |
| menu.detail | query-meal-detail | 套餐/特制详情 |
| promotion.list | query-store-coupons | 可用券，非整桌自动优化 |
| price.calculate | calculate-price | 算价；费用示例以分计 |
| order.submit | create-order | 创建待支付订单，需确认 |
| order.status | query-order | 查询指定订单 |
| order.cancel | cancel-order | 取消指定订单，需确认 |
| nutrition.list | list-nutrition-foods | 营养信息不等于过敏原证明 |
| campaign.calendar | campaign-calendar | 可用来解释当月活动 |
| availability.check / promotion.match / cart.create / cart.reprice / order.preview / order.cancel_or_refund | null | 未提供对应独立能力，明确降级 |

状态：草稿→收集→候选→算价预览→等待确认→执行中→已创建/待支付→按查询结果更新。异常或响应丢失进入未知；查询或人工核查优先，禁止盲目重发。

本地确认令牌绑定账户、工具、参数、操作者、scope和预览；与麦当劳服务器的授权/幂等无关。详细保证和限制见接入说明。
