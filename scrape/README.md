# 大观园题库抓取数据集

> 抓取时间：2026-10-12 · 来源 API：`GET /api/questions`（无认证，per_page=200 分页）
> 覆盖：完整题库 5308 + 历年真题 1335 + 模拟卷 724，去重后 **6419 道不重复题目**

## 文件

| 文件 | 题单 | 题数 | 大小 |
|---|---|---|---|
| `complete.json` | 完整题库（高数+线代+概率知识树） | 5308 | 16.1 MB |
| `exam.json` | 历年真题 | 1335 | 4.1 MB |
| `mock.json` | 模拟卷 | 724 | 2.5 MB |
| `manifest.json` / `integrity-report.json` | 抓取元数据与校验 | — | — |

## 数据完整性

- 三个题单 fetched == API total，无缺失、无重复 ID
- **每道题都含 correct_answer 与 answer_explanation**（100%）
- 跨题单重叠：真题∩完整题库 907 道、模拟卷∩完整题库 38 道、真题∩模拟卷 5 道

## 题型分布

| 题单 | 分布 |
|---|---|
| complete | {"subjective": 2632, "single_choice": 1543, "essay": 589, "fill": 541, "multiple_choice": 3} |
| exam | {"subjective": 642, "single_choice": 445, "essay": 127, "fill": 121} |
| mock | {"single_choice": 318, "fill": 213, "subjective": 107, "essay": 86} |

## 来源标签 TOP（complete）

- 0.88k: 958 题
- 0.9k: 701 题
- 1k: 642 题
- 0.66k: 526 题
- 姜晓千真题同源150: 219 题
- 竞赛题: 99 题
- 姜晓千强化例题: 87 题
- 张宇30讲: 77 题

## 题目 JSON 结构（QuestionDocument v1）

```json
{
 "id": 3356,
 "category_id": 331,
 "serial_number": 1,
 "question_type": "subjective",
 "source": "1988年·数一二",
 "source_tags": [
  "1988年真题"
 ]
}
```

字段说明：
- `stem` / `document.stem_md`：题干（LaTeX 数学公式）
- `option_a..d` / `document.options[]`：选项
- `correct_answer` / `answer.reference_answer_md`：参考答案；`answer.option_ids`：选择题答案
- `answer_explanation` / `document.explanation_md`：完整解析
- `category_full_path`：最多五级分类路径（如 高等数学 / 极限 / 极限计算 / 函数极限 / 含变限积分的极限）
- `source_tags`：来源标签（真题年份 / 660 / 880 / 1000 等题册）
- `content_hash`：内容去重指纹
- `document.asset_refs[]`：图片资源（content_sha256 寻址，可经 `/api/v1/question-assets/{hash}` 获取，本次未抓取）
