# 麦当劳中国 MCP 接入指南（Agent Quick Start）

## 1. 申请 MCP Token

申请地址：https://open.mcd.cn/mcp

步骤：
1. 访问 https://open.mcd.cn/mcp ，点击右上角【登录】
2. 使用手机号验证码登录
3. 登录后点击右上角【控制台】→ 点击【激活】
4. 同意服务协议后，MCP Token 即刻生成，请复制并妥善保存

## 2. 接入配置

### 2.1 接入地址

```
https://mcp.mcd.cn
```

### 2.2 传输协议与安全

使用 **Streamable HTTP** 协议接入。请求头中携带 Authorization 字段：

```text
Authorization: Bearer YOUR_MCP_TOKEN
```

### 2.3 MCP 配置 JSON

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_TOKEN"
      }
    }
  }
}
```

### 2.4 注意事项

- MCP Server 当前仅支持 MCP Version 2025-06-18 及之前的版本
- 每个 Token 每分钟最多 600 次请求，超限返回 429
- 请确保 MCP Client 支持 Streamable HTTP 协议
- 请妥善保管 MCP Token，避免泄露

## 3. 错误码

| code | 原因 | 处理建议 |
|:----:|:----:|:---------|
| 401 | MCP Token 无效、已过期或未提供 | 检查 Authorization 请求头和 MCP Token 配置 |
| 429 | 触发限流（超过 600 次/分钟） | 降低请求频率，合理控制调用间隔 |

## 4. 可用工具列表

| Tool | Name | Description |
|:-----|:-----|:------------|
| list-nutrition-foods | 餐品营养信息列表 | 获取麦当劳常见餐品的营养成分数据，包括能量、蛋白质、脂肪、碳水化合物、钠、钙等信息 |
| delivery-query-addresses | 获取用户可配送地址列表 | 查询用户已创建的配送地址列表，用于外送点餐时选择配送地址 |
| delivery-create-address | 新增配送地址 | 当用户无可配送地址或需新增收货地址时使用 |
| delivery-query-stores | 外送场景查询可配送门店 | 外送场景下查询用户收货地址附近可配送门店 |
| query-meal-assistance | 查询助餐服务 | 仅企业团餐场景下，查询该门店支持的助餐服务 |
| query-nearby-stores | 查询附近门店 | 查询用户提供地址附近的麦当劳餐厅 |
| query-store-coupons | 查询用户在当前门店可用券 | 查询用户在当前门店下可使用的优惠券列表 |
| query-meals | 查询当前门店可售卖的餐品列表 | 查询当前门店可售卖的餐品菜单（分类、餐品编码、标签等） |
| query-meal-detail | 查询餐品详情 | 根据餐品编码查询餐品详情（套餐组成、默认选择等） |
| calculate-price | 商品价格计算 | 根据用户选购商品列表（可含优惠券）计算商品金额、配送费、优惠金额及应付总价 |
| create-order | 创建订单 | 根据门店信息、就餐方式、商品列表等信息创建订单，返回订单详情与支付链接 |
| query-order | 查询订单详情 | 查询订单状态、订单内容、配送信息等 |
| campaign-calendar | 活动日历查询工具 | 查询麦当劳中国当月的营销活动日历 |
| available-coupons | 麦麦省券列表查询 | 查询用户当前可领取的麦麦省优惠券列表 |
| auto-bind-coupons | 麦麦省一键领券 | 自动领取麦麦省所有当前可用的麦当劳优惠券 |
| query-my-coupons | 我的优惠券查询 | 查询用户账户下所有可用优惠券列表 |
| query-my-account | 我的积分查询 | 查询用户积分账户信息，包括可用积分、累计积分等 |
| mall-points-products | 积分兑换商品列表 | 查询麦麦商城内可用积分兑换的餐品券 |
| mall-product-detail | 积分兑换商品详情 | 查询指定积分兑换商品券的详细信息 |
| mall-create-order | 积分兑换商品下单 | 使用积分兑换指定餐品券，完成积分扣减并发放券码 |
| now-time-info | 获取当前时间信息 | 返回当前的完整时间信息，以便于 LLM 知道当前的时间和日期 |
