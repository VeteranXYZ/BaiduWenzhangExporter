# QzoneArchive UI 规范提取

从 `~/GitHub/QzoneArchive` 源码 + 运行截图逆向整理，供模仿参考。

**技术栈**：Vue 3 + PrimeVue 4（Aura 预设）+ primeicons + Tauri 2
**风格定位**：浅色为底的现代桌面应用。蓝色单主色、大圆角、极轻阴影、留白充足。
不是 Material，不是拟物，接近 Linear / Vercel 那一挂的"克制的 SaaS 风"。

---

## 一、设计令牌（可直接粘贴）

```css
:root {
  font-family: Inter, "Noto Sans SC", system-ui, sans-serif;
  color: #18212f;
  background: #f3f6fb;
  font-synthesis: none;
  text-rendering: optimizeLegibility;

  --sidebar-width: 248px;
  --app-bg:        #f3f6fb;                        /* 页面底色，冷调浅灰蓝 */
  --surface:       rgba(255, 255, 255, .92);       /* 卡片（半透明）*/
  --surface-solid: #fff;                           /* 侧栏/顶栏（实色）*/
  --border:        #e4eaf2;
  --muted:         #697386;                        /* 次要文字 */
  --heading:       #172033;
  --primary:       #2878ff;
  --shadow:        0 12px 32px rgba(30, 55, 90, .07);
}

/* 暗色：挂在根元素的 .app-dark 类上，不用 media query —— 可手动切换 */
.app-dark {
  --app-bg:        #0f141d;
  --surface:       rgba(27, 34, 47, .94);
  --surface-solid: #1b222f;
  --border:        #293345;
  --muted:         #98a4b7;
  --heading:       #f3f6fb;
  --shadow:        0 12px 32px rgba(0, 0, 0, .18);
  color-scheme: dark;
}
```

> 暗色用 `.app-dark` 类名而非 `prefers-color-scheme`，这样能同时支持"跟随系统"和"手动切换"。
> PrimeVue 侧配置 `darkModeSelector: ".app-dark"` 对齐。

### 语义色（图标底色对）

每个都是「深色前景 + 同色系极浅背景」，饱和度压得很低：

| 名称 | 前景 | 背景 | 用途 |
|---|---|---|---|
| blue | `#2878ff` | `#eaf2ff` | 主要/动态 |
| purple | `#8557e8` | `#f1ebff` | 图片/媒体 |
| green | `#21a66b` | `#e7f8f0` | 成功/评论 |
| orange | `#dd7a18` | `#fff1df` | 存储/警示 |
| danger | `#df4646` | `#fff0f0` | 删除/退出 |

---

## 二、整体布局

```
┌──────────┬────────────────────────────────────────────┐
│          │  顶栏 sticky · 56px · 半透明               │
│  侧边栏   ├────────────────────────────────────────────┤
│  248px   │                                            │
│  实色     │   内容区 max 1440px · padding 28/32/48     │
│  可折叠   │                                            │
│  →78px   │                                            │
│          │                                            │
│ [设置]   │                                            │
└──────────┴────────────────────────────────────────────┘
```

```css
.desktop-sidebar {
  position: fixed; inset: 0 auto 0 0; z-index: 20;
  width: var(--sidebar-width);
  display: flex; flex-direction: column;
  padding: 22px 10px;
  background: var(--surface-solid);
  border-right: 1px solid var(--border);
  transition: width .2s;
}
.app-workspace { margin-left: var(--sidebar-width); transition: margin-left .2s; }
.sidebar-collapsed .desktop-sidebar { width: 78px; }
.sidebar-collapsed .app-workspace { margin-left: 78px; }

.page-content {
  width: min(1440px, 100%); margin: 0 auto;
  padding: 28px 32px 48px;
}
```

**折叠交互的巧思**：折叠后 logo 方块变成展开按钮——hover 时 app 图标淡出、折叠箭头淡入（两个图标叠在同一位置，靠 `opacity` 切换）。展开状态下则在侧栏右上角单独放一个折叠按钮。

---

## 三、核心组件

### 3.1 顶栏

```css
.topbar {
  position: sticky; top: 0; z-index: 15;
  min-height: 56px;
  display: flex; align-items: center; justify-content: space-between; gap: 24px;
  padding: max(10px, env(safe-area-inset-top)) 28px 10px;
  background: color-mix(in srgb, var(--surface-solid) 65%, transparent);
  border-bottom: 1px solid var(--border);
}
```

左侧是**双行标题**，这是这套 UI 最有辨识度的一个细节：

```
QZONE ARCHIVE        ← eyebrow：10px / 700 / letter-spacing .13em / 主色 / 全大写英文
概览                  ← 22px / letter-spacing -.02em
```

