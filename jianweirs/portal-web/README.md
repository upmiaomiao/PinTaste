# 无隅见微产品门户

无构建依赖的中文业务界面，由 `wuyu_harness.portal.create_portal_app` 同源提供静态文件和 `/portal/api`。

- `app.mjs`：首次管理员引导、登录、任务与续问、四阶段事实进度、证据影像、资料库及工作簿按点选图。
- `admin.mjs`：管理员模型、数据、三路联网搜索（DeepSeek 网页、秘塔学术、微信公众号）、任务策略与用户管理；业务用户不显示管理入口。
- `lib.mjs`：安全文本展示、阶段映射及请求构造。
- `assets/logo.png`：用户原无隅 AI 平台 Logo。

沿用原平台 `official/web/main.jsx` 最终加载的 `neutral.css` 白底、浅灰与松绿色系。所有分析数据、影像及状态来自后台，不使用展示用的虚构结果。用户未明确选择任务类型时不传 `task_types`，由 Harness 规划；用户未切换监管依据选项时不覆盖管理员策略。续问传 `parent_task_id`，不发送空资源数组覆盖原任务。管理员密钥输入留空保持原值，不在浏览器存储秘密。

运行轻量前端检查：

```powershell
node --check portal-web/app.mjs
node --check portal-web/admin.mjs
node --test portal-web/lib.test.mjs
```

业务页面只显示任务计划、实际处理进度及简洁证据摘要，不展示模型内部推理文本。
