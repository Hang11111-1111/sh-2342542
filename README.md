# 花屿观澜里 · 3km 水果店云端监控

每 10 分钟自动抓取「杭州萧山·花屿观澜里附近 3km 内水果店」的店铺与 SKU，对比上一轮识别
**价格涨跌 / 上架下架 / 新店开张**，通过 **pushplus** 推送到你的微信。

整套跑在 **GitHub Actions 云端**，不依赖你本机是否开机、WorkBuddy 是否运行。

---

## 工作原理

```
┌─────────────────────────────────────────────────────────────┐
│  GitHub Actions (ubuntu  runner, 每10分钟触发)                │
│   1. 自起无头 Chromium                                       │
│   2. 注入 ELEME_COOKIES（你的饿了么登录态）                  │
│   3. 打开 h5.ele.me 水果搜索页(已定位花屿观澜里)             │
│   4. 滚动分页抓取 3km 内水果店 + SKU                         │
│   5. 与历史 baseline 对比变动                                │
│   6. pushplus 推送微信  ──►  你的微信                        │
│   7. 把新 baseline 提交回仓库(供下一轮对比)                  │
└─────────────────────────────────────────────────────────────┘
```

- 3km 判定：**优先用页面真实距离 `distM ≤ 3000m`**；`distM` 缺失时退化用 `ETA ≤ 20min` 代理。
- 只监控真水果店（便利店/超市/酸奶店等已排除）。
- 标题代号 `【SG-F3K】`，一眼可辨。

---

## 部署步骤

### 1. 建仓库（建议设为 **Public**）
> ⚠️ GitHub Actions 配额：Private 仓库每月仅 **2000 分钟**免费，而每 10 分钟一次 ≈ 每月 4000+ 分钟，
> 会超额。设为 **Public** 可享**无限免费** Actions；机密都在 Secrets 里，不会随公开代码泄露。
> 若必须 Private，要么接受按量计费，要么把频率调到 15/30 分钟。

在你 GitHub 新建一个空仓库（如 `fruit-monitor-3km`），然后：

### 2. 推送本仓库
```bash
cd fruit-monitor-cloud
git init
git add .
git commit -m "init: 3km fruit monitor on GitHub Actions"
git remote add origin https://github.com/<你的用户名>/fruit-monitor-3km.git
git push -u origin main
```

### 3. 配置两个 Secrets
仓库 `Settings → Secrets and variables → Actions → New repository secret`：

| Name | 内容 |
|------|------|
| `PUSHPLUS_TOKEN` | pushplus 的 token（你已有一个：`a4b4bacfd0544983b604f36539450211`，一般无需改） |
| `ELEME_COOKIES`  | 饿了么登录 cookie 的 **JSON 数组全文**（见下） |

### 4. 手动触发验证
`Actions → 水果店3km监控·每10分钟 → Run workflow`，看日志是否 `PUSH OK`，微信是否收到 `【SG-F3K】`。

---

## 如何获取 / 刷新 `ELEME_COOKIES`

cookie 含登录态，**会过期**（几小时到几天不等）。云端抓不到/跳登录时，重抽一次并更新 Secret。

**方法（在 Mac 本地、且监控 Chrome 9222 在线时）**：
```bash
python extract_cookies.py        # 生成 eleme_cookies.json
```
把 `eleme_cookies.json` 的**完整内容**复制进 GitHub Secret `ELEME_COOKIES` 即可。

（若 9222 没开，也可在浏览器登录 h5.ele.me 后，用开发者工具 Application → Cookies 手动复制，
整理成 `[{"name":..,"value":..,"domain":..,"path":..,"expires":..}, ...]` 的 JSON。）

---

## 本地运行（可选，调试用）
```bash
# 本地模式: 复用本机已登录的 Chrome(9222), 不注入 cookie
python fruit_monitor_core.py

# 云端模式模拟(需先有 eleme_cookies.json):
HEADLESS=1 ELEME_COOKIES="$(cat eleme_cookies.json)" \
  PUSHPLUS_TOKEN=xxxx python fruit_monitor_core.py
```

---

## 注意事项
- **cron 是 UTC 时间**：`*/10 * * * *` 即每 10 分钟一次（与北京时间差 8 小时，但间隔仍是 10 分钟）。
- **cookie 过期**是云端方案唯一的软肋：过期后推送会变成「需过码/抓取异常」或空结果，按上文刷新 Secret 即可恢复。
- 饿了么对自动化抓取有风控（图形验证码）。脚本遇到验证码会**等待人工过码**（云端无头环境无法自动过），
  此时会推送 `【SG-F3K】CAP 需人工过码`；你需要回到本地浏览器过一次码并重抽 cookie。
- 全量抓取存档 `history/fruit_full_*.json` 不入库（体积大）；仅 `history/fruit_baseline.json`
  （变动对比基准）每轮提交回仓库，保证跨次「变动检测」连续。
