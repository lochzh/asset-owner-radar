# 全球资产所有者情报雷达 · Global Asset Owner Radar

每天自动抓取主权基金、养老金、保险集团、央行储备等全球资产所有者的官方公告与披露，用 AI 筛选与中国投资相关的内容，生成中文摘要，发布到一个公开网站。完全免费、零服务器、零运维。

## 部署步骤（首次 15 分钟，之后全自动）

### 第 1 步：创建 GitHub 仓库

1. 注册或登录 [GitHub](https://github.com)
2. 点击右上角 `+` → `New repository`
3. 仓库名建议 `asset-owner-radar`（任意名字都可以）
4. 选择 **Public**（公开仓库才能免费用 GitHub Actions 无限分钟数 + GitHub Pages）
5. 不要勾选任何初始化选项，点击 `Create repository`

### 第 2 步：上传项目文件

最简单的方式：把本目录所有文件压缩上传。

- 在新仓库页面点击 `uploading an existing file` 链接
- 把这个项目里的所有文件（包括隐藏的 `.github` 文件夹）拖进去
- 在底部填提交信息 `initial commit`，点击 `Commit changes`

或者用命令行：

```bash
cd /path/to/asset-owner-radar
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/你的用户名/asset-owner-radar.git
git push -u origin main
```

### 第 3 步：配置 Anthropic API Key

1. 去 [console.anthropic.com](https://console.anthropic.com) 注册账号，新用户通常有免费额度
2. 在 `API Keys` 页面创建一个 key（形如 `sk-ant-...`），复制下来
3. 回到 GitHub 仓库，点 `Settings` → `Secrets and variables` → `Actions`
4. 点 `New repository secret`
5. Name 填 `ANTHROPIC_API_KEY`，Value 填刚才那个 key
6. 点 `Add secret`

预估成本：每天约 $0.10-0.30（每月 $3-9），新账户的免费额度通常够用很久。

### 第 4 步：开启 GitHub Pages

1. `Settings` → `Pages`
2. `Source` 选 `GitHub Actions`（不是 `Deploy from a branch`）
3. 保存

### 第 5 步：手动触发首次运行

1. 点仓库顶部的 `Actions` 标签
2. 左侧选 `Daily Harvest`
3. 右侧 `Run workflow` → `Run workflow` 按钮
4. 等 5-10 分钟，第一次抓取 + AI 处理需要这么久

### 第 6 步：访问网站

地址：`https://你的用户名.github.io/asset-owner-radar/`

可以分享给国寿资产任何同事，电脑手机都能打开。手机上点浏览器 "添加到主屏幕" 就变成 App 图标。

## 它每天都做什么

- 北京时间每天早 6:00 和晚 6:00 自动运行（在 `.github/workflows/harvest.yml` 修改 cron 表达式可以调整）
- 抓取 `data/sources.json` 里配置的 28 个机构源
- 增量去重（只处理新内容）
- 调 Claude Haiku 评估中国相关性 + 生成中文摘要
- 调 Claude Sonnet 生成每日精选段落
- 把结果写入 `public/data/latest.json`，自动重新部署网站

## 怎么扩充信息源

打开 `data/sources.json`，按现有格式加一条：

```json
{
  "name": "机构全名",
  "short": "缩写显示",
  "category": "sovereign / pension / insurance / central_bank 四选一",
  "type": "rss 或 page",
  "url": "RSS 地址 或 新闻列表页 URL",
  "is_firsthand": true,
  "country": "国家代码"
}
```

提交后下一次自动运行就会生效，不需要改任何代码。

建议陆续添加的源：ADIA、CalPERS、ABP、Manulife、Aviva 等。LinkedIn 公司页面也是高管表态的好来源，但需要 RSS 中间层（如 RSS.app），可作为下一步迭代。

## 不需要改代码就能调整的东西

`scripts/harvest.py` 顶部几个常量：

- `MAX_ITEMS_PER_SOURCE`（每源最多取多少条，默认 8）
- `LOOKBACK_DAYS`（往回看几天，默认 7）
- `MIN_SCORE_TO_KEEP`（最低保留分数，默认 5.0）
- `TOP_N_FINAL`（最终展示多少条，默认 60）

## 安全与合规

- 所有摘要都是 AI 重写，不复制原文，规避版权风险
- 每条都附"查看原文"链接，引导用户到一手来源
- 页脚已写明免责声明：不构成投资建议，仅供参考
- 仓库公开但 API Key 通过 GitHub Secrets 加密保存，不会泄露

## 故障排查

- **Actions 跑失败**：去 `Actions` 标签点失败那次，看红色那一步的日志。最常见原因是 API Key 没配或额度用完。
- **网站空白**：检查 Pages 是否开启、Source 是否选了 `GitHub Actions`。
- **某个源持续抓不到**：可能官网改版了。在 `sources.json` 里先注释或删掉，不影响其他源。

## 关于成本

这套方案在 GitHub 公开仓库下完全免费：
- GitHub Actions：公开仓库无限分钟数
- GitHub Pages：免费托管，全球 CDN
- 唯一花钱的是 Claude API，每月不到 10 美元。如果想完全零成本，把 `harvest.py` 里的 Anthropic 调用换成 Google Gemini 免费层（每天有免费配额）也可以。
