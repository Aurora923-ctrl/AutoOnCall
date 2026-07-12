from __future__ import annotations

import html
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_SVG = ROOT / "docs" / "架构图" / "自动值守诊断流程图.svg"
OUT_HTML = ROOT / "docs" / "架构图" / "自动值守诊断流程图.html"
OUT_PNG = ROOT / "docs" / "架构图" / "自动值守诊断流程图.png"


W = 2400
H = 1500


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def text_block(
    x: int,
    y: int,
    lines: list[str],
    *,
    size: int = 30,
    weight: int = 500,
    fill: str = "#1f2937",
    gap: int = 42,
) -> str:
    out = []
    for index, line in enumerate(lines):
        out.append(
            f'<text x="{x}" y="{y + index * gap}" font-size="{size}" '
            f'font-weight="{weight}" fill="{fill}">{esc(line)}</text>'
        )
    return "\n".join(out)


def card(
    x: int,
    y: int,
    w: int,
    h: int,
    title: str,
    lines: list[str],
    *,
    accent: str,
    fill: str = "#ffffff",
    title_size: int = 34,
    body_size: int = 27,
    line_gap: int = 38,
) -> str:
    body_y = y + 96
    return f"""
<g>
  <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="26" fill="{fill}" stroke="#d6dee8" stroke-width="2"/>
  <rect x="{x}" y="{y}" width="10" height="{h}" rx="5" fill="{accent}"/>
  <circle cx="{x + 40}" cy="{y + 46}" r="14" fill="{accent}" opacity="0.16"/>
  <text x="{x + 66}" y="{y + 58}" font-size="{title_size}" font-weight="750" fill="{accent}">{esc(title)}</text>
  {text_block(x + 36, body_y, lines, size=body_size, fill="#263244", gap=line_gap)}
</g>"""


def lane(
    x: int,
    y: int,
    w: int,
    h: int,
    title: str,
    lines: list[str],
    *,
    accent: str,
    tag: str,
    fill: str,
) -> str:
    return f"""
<g>
  <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="28" fill="{fill}" stroke="#d6dee8" stroke-width="2"/>
  <text x="{x + 34}" y="{y + 58}" font-size="34" font-weight="760" fill="{accent}">{esc(title)}</text>
  {text_block(x + 34, y + 112, lines, size=27, fill="#334155", gap=39)}
  <rect x="{x + 34}" y="{y + h - 58}" width="{len(tag) * 26 + 44}" height="38" rx="19" fill="#ffffff" stroke="{accent}" stroke-width="2" opacity="0.96"/>
  <text x="{x + 56}" y="{y + h - 31}" font-size="22" font-weight="700" fill="{accent}">{esc(tag)}</text>
</g>"""


def arrow(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    color: str = "#64748b",
    width: int = 4,
    dashed: bool = False,
    label: str | None = None,
    label_x: int | None = None,
    label_y: int | None = None,
) -> str:
    dash = ' stroke-dasharray="12 12"' if dashed else ""
    label_svg = ""
    if label:
        lx = label_x if label_x is not None else (x1 + x2) // 2
        ly = label_y if label_y is not None else (y1 + y2) // 2 - 12
        label_svg = (
            f'<rect x="{lx - 14}" y="{ly - 30}" width="{len(label) * 24 + 28}" height="42" '
            f'rx="21" fill="#ffffff" stroke="{color}" stroke-width="1.5"/>'
            f'<text x="{lx}" y="{ly}" font-size="22" font-weight="700" fill="{color}">{esc(label)}</text>'
        )
    return f"""
<g>
  <path d="M {x1} {y1} L {x2} {y2}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linecap="round"{dash} marker-end="url(#{color_to_marker(color)})"/>
  {label_svg}
</g>"""


def elbow(
    points: list[tuple[int, int]],
    *,
    color: str = "#64748b",
    width: int = 4,
    dashed: bool = False,
    label: str | None = None,
    label_pos: tuple[int, int] | None = None,
) -> str:
    d_attr = "M " + " L ".join(f"{x} {y}" for x, y in points)
    dash = ' stroke-dasharray="12 12"' if dashed else ""
    label_svg = ""
    if label and label_pos:
        lx, ly = label_pos
        label_svg = (
            f'<rect x="{lx - 16}" y="{ly - 31}" width="{len(label) * 24 + 34}" height="42" '
            f'rx="21" fill="#ffffff" stroke="{color}" stroke-width="1.5"/>'
            f'<text x="{lx}" y="{ly}" font-size="22" font-weight="700" fill="{color}">{esc(label)}</text>'
        )
    return f"""
<g>
  <path d="{d_attr}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"{dash} marker-end="url(#{color_to_marker(color)})"/>
  {label_svg}
</g>"""


