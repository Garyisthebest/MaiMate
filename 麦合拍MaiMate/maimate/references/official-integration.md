# 麦当劳中国 MCP：已核验的真实接入

来源：[官方 GitHub 文档](https://github.com/M-China/mcd-mcp-server)、[官方网页](https://open.mcd.cn/mcp/doc)、[Agent Quick Start](https://open.mcd.cn/mcp/guide.md)。核验日期：2026-10-24；官方仓库文档快照 `e90ecc5c918f307ff5f997caa73e4d60bef375e9`。原文仅缓存于本地 `runtime/official-docs/github/`，此处维护接入结论。文档与实时工具有差异，执行以 `tools/list` 及 inputSchema 为准。

本次只读发现35项工具，GitHub表格列33项。其中活动日期/场次在文档为 `query-partystore-date/session`，实时名称为 `query-party-store-date/session`；实时另返回 `query-promotions`、`query-survey-coupon`。客户端兼容已核验的活动查询名称，未接入的新工具不因名字像查询就自动放行。官方托管服务不提供本项目的聊天记忆；口味与反馈由本地SQLite实现，不上传到麦当劳作为画像。

## 协议和凭证

- 地址固定预填：`https://mcp.mcd.cn`，不是文档站点，也不附加 `/mcp`。
- 传输：Streamable HTTP；`Authorization: Bearer <Token>`。
- Quick Start 写明服务只支持 `2025-06-18` 及之前版本。本客户端请求此版本并验证服务器协商版本。
- 先 `initialize`，再 `notifications/initialized`，随后分页 `tools/list`。
- 工具调用必须是 `tools/call`，参数为 `{"name":"query-meals","arguments":{...}}`，不能直接把工具名作为 JSON-RPC method。
- 请求 Accept 同时包含 `application/json, text/event-stream`；保留服务分配的 `Mcp-Session-Id`，后续发送协商版本的 `MCP-Protocol-Version`。
- 文档限流：每 Token 每分钟600请求。401需修正Token；429需降低调用频率。本客户端不自动重试，避免给写操作造成未知重放。
- TLS保持验证开启，不提供跳过证书验证选项；不跟随携带Token的重定向。

## 可落地流程与官方字段

以下字段供理解业务，执行前以实时 inputSchema 校验。

1. **附近门店** query-nearby-stores：2026-10-24 实时 schema 只支持 searchType=1/2，未提供经纬度字段；使用 searchType=2 + city/keyword。旧网页提及的 searchType=3 不能使用，自动定位先反向地理编码。beType=1 到店取餐、5 得来速；不默认用 searchType=1 读收藏。
   当日实际调用证实：缺少city时返回600058，尽管schema未将city列入required。客户端先补充城市，不把普通业务失败当作模型key无效；用户可只回复城市，保留门店及选餐需求。
2. **外送** `delivery-query-stores` 使用用户已选择的地址信息；`delivery-query-addresses` 涉及个人信息，只在用户选择外送且需要该信息时调用；新增地址属于写操作。
3. **菜单** query-meals：storeCode、orderType（1到店/2外送）、beType；到店取餐 beType=1 时不传 beCode，其他场景按实时 schema 传。预约格式 yyyy-MM-dd HH:mm；门店和 BE 编码来自查询结果，不生成。
4. **详情** `query-meal-detail`：同上，再带菜单返回的 `code`。根据 `rounds` / `modification` / `supportModify` 组装套餐和特制选项；不能凭名称猜code、key或默认组合。
5. **优惠** `query-store-coupons`：门店、渠道、业务类型、预约信息同菜单。它只列券，不等于自动匹配整桌最优优惠。领券 `auto-bind-coupons` 是账户写操作，需用户明确授权；不会自动执行。
6. **算价** `calculate-price`：同门店和履约参数，加 `items`；每项 `productCode`、`quantity`，可带 `couponId` / `couponCode` / `roundList` / `modification`。`withOrder` 涉及随单购会员或商品，必须显示额外商品/权益和成本，获得用户同意后才带入。`needTableware` 可能增加餐具费。
7. **最终预览**：算价结果 + 本地草稿组成预览，不存在官方独立 `order.preview`。逐项列出门店、履约方式、商品及套餐子项、数量、替换、优惠、总价、费用、付款人、时间和取消规则；未知项标为未知，不捏造备餐时间。用户确认后再次算价；若金额、商品、优惠或可售变化，使旧确认失效并重新展示。
8. **创建订单** `create-order`：携带门店、`beType`、`items`；到店的 `takeWayCode` 必须从算价结果 `takeWayList` 选择，外送需 `addressId`，企业团餐需助餐服务字段。参数以实时schema为准；文档创建订单参数表对 `orderType` 的列举不完整，不能自行假定接受或拒绝。返回的 `orderId`、`payH5Url` 用于官方付款，不在skill内自动支付或保存银行卡。
9. **订单查询** `query-order` 传 `orderId`。返回“待支付”不能显示成制作中或付款成功。
10. **取消** `cancel-order` 传 `orderId` 和 `cancelReasonCode`，需对当前订单再次确认。它不等于独立退款API，也不承诺退款时效。

## 金额与数据

菜单 `currentPrice` / `originalPrice` 示例是元字符串；算价 `data.price`、`subtotal`、`tablewarePrice` 等示例按分返回；订单详情的 `price`、`totalAmount`、`realTotalAmount` 示例又是元字符串。展示时按照对应接口转换，使用 Decimal/整数分，不凭数值大小猜单位。若实时schema或业务说明冲突，停止下单并核实。

`query-meals` 示例带 `frequent` 常点字段：本skill不使用它构建偏好画像，也不把该字段传入群汇总。营养列表不代表完整过敏原声明；菜单和详情缺失过敏原、制作时长、交叉接触信息时不能推定安全或准时。

MCP `tools/call` 返回有 `content` / `structuredContent` / `isError` 的信封；正文可能是JSON文本，还需检查内部 `success`。`McdAdapter.business_payload()` 可解出业务JSON，保留原信封供调用者处理。

## 确认和重复提交的实际保证

官方文档没有声明 `Idempotency-Key`、原子价格版本、确认令牌、库存预占或按幂等键查单能力。客户端不声称具备这些服务端保证，也不向工具arguments添加schema未声明的字段。

`confirmation.py` 用项目内SQLite保存请求哈希、scope、操作者哈希、有效期和状态；不保存完整地址/过敏原/支付信息。令牌是本地流程约束，不能证明浏览器身份或代替多人授权系统。创建订单时prepare与confirm都会重新算价，预览JSON的`total_cents`必须等于最新官方整数分总价；confirm还会比较商品明细、优惠、配送费、餐具费等快照，变化则令预览失效，写请求不会发出。一次确认绑定工具、参数、账户、服务地址和预览；参数变更、过期、重复执行均阻止。执行前原子记为 `in_flight`，异常记为 `unknown`；进程崩溃保留锁定，不能自动重发。成功记为 `complete`，返回能解析时仅记录订单号。

scope按业务操作区分，例如创建用`create:table-001`，取消用`cancel:order-001`。同一操作的重试必须复用稳定scope；不能沿用已完成创建的scope取消订单。不要换scope或删除记录绕过锁定。本地台账不保证在其他客户端、其他台账或直接API调用中实现去重；跨客户端幂等需官方明确支持。

提交未知且有orderId时查询该订单；没有orderId时提示在官方客户端核查。`order-list` 读取历史订单，只有用户明确授权用于核查当前异常时才使用，不能后台自动扫描，也不能“没搜到”就自动重新创建。

## 当前能力缺口

未见独立购物车、库存检查/预占、退款、官方订单预览工具；这些映射为null。查询当前菜单和重新算价不能称为库存锁定，也无法消除算价与创建之间的竞态。MVP使用本地草稿、算价和官方创建/付款页，不能声称实现报告中生产版的完整订单一致性、多用户认证或微信/企微群卡片服务。