```css
.topbar-eyebrow, .section-kicker {
  margin: 0; color: var(--primary);
  font-size: 10px; font-weight: 700; letter-spacing: .13em;
}
.topbar h1 { margin: 2px 0 0; font-size: 22px; letter-spacing: -.02em; }
```

右侧：主题切换（圆形 text 按钮）+ 账号 chip。

**账号 chip 的微交互**：默认显示头像+昵称+QQ号+下拉箭头；hover 时箭头淡出、**退出图标淡入并变红**，底色转成浅红。一个控件承载两种意图，不额外占位。

### 3.2 侧栏导航项

```css
.side-navigation a {
  display: flex; align-items: center; gap: 14px;
  min-height: 46px; padding: 0 14px;
  border-radius: 12px;
  color: var(--muted); font-weight: 600;
  text-decoration: none; white-space: nowrap; overflow: hidden;
  transition: .18s;
}
.side-navigation a i { width: 20px; flex: 0 0 20px; font-size: 17px; }
.side-navigation a:hover { color: var(--heading); background: var(--app-bg); }
.side-navigation a.router-link-active { color: #1768ff; background: #eaf2ff; }
.app-dark .side-navigation a.router-link-active { color: #78aaff; background: #172b4d; }
```

图标固定 20px 宽，折叠时只需 `span { display:none }` + 居中，图标位置不跳动。

「设置」被单独放在 `.sidebar-footer`（`margin-top:auto` + 上边框），与主导航视觉分离。

### 3.3 Hero 面板（页面顶部的蓝色大横幅）

```css
.hero-panel {
  display: flex; align-items: center; justify-content: space-between; gap: 28px;
  padding: 30px 34px; border-radius: 20px; color: #fff;
  background:
    radial-gradient(circle at 85% 0%, rgba(255,255,255,.2), transparent 32%),
    linear-gradient(120deg, #155fe4, #3d8fff 62%, #6eb6ff);
  box-shadow: 0 18px 42px rgba(31,105,231,.22);
}
.hero-panel h2 { margin: 8px 0; font-size: clamp(22px, 3vw, 32px); line-height: 1.3; max-width: 680px; }
.hero-panel p  { margin: 0; color: rgba(255,255,255,.82); }
.hero-panel .section-kicker { color: #d9e9ff; }
.hero-panel .p-button { color: #1768db; background: #fff; border-color: #fff; }  /* 反色按钮 */
```

两层渐变叠加是关键：底层 120° 三段线性渐变给方向感，上层右上角的径向白光给体积感。阴影带蓝色调（`rgba(31,105,231,.22)`）而非中性灰，让面板像在发光。

### 3.4 卡片

```css
.stat-card, .surface-card {
  background: var(--surface);
  border: 1px solid var(--border);
  box-shadow: var(--shadow);
}
.surface-card { border-radius: 18px; padding: 22px; }

.stat-card {
  display: flex; align-items: center; gap: 15px;
  min-height: 118px; padding: 20px; border-radius: 16px;
}
.stat-card p      { font-size: 12px; font-weight: 600; color: var(--muted); }  /* 标签 */
.stat-card strong { display: block; margin: 3px 0; font-size: 24px; }          /* 数值 */
.stat-card small  { font-size: 11px; color: var(--muted); }                    /* 补充 */

.stat-icon { display: grid; place-items: center; width: 44px; height: 44px; flex: 0 0 44px; border-radius: 13px; }
```

统计卡的结构固定为：**左侧 44px 圆角色块图标 + 右侧三行（标签/数值/补充）**。

### 3.5 区块标题

```css
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.section-heading h3 { margin: 5px 0 0; font-size: 18px; }
```

与顶栏同构——上面一行小号大写英文 kicker，下面一行中文标题，右侧放操作按钮（「查看全部」用 `severity="secondary" text size="small"`）。

### 3.6 空状态

```css
.empty-state { display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; }
.empty-state > span {
  display: grid; place-items: center;
  width: 58px; height: 58px; border-radius: 18px;
  color: #4384e8; background: #eaf2ff; font-size: 23px;
}
.empty-state h4 { margin: 16px 0 4px; }
.empty-state p  { max-width: 460px; margin: 0 0 20px; color: var(--muted); line-height: 1.65; }
.empty-state.compact { min-height: 220px; }
```

图标色块 + 标题 + 一句说明 + 行动按钮。**文案随登录态变化**（「尚未登录」vs「还没有归档记录」），而不是一句通用的「暂无数据」。

### 3.7 排行榜 / 进度条

排行项结构：`名次徽章 · 头像 · 昵称+QQ号+细进度条 · 右侧数值+明细`。
进度条宽度按 `当前值 / 最大值 × 100%` 内联计算，前三名的名次徽章有专属配色（`.rank-1/2/3`）。

---

## 四、响应式