def color_to_marker(color: str) -> str:
    return {
        "#2563eb": "arrow-blue",
        "#059669": "arrow-green",
        "#d97706": "arrow-amber",
        "#dc2626": "arrow-red",
        "#7c3aed": "arrow-purple",
        "#64748b": "arrow-slate",
    }.get(color, "arrow-slate")


def marker_defs() -> str:
    markers = {
        "arrow-blue": "#2563eb",
        "arrow-green": "#059669",
        "arrow-amber": "#d97706",
        "arrow-red": "#dc2626",
        "arrow-purple": "#7c3aed",
        "arrow-slate": "#64748b",
    }
    return "\n".join(f"""
<marker id="{name}" markerWidth="14" markerHeight="14" refX="10" refY="5" orient="auto" markerUnits="strokeWidth">
  <path d="M 0 0 L 10 5 L 0 10 z" fill="{color}"/>
</marker>""" for name, color in markers.items())


def build_svg() -> str:
    main_cards = [
        (80, 250, 310, 190, "告警接入", ["告警管理器", "回调输入", "指纹去重"], "#2563eb"),
        (
            450,
            250,
            310,
            190,
            "接口 / 事件流",
            ["FastAPI 路由", "/api/alerts / aiops", "流式诊断事件"],
            "#7c3aed",
        ),
        (
            820,
            250,
            310,
            190,
            "计划器",
            ["运行手册检索", "模型结构化计划", "计划步骤队列"],
            "#059669",
        ),
        (1190, 250, 310, 190, "执行器", ["工具注册表", "风险门禁", "只读工具取证"], "#d97706"),
        (1560, 250, 310, 190, "证据", ["工具调用记录", "证据矩阵", "轨迹可回放"], "#2563eb"),
        (
            1930,
            250,
            390,
            190,
            "重规划器",
            ["证据分析器", "补查 / 审批 / 报告", "证据不足则降级"],
            "#dc2626",
        ),
    ]
    cards_svg = "\n".join(card(*item[:6], accent=item[6]) for item in main_cards)
    arrows_svg = "\n".join(arrow(x, 345, x + 60, 345) for x in [390, 760, 1130, 1500, 1870])

    outcomes = "\n".join(
        [
            card(
                120,
                1110,
                430,
                210,
                "继续补查 / 降级",
                ["缺指标、缺日志、缺运行手册", "追加只读步骤或重试", "不完整 / 已降级"],
                accent="#dc2626",
                fill="#fffafa",
            ),
            card(
                650,
                1110,
                430,
                210,
                "人工审批",
                ["中高风险动作暂停", "生成审批请求", "审批后仍不自动写生产"],
                accent="#d97706",
                fill="#fffdf5",
            ),
            card(
                1180,
                1110,
                430,
                210,
                "报告与轨迹",
                ["根因、影响范围、关键证据", "证据回链", "可复盘诊断时间线"],
                accent="#2563eb",
                fill="#f8fbff",
            ),
            card(
                1710,
                1110,
                430,
                210,
                "离线评测",
                ["智能运维黄金用例", "检索增强评测 / RAGAS", "回归门禁防退化"],
                accent="#059669",
                fill="#f7fffb",
            ),
        ]
    )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<defs>
  {marker_defs()}
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="#f8fafc"/>
    <stop offset="58%" stop-color="#f3f7fb"/>
    <stop offset="100%" stop-color="#eef6f2"/>
  </linearGradient>
  <filter id="softShadow" x="-20%" y="-20%" width="140%" height="140%">
    <feDropShadow dx="0" dy="12" stdDeviation="12" flood-color="#1f2937" flood-opacity="0.10"/>
  </filter>
</defs>
<rect width="{W}" height="{H}" fill="url(#bg)"/>
<g opacity="0.55">
  <path d="M0 190 H2400" stroke="#e2e8f0" stroke-width="2"/>
  <path d="M0 1010 H2400" stroke="#e2e8f0" stroke-width="2"/>
