# Soul Mate 管理台 AC 混合玻璃界面实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Owner 管理台改造成清透工作台与温和陪伴空间的 AC 混合界面，并在 iPhone 17 主屏幕环境下保持可读、可操作和无底部白边。

**Architecture:** 保留现有 React 页面、路由和后端接口，集中改造 CSS tokens、Shell 导航和可复用玻璃组件；页面只消费语义化 tokens，不在页面内散落颜色。桌面侧栏、平板收窄导航和手机底部标签栏共享同一导航数据，`admin-app-shell` 与 `/chat` PWA 的高度和元数据状态隔离。

**Tech Stack:** React 18、TypeScript、Vite、现有 CSS tokens、CSS `backdrop-filter`/`color-mix`、VisualViewport API、TanStack Query、现有 Vitest/TypeScript 构建链。

## Global Constraints

- 管理台外层 `html、body、#root、admin-app-shell` 统一 `100vh`，不得在管理台高度链混用 `100%`、`100dvh` 和 fixed body。
- safe-area 只用于顶部状态区、底部标签栏和 Sheet 内部留白；VisualViewport 只处理键盘滚动。
- 正文、审核内容、日志和密集表格保持不透明；玻璃只用于导航、状态、卡片、弹窗和 Sheet。
- 所有交互控件保留可见焦点、aria-label、至少 44×44px 点击区域、loading/成功/失败反馈。
- 动效只使用 opacity/transform，默认 180–260ms；必须支持 `prefers-reduced-motion`。
- 不改后端接口、数据库、人设、微信绑定和聊天 PWA 的业务逻辑；不初始化或推送 Git。
- 每个代码批次后运行 `npm run typecheck`、`npm run build`、`docker compose up -d --build` 和 `/healthz` 检查。

---

### Task 1: 建立前端验收基线和管理台 Shell 结构

**Files:**
- Modify: `web/frontend/src/components/Shell.tsx`
- Modify: `web/frontend/src/App.tsx`
- Modify: `web/frontend/src/styles/global.css`
- Modify: `web/frontend/src/styles/shell.css`
- Test: `tests/test_owner_admin_ui.py`

**Interfaces:**
- Consumes: 现有 `Shell` 的导航项、人物上下文、认证状态和路由。
- Produces: `admin-app-shell` 根类、统一导航项数据、桌面侧栏/平板抽屉/手机底部导航三个呈现模式。

- [x] **Step 1: 写失败的结构回归测试**

在现有管理台 UI 合同测试中增加静态检查：Shell 必须输出 `admin-app-shell`，导航必须包含五个中文区域，聊天 `/chat` 不得复用管理台底部导航。

- [x] **Step 2: 运行测试确认失败**

运行：`python -m pytest tests/test_owner_admin_ui.py -q`  
预期：新增断言因当前 Shell 没有 `admin-app-shell` 或移动导航结构而失败。

- [x] **Step 3: 实现统一导航模型和根壳**

在 `Shell.tsx` 中把导航条目集中为一个数组，桌面/平板/手机均由同一数组渲染；根节点增加 `admin-app-shell`，`/chat` 路由继续走现有独立分支。手机导航只显示五个主要区域，其余操作仍通过页面内按钮和 Sheet 进入。

- [x] **Step 4: 运行结构测试确认通过**

运行：`python -m pytest tests/test_owner_admin_ui.py -q`  
预期：全部通过，原有登录、路由和导航断言不回归。

- [x] **Step 5: 运行前端类型检查**

运行：`npm run typecheck`（工作目录 `web/frontend`）  
预期：退出码 0。

### Task 2: 视觉 tokens、玻璃层级和高度链

**Files:**
- Modify: `web/frontend/src/styles/tokens.css`
- Modify: `web/frontend/src/styles/global.css`
- Modify: `web/frontend/src/styles/shell.css`
- Create: `web/frontend/src/styles/admin-glass.css`
- Test: `tests/test_owner_admin_ui.py`

