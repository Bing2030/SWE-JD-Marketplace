# SWE-JD-Marketplace

高级软件工程师职位描述（JD）数据集 + 在线检索站。

**在线浏览：<https://bing2030.github.io/SWE-JD-Marketplace/>**

## 是什么

2026 年 10 月快照的三批软件工程师 JD，统一 schema 合并为单文件 [`data/jobs.json`](data/jobs.json)，配零构建静态检索站（中文 UI、全文搜索、多维筛选、统计面板）。仅供研究学习。

## 数据来源与规模

| 源 | 读取 | 保留 | 说明 |
|---|---:|---:|---|
| A · LinkedIn | 867 | 867 | 中国 543 / 新加坡 149 / 美国 175，senior+ 为主；附岗位分析备注（notes） |
| B · 中国多平台 | 427 | 311 | jobui 127 / Boss直聘 124 / 美团 59 / 猎聘 1；116 条与 C 重叠被吸收进官网记录 |
| C · 中国官网直投 | 1062 | 984 | 字节 381 / 阿里 217 / 腾讯 133 / 美团 114 / 飞书 49 / 小红书 48 / JD 31 / DeepSeek 6 / Moka 5；78 条同文重复合并 |
| **合计** | 2356 | **2162** | 去重规则见下 |

**字段覆盖（实测）**：description 2162（100%）· notes 867（仅 A 源分析备注）· experience 599 · salary 212（C 源官网薪资全空，未虚构）· skills 234（仅 B 源）· 双源条目 62 · 收录异标题 14。

**关键分布**：is_senior 是 1280 / 否 307 / 未知 575；地区 中国 1835 / 新加坡 152 / 美国 175；AI 相关（关键词规则）1352。

## Schema（v1.0）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | `<platform>:<原生id>`，全库唯一 |
| `title` / `company` | string | 职位名 / 公司 |
| `level` | enum | `campus/entry/mid/senior/lead/staff/architect/manager/principal/director/cto/unknown` |
| `level_inferred` | bool | true = 由标题关键词推导（C 源无级别列） |
| `is_senior` | bool\|null | level ∈ {senior…cto} → true；{campus,entry,mid} → false；unknown → null |
| `role` | enum | `ai_agent/ai_platform/architecture/backend/management/payment_risk/other` + `role_inferred` |
| `region` | enum | `cn / sg / us` |
| `cities[]` | string[] | 归一化城市数组（多城市发布拆多元素）；原文见 `location_raw` |
| `workplace` | enum | `onsite / hybrid / remote / unknown`（仅 A 源标注） |
| `salary` | object\|null | `{text, currency, period(monthly/annual/null), min, max, months, months_est, annual_min, annual_max}`；**月薪不换算年包**，period 显式声明 |
| `experience` | string\|null | 原文；解析值 `years_min` / `years_max` |
| `education` | string\|null | 原文 |
| `skills[]` | string[] | B 源 must_have_skills 拆分 |
| `description` | string | JD 全文（A 原生 / B 按【职责】【要求】【加分】拼装 / C jd_text） |
| `notes` | string | 岗位分析备注（仅 A 源 867 条）；内部采集元数据已剔除 |
| `alt_titles[]` | string[] | C 源同文合并时收集的异标题 |
| `ai_related` | bool | 标题+技能+正文关键词规则（agent/智能体/LLM/大模型/RAG/MCP/多模态…） |
| `sources[]` | array | `[{platform, url, collected_at}]` 多源数组，跨源合并条目天然带双链接 |
| `applicants` | int\|null | 仅 A 源；`collected_at` 全源有值 |

## 去重（确定性，可复现）

1. **C 源内部**：`md5(jd_text)` 分组 → 78 条同文记录合并（多城市/多址发布）：cities 取并集、alt_titles 收集、sources 追加；
2. **B∩C 跨源**：`归一(company)+归一(title)` 精确匹配 → 116 条 B 记录吸收进 C 主记录（B 的 url 并入 sources[]）。不做模糊匹配。

个人筛选标记（is_target / keep / filter_reason / senior_fit 等）与内部采集备注已剔除；构建脚本含 banned 字段自检。

## 重建数据

```bash
python3 scripts/build_dataset.py --config scripts/sources.local.json -o data/jobs.json
```

`scripts/sources.local.json` 指向本地三源原始文件（gitignore，模板见 `scripts/sources.example.json`）。原始抓取数据因平台服务条款**不随仓库发布**，流程为“本地跑脚本 → 提交产物”。脚本仅用 Python 标准库，输出确定性。

## 在线站点

零构建纯静态：vendored [MiniSearch](https://github.com/lucaong/minisearch) + 自定义 tokenizer（ASCII 按词 + CJK 滑窗 bigram + 单字前缀兜底，`java` 不误中 `javascript`）、7 组 facets（组内 OR、组间 AND、带计数）、行内展开详情、50 条分页、`location.hash` 状态回放、深色模式自适应。

## 许可

- **数据**：[CC BY 4.0](DATA_LICENSE.md)（各条 JD 原文版权归原发布平台与招聘方）
- **代码**：[MIT](LICENSE)

## 免责声明

- 数据为 **2026-10 快照**，仅供研究学习，不代表任何平台的当前招聘状态；
- JD 内容版权归原发布方，每条记录保留原始链接以便溯源；
- 未收录联系方式等个人信息；
- `notes` 为整理时的个人分析备注（仅 LinkedIn 源），观点性内容仅供参考；
- 如认为某条内容不应收录，请开 issue，将及时处理。