</g>

<text x="80" y="88" font-size="62" font-weight="820" fill="#0f172a">AutoOnCall 诊断闭环流程图</text>
<text x="82" y="140" font-size="30" font-weight="500" fill="#64748b">从告警到诊断报告：LLM 只负责计划与表达，后端用工具契约、证据链、审批和评测约束行为</text>
<rect x="1810" y="58" width="510" height="70" rx="35" fill="#ffffff" stroke="#d6dee8" stroke-width="2"/>
<text x="1844" y="103" font-size="26" font-weight="760" fill="#334155">核心主线：智能运维智能体 + 检索证据</text>

<g filter="url(#softShadow)">
{cards_svg}
</g>
{arrows_svg}

<g filter="url(#softShadow)">
{lane(120, 560, 980, 270, "检索增强 / 运行手册知识链路", ["文档上传：Markdown / PDF / HTML / CSV / XLSX", "结构化切分 -> 向量化 -> Milvus + 词法检索", "重排序 + 可信门控 + 引用保护", "无可信来源时拒答"], accent="#059669", tag="为计划器和执行器提供可引用知识依据", fill="#f7fffb")}
{lane(1300, 560, 980, 250, "外部系统适配器 / 工具层", ["Prometheus 指标、Loki / 日志网关、调用链、Kubernetes", "Redis、MySQL、配置管理库、发布历史、历史工单", "工具输出统一归一化为证据，失败也写入报告"], accent="#d97706", tag="不是模型自由操作生产", fill="#fffdf5")}
</g>

{elbow([(970, 560), (970, 500), (1345, 500), (1345, 442)], color="#059669", width=5, label="运行手册命中", label_pos=(1020, 490))}
{elbow([(1790, 560), (1790, 500), (1345, 500), (1345, 442)], color="#d97706", width=5, label="工具取证", label_pos=(1650, 490))}

<g filter="url(#softShadow)">
  <path d="M 1200 900 L 1380 1005 L 1200 1110 L 1020 1005 Z" fill="#ffffff" stroke="#cbd5e1" stroke-width="3"/>
  <text x="1108" y="988" font-size="31" font-weight="780" fill="#0f172a">证据是否充分？</text>
  <text x="1094" y="1033" font-size="31" font-weight="780" fill="#0f172a">动作是否高风险？</text>
</g>

{elbow([(2125, 440), (2125, 510), (1200, 510), (1200, 900)], color="#dc2626", width=5, label="决策门", label_pos=(1230, 510))}

<g filter="url(#softShadow)">
{outcomes}
</g>

{elbow([(1045, 1005), (335, 1005), (335, 1110)], color="#dc2626", width=5, label="缺证据", label_pos=(670, 995))}
{elbow([(1158, 1085), (865, 1085), (865, 1110)], color="#d97706", width=5, label="需审批", label_pos=(930, 1075))}
{elbow([(1242, 1085), (1395, 1085), (1395, 1110)], color="#2563eb", width=5, label="可报告", label_pos=(1280, 1075))}
{elbow([(1355, 1005), (1925, 1005), (1925, 1110)], color="#059669", width=5, label="回归验证", label_pos=(1660, 995))}

<rect x="120" y="1375" width="2160" height="66" rx="28" fill="#ffffff" stroke="#d6dee8" stroke-width="2"/>
<text x="158" y="1418" font-size="28" font-weight="650" fill="#334155">面试表达：模型不直接改生产；所有结论必须回链证据或引用；中高风险只进入审批、演练、沙箱或人工记录。</text>
</svg>"""


def write_files() -> None:
    svg = build_svg()
    OUT_SVG.parent.mkdir(parents=True, exist_ok=True)
    OUT_SVG.write_text(svg, encoding="utf-8")
    OUT_HTML.write_text(
        f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>自动值守诊断流程图</title>
<style>html,body{{margin:0;background:#f8fafc}} svg{{display:block;width:100vw;height:auto}}</style></head>
<body>{svg}</body>
</html>""",
        encoding="utf-8",
    )
    print(OUT_SVG)
    print(OUT_HTML)
    print(OUT_PNG)


if __name__ == "__main__":
    write_files()