**Interfaces:**
- Consumes: Task 1 输出的 `admin-app-shell` 类和现有主题 tokens。
- Produces: `--glass-*`、`--motion-*`、`--admin-*` 语义变量、桌面/移动响应式高度规则和玻璃降级样式。

- [x] **Step 1: 写失败的 CSS 合同测试**

检查 tokens 和样式文件包含：`--glass-nav`、`--glass-card`、`--glass-overlay`、`100vh`、`env(safe-area-inset-bottom)`、`prefers-reduced-motion`；并确认管理台样式没有将 `100dvh` 放进高度链。

- [x] **Step 2: 运行测试确认失败**

运行：`python -m pytest tests/test_owner_admin_ui.py -q`  
预期：新增玻璃 token 或高度断言失败。

- [x] **Step 3: 添加 tokens 和玻璃样式**

增加浅色/深色两套玻璃变量；使用 `backdrop-filter` 和 `-webkit-backdrop-filter`，并提供高不透明度回退。外层管理台统一 `height/min-height: 100vh`，底部导航使用 `padding-bottom: env(safe-area-inset-bottom)`，不修改聊天 PWA 的专属 CSS。

- [x] **Step 4: 运行 CSS 合同测试和类型检查**

运行：`python -m pytest tests/test_owner_admin_ui.py -q`；`npm run typecheck`。  
预期：全部通过。

### Task 3: 响应式导航、人物切换和动效

**Files:**
- Modify: `web/frontend/src/components/Shell.tsx`
- Modify: `web/frontend/src/styles/shell.css`
- Modify: `web/frontend/src/styles/admin-glass.css`
- Test: `tests/test_owner_admin_ui.py`

**Interfaces:**
- Consumes: Task 1 导航模型和 Task 2 tokens。
- Produces: `.admin-bottom-nav`、`.admin-sheet`、人物切换的共享元素 class、关闭/焦点恢复行为。

- [x] **Step 1: 写失败的交互结构测试**

检查手机导航拥有 `aria-label`、选中状态和五个入口；抽屉/Sheet 有关闭按钮和 `aria-expanded`；所有图标按钮具备可读标签。

- [x] **Step 2: 运行测试确认失败**

运行：`python -m pytest tests/test_owner_admin_ui.py -q`  
预期：当前导航缺少移动端结构或可访问性属性时失败。

- [x] **Step 3: 实现三种导航形态和 motion**

桌面显示玻璃侧栏，900px 以下改为可关闭抽屉，600px 以下显示底部玻璃标签栏。切换和 Sheet 只动画 `transform/opacity`，默认 180–260ms；增加 reduced-motion 覆盖；点击遮罩、Escape 和路由变化关闭抽屉。

- [x] **Step 4: 运行交互测试和前端构建**

运行：`python -m pytest tests/test_owner_admin_ui.py -q`；`npm run typecheck`；`npm run build`。  
预期：全部通过。

### Task 4: 总览、联系人和设置页面的玻璃层级与移动信息架构

**Files:**
- Modify: `web/frontend/src/pages/OverviewPage.tsx`
- Modify: `web/frontend/src/pages/ContactsPage.tsx`
- Modify: `web/frontend/src/pages/SettingsPage.tsx`
- Modify: `web/frontend/src/styles/pages.css`
- Modify: `web/frontend/src/styles/forms.css`
- Test: `tests/test_owner_admin_ui.py`

**Interfaces:**
- Consumes: Task 2 tokens、Task 3 navigation/Sheet、现有 API 数据结构。
- Produces: 直白中文的状态卡、移动折叠区、统一表单反馈和关系/记忆检查点展示。

- [x] **Step 1: 写页面可访问性和文案合同测试**

检查总览、联系人、设置页面仍有主标题、空状态、错误状态、表单 label、保存中状态和确认按钮；禁止出现只在 hover 可见的核心操作。

- [x] **Step 2: 运行测试确认失败**

运行：`python -m pytest tests/test_owner_admin_ui.py -q`  
预期：新增页面结构断言先失败。

- [x] **Step 3: 改造页面层级**

