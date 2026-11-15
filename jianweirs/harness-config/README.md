# DeepSeek Harness 接入

此目录通过官方 Cordis 插件配置扩展 DeepSeek Harness，`deepseek-harness/` 保留上游源码。官方启动入口为 `dsh --profile <name> --patch <file>`；当前上游不支持用 `--config` 启动任意应用树。

`wuyu.cordis.yml` 添加官方 `@deepseek-ai/dsh-mcp-client`，以 stdio 启动项目 Python 的 `-m wuyu_harness.mcp`，并设置 `deepseek-official/deepseek-flash` 为默认主模型。`wuyu-web.cordis.yml` 为 Web 添加“无隅见微多星遥感”预设；专业选星、识别、复核及秘塔检索在 Python 后端执行。Web 预设启用文件工具和报告呈现工具。

## 本机启动

先完成 Python 后端依赖安装，并将凭据填写在项目根目录 `.env.wuyu`。不要覆盖现有奥维 `.env`。配置了 `WUYU_MAIN_BASE_URL` 和 `WUYU_MAIN_API_KEY` 时，启动器自动使用 OpenAI Chat Completions 自定义主模型；未配置时使用官方 `deepseek-official/deepseek-flash`，凭据为 `DEEPSEEK_API_KEY`。其他角色的配置见项目中的示例配置。

```powershell
# 检查本机运行环境，不发起模型请求；缺少上游安装或构建时返回非零退出码。
.\start-wuyu-harness.ps1 -Check

# 首次安装依赖并构建上游；成功后检查。需要可用的 pnpm 和网络。
.\start-wuyu-harness.ps1 -Prepare -Check

# 启动 Web，默认仅监听 127.0.0.1:3082。
.\start-wuyu-harness.ps1

# 隐藏后台启动；日志和经核验的进程记录保存在忽略目录 outputs。
.\start-wuyu-harness.ps1 -Background -NoOpen

# 重新打开已运行服务，并在当前浏览器建立本地登录状态；不会重启服务。
.\open-wuyu-harness.ps1

# 仅停止本项目记录且 PID、启动时间、解释器与启动命令一致的 Web 进程树。
.\stop-wuyu-harness.ps1

# 查看最终插件树，不调用模型。
.\start-wuyu-harness.ps1 -DumpConfig

# 用上游桥接插件实际安装的 MCP SDK 检查握手、工具列表和状态；不调用模型。
.\start-wuyu-harness.ps1 -CheckMcp

# 单次任务，任务本身会使用已配置的模型/数据接口。
.\start-wuyu-harness.ps1 -Profile headless -Task '检查无隅见微服务状态和数据目录，列出尚未配置的能力。'
```

脚本优先选用支持的系统 Node，其次选用 Codex 捆绑的 Node。上游要求 `^22.19.0 || >=24.0.0`；本机系统 Node 22.17.0 不满足要求。可显式传入 `-NodePath`、`-PythonPath` 和 `-PnpmPath`。`-Prepare` 使用上游 `pnpm install --frozen-lockfile` 和 `pnpm run build`，以安装结果为准，不把源码下载成功当成服务启动成功。

上游 Web 使用本地浏览器认证。新浏览器首次直接访问 `http://127.0.0.1:3082` 可能显示 `authentication required` 或返回 401；后台服务已启动时，运行 `.\open-wuyu-harness.ps1` 重新打开即可。它核验当前进程与监听端口，从该进程的私有日志读取启动入口并交给浏览器，不在终端显示令牌；进入页面后地址会自动移除令牌。`-Check` 或 `-WhatIf` 只核验入口，不打开浏览器。

Windows 下也可双击根目录的 `open-wuyu-harness.cmd`；它使用系统 Windows PowerShell 调用同一脚本，支持中文项目路径。

`launch.py` 读取私有 `.env.wuyu` 后，调用与上游 `pnpm dsh` 相同的 `node --import tsx/esm apps/cli/src/bin.ts` 官方入口；它没有另建代理循环。默认 Harness home 放在忽略目录 `outputs/harness-home`，也可由调用环境的 `DSH_HOME` 覆盖。应用只向 MCP 明确转发 `WUYU_`、`DEEPSEEK_`、`METASO_` 三组环境变量；奥维 PSK 不跨此接口。

## 自定义主模型

若主模型端点使用 OpenAI Chat Completions，填写 `WUYU_MAIN_BASE_URL`、`WUYU_MAIN_API_KEY`、`WUYU_MAIN_MODEL` 后，直接启动即可自动选择；也可显式指定：

```powershell
.\start-wuyu-harness.ps1 -CustomMainModel
```

这会附加 `wuyu-main-compatible.cordis.yml`，用上游 pi-ai 适配器注册 `wuyu-main` 路由。该业务路由固定为纯文本，即使端点支持图片或旧环境仍配置 `WUYU_MAIN_INPUT=text,image`，也不向主模型开放视觉输入。高分逐图和跨图判读由后端 `wuyu-jianwei-rs` 负责，科学影像由已配置科学分析角色负责；主模型组织专业结果、工具和资料。业务预设关闭 `read_image`，报告 MCP 仅返回影像/标注及报告引用，界面仍可展示原图。更换端点时应重新核验文本、工具调用和流式响应。

## 核验接入

接入验收时执行状态与目录检查；日常任务直接使用“无隅见微多星遥感”预设提交。MCP 工具显示为 `mcp__wuyu__wuyu_status`、`mcp__wuyu__wuyu_catalog` 等；完整任务提交后应返回 `run_id`，使用长等待工具直到报告形成。MCP 连接失败时，上游可能只停用这个可选插件而继续启动 Web，因此验收必须确认工具实际存在且状态调用成功。

`project-workspace.mjs` 在首次空安装时将本工程初始化为默认工作区，在已有安装中添加工程工作区；已有会话和目录归属保留。已有 Documents 会话会继续恢复，首次切换时在侧栏选择工程工作区再新建会话，后续“新会话”继承当前工程。文件服务、无原始目录会话的默认目录和沙箱默认根目录均指向本工程，MCP 始终在项目根目录运行；报告位于 `outputs/wuyu`。

普通点位任务直接提交，由后端按需选源取图，使用 `wuyu_wait_run` 长等待至完成后读取专业判读、结构化报告和影像引用；不通过紧密轮询或遍历源码猜接口。Excel 资料使用工作簿记录和图片工具，主模型只根据返回的专业结果和工具证据组织回答，不再次看图。`wuyu_workbook_records` 省略文档ID时查询本机已登记清单，业务点位编码可直接用于逐点查图。

源码安装与构建、MCP 协议接入、真实数据获取和模型端到端分析是不同的验收步骤。未配置凭据和专业服务时，应保留明确的缺项、跳过或失败状态。

依据：[官方架构](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)、[CLI 行为](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/reference/README.md)、[官方 MCP 客户端](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)。
