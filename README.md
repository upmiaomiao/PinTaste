# 无隅 / PinTaste 测试站源码

这个仓库是测试站（https://test-ai.wuyullm.com/）代码的唯一来源。`main` 分支 = 测试站当前正在运行的代码。

正式站（/opt/wuyu-releases）暂未纳入本仓库管理。

## 目录

| 目录 | 内容 | 测试站如何生效 |
| --- | --- | --- |
| `platform` | 问答平台前端、接口和 worker | 容器 `wuyu-staging-platform` 只读挂载 `source/platform/official`；改源码后重启容器。前端需在服务器构建（见下） |
| `backend` | PPT 后端、workflow worker 和测试 | 容器 `wuyu-staging-backend`、`wuyu-staging-workflow-worker` 跑镜像里的代码，不挂载源码；见"后端注意事项" |
| `harness` | Work 工作台前端和依赖包 | `wuyu-staging-router.service` 启动时读取；改后重启该服务 |
| `work-agent` | 工作台工具桥、面板（`dist/ui/index.html`）和工具 | 同上，重启 `wuyu-staging-router.service` |
| `router` | 路由和模型网关 | 对应的 systemd 服务 |
| `presets`, `seed` | 预设和初始化定义 | — |
| `ops` | 测试站 PostgreSQL 备份、迁移脚本、systemd 单元、代码管理脚本 | — |
| `jianweirs` | 遥感程序副本 | 不在本次管理范围 |

## 代码管理流程

服务器工作副本是 `/opt/wuyu-staging/source`，它的 `origin` 就是本仓库。**它同时被运行中的容器挂载，所以在这个目录里不要切换分支、不要 reset/checkout 文件**——那等于直接改线上。

每次改动（对应一个 `operations/opNNN-名称/` 操作目录）：

1. **准备**：在操作目录里做候选副本、备份和测试（与以往做法一致），不直接改 `source`。
2. **上线**：经确认后，把候选文件复制进 `source`（以及需要时复制进容器），重启对应服务，核对哈希。
3. **提交**：在 `source` 里、`main` 分支上提交，只 `git add` 本次改动的文件。提交说明第一行以 `opNNN:` 开头，正文写清改了什么、为什么，以及操作目录路径。
4. **推送**：运行 `ops/git/push_checked.sh`。它先用 gitleaks 只扫描尚未推送的新提交，有发现就停止，否则推送到 GitHub。

别人在 GitHub 上基于 `main` 提交的补丁：先在操作目录里用独立克隆或 `git worktree` 验证，上线后再在 `source` 里 `git cherry-pick`（保留原作者），与上线内容不一致的地方用单独的提交说明。

## 不进仓库的内容

凭据、API 密钥、密码、环境文件、`private/` 目录、数据库与备份、上传的业务文件、日志、`.bak` 副本和构建环境文件都由 `.gitignore` 排除，也不要用 `git add -f` 强行加入。凭据通过部署环境或测试站 `private/` 机制配置。

## 构建

问答站前端（构建会清空 `dist`，之后必须把旧的打包文件合并回来再生成清单，否则开着旧页面的浏览器会加载失败）：

```bash
cd /opt/wuyu-staging/source/platform/official
PATH=/opt/node/bin:$PATH node ../node_modules/vite/bin/vite.js build --config vite.config.mjs
# 把上一版 dist/assets 中新构建里没有的文件复制回 dist/assets，然后：
python3 ../ops/build_static_assets_manifest.py dist
```

Work 面板 `work-agent/dist/ui/index.html` 不能手改或用旧 TypeScript UI 重建，只能用各步骤的哈希锁定转换脚本生成（例如 `work-agent/tools/page-compare-ui.cjs`，见 `work-agent/PAGE_COMPARE.md`）。

## 后端注意事项

- 测试站后端镜像目前是 `sha256:6571da12…`。op191 的 PPT 阅读修复和设计习惯后端是用 `docker cp` 复制进两个容器的，代码与本仓库 `main` 一致；**从镜像重建这两个容器前，必须先用 `main` 构建新镜像**，否则这些改动会丢失。
- PostgreSQL 下后端拒绝通过 `create_app` 跑 Alembic，应用账号 `ppt_app91` 没有建表权限，表归迁移账号 `ppt_migrator91`，并逐表授权给应用账号（数据库没有默认授权）。改表结构请参照 `ops/postgres/migrate026_design_habits.py`：用迁移账号在单个事务里建表、授权、改版本号，并先在按真实角色恢复的副本上重演。

## 安全检查

2026-11-15 首次导出时用 Gitleaks 8.30.1 和额外的凭据规则做过扫描；此后每次推送都由 `ops/git/push_checked.sh` 扫描新增提交。仓库从快照提交开始，不包含服务器上更早的 Git 历史。
