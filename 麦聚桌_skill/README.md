# 麦聚桌 · 你的点餐搭子

微信里的到店点餐智能体，同一引擎也提供网页聊天。支持真实门店与菜单、千问／DeepSeek 自然语言理解、套餐换搭配、官方算价、确认后创建订单、官方收银台付款，以及订单状态与取餐码查询。

## 开始使用

首次克隆请先按根目录 README 创建项目内 Conda 环境并复制空凭证模板。已有环境位于 .conda/，麦当劳 key 由后端从 .env 读取。

    cd MaiMate/麦聚桌_skill
    ./start-preview.sh

- 点餐：http://127.0.0.1:8765/
- 模型与定位设置：http://127.0.0.1:8765/settings
- 个人微信连接：http://127.0.0.1:8765/weixin

在设置页选择千问或 DeepSeek，填写 API key，保存并测试即可。默认中国内地千问接口 / qwen-plus、DeepSeek 官方接口 / deepseek-chat；模型名可改。配置实时生效，无需重启。key 不回显，保存在项目 runtime/model.json，权限600。也可通过本项目 .env 的 LLM_PROVIDER、LLM_MODEL、LLM_API_KEY 配置；页面配置优先。

模型会收到本次对话和必要菜单、选配信息，不会收到麦当劳 Token、微信凭证、历史画像或完整账户资料。无模型 key 时可用明确指令选店、套餐、换选项，但不是完整自然语言能力。

“保存并测试”会验证微信／网页实际使用的结构化点餐工具调用与本地意图校验。DeepSeek 点餐请求显式关闭思考模式，以兼容强制 `order_intent` 调用；无需因此更换已配置的 key 或模型。接口错误按参数、认证、余额和限流分别提示，不回显供应商原文。

## 到店后的流程

1. 发门店名，或点“使用当前位置”。默认一位，预算不是必填。
2. 从真实搜索结果选门店；发“推荐牛肉套餐，饮料无糖”。
3. 说“套餐1”或“第二个”；可以继续换饮料或修改真实选配。
4. 选择堂食／外带，查看官方应付金额和完整搭配。
5. 回复“确认下单”，获得官方付款链接；本人在官方收银台确认付款。
6. 发“订单状态”查看进度、取餐码；另买一单需明确说“重新点餐”。

本项目没有代扣 API。订单未支付、已支付、配餐中等状态严格区分。算价与创建之间没有库存锁定或原子价格版本，不保证官方服务器端幂等。

## 自动定位与微信

浏览器定位需要系统授权。为适配当前官方 city/keyword schema，使用 Photon（备用 Nominatim）的 OpenStreetMap 数据将 WGS84 坐标转成城市和地标，再查门店。解析结果只在内存缓存，每秒最多一次请求，不做自动补全。定位不可用时可发门店名，不拿 IP 或默认城市冒充位置。

个人微信协议未暴露原生位置消息。设置页启用微信定位后，在微信发“定位”；打开单次链接授权，查看门店列表，回微信回复“选店1”。令牌单次使用、5分钟到期。

Cloudflare 官方组件位于 runtime/bin，通过 GitHub 官方 release SHA256 校验。重装：

    TMPDIR="$PWD/runtime/tmp" PYTHONDONTWRITEBYTECODE=1 .conda/bin/python mcdonald-lunch-table/scripts/install_location_tunnel.py

隧道只指向独立127.0.0.1:8766接收器；后台8765仍仅本机访问。临时隧道没有可用性保证，部分网络或微信浏览器可能拦截，可在系统浏览器打开或发门店名继续。重启后需重新开启通道。没有修改全局 OpenClaw、Conda 或系统代理配置。

微信桥接遵循腾讯公开协议，见 mcdonald-lunch-table/references/weixin-integration.md。只回复扫码绑定者的新文字或已转写语音，不接群。重启后默认暂停，在连接页恢复；电脑休眠、断网或服务退出时无法接收。

## 本地数据与确认

### 越用越懂你的口味

在微信说“记住，我爱吃穷鬼套餐，喜欢无糖饮料”。“穷鬼套餐”按1+1超值／随心配系列查当前真实菜单，不写死活动价。明确口味和多次选餐会影响后续推荐；“今天想换牛肉”“今天要普通可乐”等本次要求优先，不因一次点餐永久覆盖习惯。

发“我的偏好”查看，“忘记无糖”删除单项，“清除偏好”全部删除，“关闭记忆”停止学习和使用。微信连接页也可查看和管理。结构化偏好仅保存在项目 `runtime/dining-memory.sqlite3`，权限600；不保存完整聊天、健康信息或精确位置，不自动读官方历史订单。

微信页新增最近20轮脱敏对话，方便核对异常；只在本次服务内存中，重启清空，无法拉取微信旧聊天。只读接口失败后可发“重试”，会实际执行该查询；创建订单不会被重放。

### 麦当劳账号与微信账号

点餐使用申请当前 MCP Token 时授权的麦当劳账号，微信扫码只是收发入口，不会切换麦当劳账号。发“账号说明”了解授权，“我的积分”或“我的优惠券”按需读取资产；账户资产不发送给模型或保存进偏好库。当前接口不提供完整个人档案、手机号或会员等级，官方付款页是否需要验证以页面为准。

所有新文件、依赖、缓存、临时数据和凭证只在本项目内：

- .env：麦当劳 key，不公开、不记录到日志。
- runtime/model.json：模型配置，不回显密钥。
- runtime/weixin/：绑定、游标、消息哈希去重。
- runtime/confirmations.sqlite3：请求哈希、预览确认、提交状态、已知订单号；不存原始聊天。
- runtime/web-session.key：本机浏览器会话签名。
- runtime/location/、runtime/bin/：定位隧道、日志、组件。

确认前后官方算价；换搭配、换店、过期或金额变化使旧确认失效。写请求前原子记录 in_flight，未知结果保持锁定。没有订单号时在官方客户端核查，不删除台账或换会话绕过。通用 CLI 禁止直接调用写工具。

## 测试

    TMPDIR="$PWD/runtime/tmp" PYTHONDONTWRITEBYTECODE=1 .conda/bin/python -m pytest -q mcdonald-lunch-table/tests --basetemp="$PWD/runtime/test-tmp" -o cache_dir="$PWD/runtime/pytest-cache"

工具协议和金额单位见 references/official-integration.md。模型及订单链路在隔离模拟服务器验证；真实 MCP 只测试门店、菜单、详情和算价，不创建订单或测试扣款。

如需重建 Conda，缓存同样留在项目：

    CONDA_PKGS_DIRS="$PWD/runtime/conda-pkgs" CONDA_ENVS_PATH="$PWD/runtime/conda-envs" XDG_CACHE_HOME="$PWD/runtime/cache" TMPDIR="$PWD/runtime/tmp" conda env create --prefix "$PWD/.conda" -f environment.yml
