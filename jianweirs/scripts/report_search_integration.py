"""Build a local, credential-free summary of the saved live search acceptance."""
from html import escape
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "wechat-integration-20261022"


def read(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def main():
    final = read("portal-acceptance.json")["tests"][0]
    previous = read("portal-acceptance-broad-keywords.json")["tests"]
    current = read("current-platform-verification.json")
    policy = read("wechat-policy-final.json")
    channel = final["channels"]["wechat"]
    article = next(s for s in final["source_metadata"] if s["source_type"] == "wechat")
    timings = "".join(f"<tr><td>{escape(step['tool'])}</td><td>{step['seconds']:.3f} 秒</td></tr>" for step in channel["timings"])
    trials = "".join(f"<tr><td>{escape(t['name'])}</td><td>{t['seconds']:.3f} 秒</td><td>{escape(str(t['source_counts']))}</td><td>{escape(t['status'])}</td></tr>" for t in previous + [final])
    summary = {
        "local_platform": current["local_platform"], "web": "DeepSeek / DMX", "academic": "Metaso scholar",
        "wechat_mcp_status": current["wechat_health"]["status"], "final_question_seconds": final["seconds"],
        "source_counts": final["source_counts"], "cited_source_types": final["cited_source_types"],
        "wechat_seconds": channel["latency_seconds"], "wechat_tools": channel["timings"],
        "snapshot_status": final["snapshots"], "final_policy_query_status": policy["status"],
        "verification": {"prior_regression_passed": 475, "final_focused_passed": 44, "frontend_passed": 49,
                         "browser_checked": ["source footer", "citation opens hidden card in current tab", "saved article", "MCP connection control"]},
        "deployment_scope": "Local Windows portal at port 3080; the remote WeChat MCP service was consumed, not redeployed."
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>无隅 AI 三路搜索验收 · 2026-10-22</title>
<style>body{{font:16px/1.8 'Microsoft YaHei',sans-serif;color:#17232c;background:#f5f7f9;margin:0}}main{{max-width:1040px;margin:30px auto;padding:32px;background:white;border-radius:14px}}h1{{font-size:28px}}h2{{margin-top:30px;font-size:21px}}table{{width:100%;border-collapse:collapse;margin:14px 0}}th,td{{border:1px solid #dce2e5;text-align:left;padding:10px}}th{{background:#eff5f3}}.ok{{color:#087e65;font-weight:bold}}.note{{color:#52626d}}img{{width:100%;border:1px solid #ddd}}a{{color:#087e65}}code{{word-break:break-all}}@media(max-width:600px){{main{{margin:0;padding:18px}}table{{font-size:13px}}}}</style>
<main><h1>无隅 AI：DeepSeek、秘塔与微信公众号验收</h1><p class="ok">本机平台已更新并启动：<a href="{escape(current['local_platform'])}">{escape(current['local_platform'])}</a></p>
<p>2026-10-22 完成真实联网请求、独立测试账户完整问答与浏览器操作验收。正式入口保留原有账号、模型及任务记录；真实问答使用隔离实例验证相同代码与检索配置。本轮没有部署远端产品容器。</p>
<h2>当前配置</h2><table><tr><th>来源</th><th>实际调用</th><th>结果</th></tr><tr><td>网页</td><td>DeepSeek（DMX /v1/tools/search）</td><td>已配置、实际返回网页</td></tr><tr><td>学术</td><td>秘塔 /api/v1/search，scope=scholar</td><td>实际返回 scholars 列表</td></tr><tr><td>微信公众号</td><td>MCP initialize → tools/list → search → get_article</td><td>{escape(current['wechat_health']['status'])}</td></tr></table>
<p>公众号默认近两年、最多5条结果、最多3页索引，立即读取前2篇（实际不足2篇时按实际数量）。不同渠道同步检索，公众号内部串行调用；服务冷却时不会快速重试。</p>
<h2>最终完整问答</h2><p>问题：结合污水处理论文和公众号文章，介绍膜生物反应器 MBR 的优势与限制。</p>
<p><strong>{final['seconds']:.3f} 秒完成</strong>，取得网页 {final['source_counts']['webpage']} 条、学术 {final['source_counts']['academic']} 条、公众号 {final['source_counts']['wechat']} 篇，回答实际引用了三类来源。公众号阶段总计 <strong>{channel['latency_seconds']:.3f} 秒</strong>，含握手、搜索、正文读取及连接开销。</p>
<table><tr><th>公众号步骤</th><th>实际耗时</th></tr>{timings}</table>
<p>实际文章：{escape(article['title'])}；公众号：{escape(article['account'])}；正文日期：{escape(article['published_at'])}。已读取并保存 {article['content_chars']} 字符，任务内保存内容接口返回 HTTP 200。</p>
<h2>重复测试记录</h2><table><tr><th>问题</th><th>完整耗时</th><th>各类来源数量</th><th>运行状态</th></tr>{trials}</table>
<p class="note">前两行使用宽泛关键词，最后一行已按“膜生物反应器”进行精确检索；数量差异同时受实时索引影响，不能视为稳定的质量提升百分比。最终“危险废物贮存”搜索为 {escape(policy['status'])}，耗时 {policy['latency_seconds']:.3f} 秒，未捏造公众号来源。</p>
<h2>页面与内容处理</h2><p>回答底部显示来源编号、渠道、标题、公众号、日期、是否用于回答、正文读取状态及“查看已保存内容”。点击回答中的引用会在当前页定位来源，并展开隐藏条目。正文归属当前任务，其他账号不能读取。外部引用过期不影响已保存正文。</p>
<p>实测发现索引与正文日期可能跨年不一致；显示正文日期并保留冲突提示。来源仍是外部资料：已读取不代表事实已验证；公众号不自动成为官方政策依据；仅取得摘要或正文片段时保留该限制。精确主题检索避免将宽泛行业广告用于技术论证。</p>
<img alt="回答底部三类来源展示" src="../../output/playwright/wuyu-three-source-footer.png">
<h2>验证与原始记录</h2><p>此前475项 Python 回归检查通过；最终相关44项 Python 检查通过；49项前端检查通过。另验证了真实 MCP 连接、学术原生接口、完整问答、引用定位、正文保存页面及账号隔离。</p>
<ul><li><a href="portal-acceptance.json">最终门户验收 JSON</a></li><li><a href="current-platform-verification.json">正式本机入口与连接检查</a></li><li><a href="mcp-search-live.json">首次 MCP 搜索和正文测速</a></li><li><a href="wechat-query-specificity.json">关键词相关性对照</a></li><li><a href="wechat-policy-final.json">公众号空结果复测</a></li><li><a href="summary.json">简要机器可读结果</a></li></ul>
<p class="note">这些是本次少量真实请求的实测值，未进行并发容量测试，也未验证全部回答的专业准确率。完整回答耗时包含主模型综合过程，不能等同于搜索服务耗时。</p></main></html>"""
    path = OUT / "验收结果.html"
    path.write_text(html, encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