总览使用人物卡和系统卡的轻玻璃；联系人把关系/检查点/记忆关联放入可折叠区；设置把常用配置放在首屏，高级配置放进 Sheet 或 details。所有正文卡片保持不透明底色，表单错误就地显示。

- [x] **Step 4: 运行页面测试和构建**

运行：`python -m pytest tests/test_owner_admin_ui.py -q`；`npm run typecheck`；`npm run build`。  
预期：全部通过。

### Task 5: 人设、微信与公共组件收口

**Files:**
- Modify: `web/frontend/src/pages/PersonaPage.tsx`
- Modify: `web/frontend/src/pages/WechatPage.tsx`
- Modify: `web/frontend/src/components/Badge.tsx`
- Modify: `web/frontend/src/components/StatusStates.tsx`
- Modify: `web/frontend/src/styles/status.css`
- Modify: `web/frontend/src/styles/dialog.css`
- Test: `tests/test_owner_admin_ui.py`

**Interfaces:**
- Consumes: Task 2 的玻璃 tokens、Task 3 的 Sheet 和公共状态组件。
- Produces: 人设审核、微信媒体和危险确认的一致视觉与反馈，不改变既有 API 行为。

- [x] **Step 1: 写失败的公共状态测试**

检查素材审核、桥接错误、敏感内容展开和确认弹窗都拥有可读按钮文本、loading/成功/失败状态和可关闭路径。

- [x] **Step 2: 运行测试确认失败**

运行：`python -m pytest tests/test_owner_admin_ui.py -q`  
预期：至少一个旧页面缺少统一反馈断言。

- [x] **Step 3: 复用公共组件改造页面**

移除重复的状态徽章/空状态/确认样式，统一使用公共组件；玻璃只包住操作区域，长审核正文不透明；敏感内容的查看按钮保持二次确认和明确关闭反馈。

- [x] **Step 4: 运行完整前端与后端验证**

运行：`npm run typecheck`；`npm run build`；`python -m pytest -q -p no:cacheprovider`。  
预期：前端构建成功，后端 311 项基线及新增 UI 合同全部通过。

### Task 6: Docker、真机尺寸和文档验收

**Files:**
- Modify: `docs/PWA_IPHONE.md`
- Modify: `README.md`
- Modify: `STATUS.md`
- Modify: `PLAN.md`

**Interfaces:**
- Consumes: Task 1–5 的完成代码和测试结果。
- Produces: 管理台移动入口、白条排查、回滚和故障处理说明。

- [x] **Step 1: 执行 Docker 同步构建**

运行：`docker compose up -d --build`；等待容器 healthy；运行 `curl.exe -sS http://127.0.0.1:8080/healthz`。  
预期：返回 `{"status":"ok"}`。

- [x] **Step 2: 做静态尺寸和编码扫描**

检查管理台样式中外层高度只使用 `100vh`，没有新增 BOM 或常见中文乱码；确认 `prefers-reduced-motion`、safe-area 和焦点样式仍存在。

- [x] **Step 3: 更新文档和状态**

在 README 增加管理台桌面/手机使用说明；在 PWA 子文档增加管理台入口和白条验收；本轮同时更新根目录 `STATUS.md` 与 `PLAN.md`。

- [ ] **Step 4: 完成真机验收清单**

在 iPhone 17 添加到主屏幕后验证：底部无白边、标签栏不遮内容、键盘不遮输入、Sheet 可关闭、深浅色和 reduced-motion 可用。桌面和手机验收均保留失败截图及修复记录，不把截图正文写入日志。

## Self-review

- 设计文档中的桌面、平板、iPhone 布局对应 Task 1、3、6。
- 玻璃层级、颜色、动效、降级和 reduced-motion 对应 Task 2、3、5。
- `100vh`、safe-area、VisualViewport 和白条规则对应 Task 2、3、6。
- 易读性、确认操作、异步反馈和无障碍对应 Task 4、5。
- 计划不包含后端、数据库、人格、微信链路或聊天 PWA 业务改造，符合已确认范围。
- 已扫描本计划，没有待补字段、占位函数或未定义的跨任务接口。
