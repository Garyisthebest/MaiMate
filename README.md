# MaiMate · 麦聚桌

以个人微信为入口的麦当劳到店点餐智能体，也提供本机网页。使用麦当劳中国官方 MCP 查询门店、菜单与价格，通过千问或 DeepSeek 理解自然语言；展示订单预览并获得明确确认后，创建待支付订单，交由用户在官方收银台付款。

## 功能

- 查询真实门店和可售餐品，推荐套餐、调整搭配与取餐方式。
- 识别“穷鬼套餐”等常见别名，再匹配当前门店的真实菜单。
- 可管理的本地口味记忆，本次明确要求优先于长期偏好。
- 个人微信文字及已转写语音消息；网页聊天、模型设置和微信连接管理。
- 浏览器授权定位，以及使用独立监听器的单次微信定位链接。
- 官方算价、预览确认、订单状态与取餐码查询；未知提交结果保留本地锁定。

没有自动代扣能力。微信桥接是公开协议的 Python 兼容实现，不是官方 OpenClaw 插件。供应商未承诺服务端幂等，本地确认台账不等于这一保证。

## 安装与启动

需要 Git 和 Conda。所有环境、缓存与运行数据均保存在克隆目录内，无需修改全局配置。

```sh
git clone https://github.com/Garyisthebest/MaiMate.git
cd MaiMate/麦聚桌_skill
mkdir -p runtime/tmp runtime/cache runtime/conda-pkgs runtime/conda-envs

CONDA_PKGS_DIRS="$PWD/runtime/conda-pkgs" \
CONDA_ENVS_PATH="$PWD/runtime/conda-envs" \
XDG_CACHE_HOME="$PWD/runtime/cache" TMPDIR="$PWD/runtime/tmp" \
conda env create --prefix "$PWD/.conda" -f environment.yml

cp .env.example .env
chmod 600 .env
```

在本机 `.env` 填写自己申请的 `MCDONALD_MCP_KEY`，不要提交该文件。官方申请与接入说明：<https://open.mcd.cn/mcp/doc>。

```sh
./start-preview.sh
```

打开 <http://127.0.0.1:8765/> 点餐；在 `/settings` 配置千问或 DeepSeek，在 `/weixin` 扫码连接个人微信。模型密钥也可以通过本机 `.env` 配置。电脑休眠或服务退出时，微信通道无法接收消息。

## 隐私与发布范围

仓库发布源码、合成测试样例、空凭证模板和项目文档。真实密钥、微信绑定数据、偏好库、订单台账、聊天及定位运行数据、Conda 环境、浏览器缓存和本机助手配置均不属于发布内容，已设置 Git 忽略规则。测试中的账户、密钥、订单和位置数据为模拟样例。

运行时麦当劳凭证来自 `.env`，模型与微信配置、结构化口味偏好等保存在 `麦聚桌_skill/runtime/`。本机凭证与数据不能粘贴到 Issue、截图或日志中。模型服务会收到点餐所需的对话与菜单信息，具体处理范围见应用说明。

## 测试

在 `麦聚桌_skill/` 中运行隔离测试，不创建真实订单：

```sh
mkdir -p runtime/tmp
TMPDIR="$PWD/runtime/tmp" PYTHONDONTWRITEBYTECODE=1 \
.conda/bin/python -m pytest -q mcdonald-lunch-table/tests \
  --basetemp="$PWD/runtime/test-tmp" -o cache_dir="$PWD/runtime/pytest-cache"
```

## 项目内容

- [应用说明](麦聚桌_skill/README.md)：配置、微信、定位、记忆、账户与确认机制。
- [点餐 Skill](麦聚桌_skill/mcdonald-lunch-table/SKILL.md)：智能体执行规范与参考资料。
- [开发报告](麦聚桌_McDonalds_MCP开发报告.md)：早期产品设计，包含尚未实现的探索方案；当前能力以源码和应用说明为准。

本项目为独立开发项目，与麦当劳、腾讯及模型服务商不存在官方隶属关系。仓库暂未附加开源许可证。