断点：**1050px**（平板）/ **767px**（手机）/ **390px**（小屏手机）

移动端形态变化：

| | 桌面 | 移动 |
|---|---|---|
| 导航 | 左侧固定栏 248px | 底部 Tab 栏，5 等分 |
| 统计卡 | 横向（图标在左） | 纵向（图标在上），min-height 145px |
| Hero | 横向，按钮在右 | 纵向，按钮换行 |
| 内容 padding | `28px 32px 48px` | `18px 16px calc(86px + safe-area)` |

```css
.mobile-navigation {
  position: fixed; inset: auto 0 0; z-index: 30;
  display: grid; grid-template-columns: repeat(5, minmax(0,1fr));
  padding: 6px 8px max(6px, env(safe-area-inset-bottom));
  background: color-mix(in srgb, var(--surface-solid) 94%, transparent);
  border-top: 1px solid var(--border);
  backdrop-filter: blur(18px);
}
.mobile-navigation a {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 4px; min-height: 50px; border-radius: 8px;
  font-size: 10px; font-weight: 600; color: var(--muted);
}
.mobile-navigation a i { font-size: 19px; }
.mobile-navigation a.router-link-active {
  color: var(--primary);
  background: color-mix(in srgb, var(--primary) 10%, transparent);
}
```

**6 个导航项塞进 5 格的做法**：常用 4 个直接放（概览/归档/媒体/任务），剩下的（联系人/设置）收进第 5 格「更多」，点击从底部弹出 Drawer，Drawer 里每项是「图标块 + 标题 + 副标题 + 右箭头」的列表样式。

`env(safe-area-inset-*)` 到处都用，刘海屏和 Home 指示条都照顾到了。

---

## 五、动效

```css
.native-page-enter-active, .native-page-leave-active { transition: opacity .14s ease, transform .14s ease; }
.native-page-enter-from { opacity: 0; transform: translateY(5px); }
.native-page-leave-to   { position: absolute; pointer-events: none; opacity: 0; transform: translateY(-3px); }

/* 移动端更快、改为横向位移 */
@media (max-width: 767px) {
  .native-page-enter-active, .native-page-leave-active { transition-duration: .12s; }
  .native-page-enter-from { transform: translateX(8px); }
  .native-page-leave-to   { transform: translateX(-5px); }
}
```

整体时长表：页面切换 `.12–.14s`、导航项 `.18s`、侧栏宽度 `.2s`、图标淡入淡出 `.15s`。
**全部在 0.2s 以内**——这是"感觉像原生应用"而非"网页"的关键。离场元素设 `position:absolute` 以免撑开布局。

---

## 六、值得抄的设计决策

1. **双行标题（英文 kicker + 中文正题）** 贯穿顶栏和每个区块标题，几乎零成本地拉出层次感和"产品感"。
2. **单主色**。全局只有一个蓝 `#2878ff`，语义色只在小图标块上出现，界面不会花。
3. **阴影极轻且带色调**。`rgba(30,55,90,.07)` 是偏蓝的深色而非纯黑，浅灰蓝底上显得干净。
4. **圆角分级**：大面板 20px → 卡片 18px → 统计卡/图标块 16/13px → 导航项 12px → 小徽章 8px。尺寸越小圆角越小，比例才稳。
5. **半透明 + backdrop-filter** 用在顶栏和移动端底栏，滚动时有层次但不喧宾夺主。
6. **同一控件承载多意图**（账号 chip hover 变退出按钮），省空间又不藏功能。
7. **空状态文案随上下文变化**，而不是千篇一律的「暂无数据」。
8. **折叠侧栏时图标位置不位移**——固定 20px 图标宽 + 隐藏文字 + 居中，视觉上很稳。

---

## 七、迁到你的百度归档工具

页面可以直接对应：

| QzoneArchive | 你的项目 |
|---|---|
| 概览 Dashboard | 总数 / 已归档 / 失败 / 资源数 四张统计卡 + 年份分布 |
| 归档内容 Archives | 文章列表，搜索 + 复选框 + 批量导出 |
| 媒体 Media | 图片时光轴（按年份） |
| 任务 Tasks | 抓取进度、暂停/继续 |
| 回收站 RecycleBin | **异常跳过列表**（逐条/批量重试） |
| 设置 Settings | Cookie 管理、并发与延迟、数据目录 |

统计卡的四个色调也能直接映射：
`blue` 总数 · `green` 已完成 · `orange` 警告 · `danger` 失败。

**如果用 Python 做 GUI**：这套设计是纯 CSS + 语义化 HTML，PrimeVue 只提供了 Button/Dialog/Drawer/Popover/ProgressBar/Tag 这几个基础件。用 pywebview 或 FastAPI + 原生 HTML 完全能复刻，不必引入 Vue —— 上面的 CSS 块可以原样使用。
