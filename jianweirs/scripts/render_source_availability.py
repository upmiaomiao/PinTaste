"""Render saved source checks into a portable Chinese HTML table, without probes."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path


LABELS = {"optical":"现有光学底图", "multispectral":"多光谱", "hyperspectral":"高光谱", "sar":"SAR"}
STATES = {"passed":"通过", "failed":"失败/暂不可用", "credentials_required":"需要账户或授权", "unsupported":"当前接口不支持",
          "unknown":"尚不能判定", "no_coverage":"指定条件未检出", "blocked":"依赖阻断"}
REASONS = {
    "decoded_optical_image":"已获取并完整解码真实PNG/JPEG",
    "actual_capture_returned":"实际取图接口成功",
    "current_mosaic_has_no_requested_date_selection":"现有底图取图接口不支持按指定历史日期选景",
    "omap_permission_denied":"奥维/当前图源返回权限拒绝（HTTP 403）",
    "omap_timeout":"奥维取图超时；不能据此判定永久无权限或无覆盖",
    "scene_footprints_intersect":"目录返回与样本范围相交的场景；未证明整个ROI无云且可用",
    "no_catalog_scenes_in_sample_period":"该代表点及2026年1—4月未返回场景",
    "regional_archive_intersection":"扩大到清单点位包络及历史档案后检出场景；不能推广到每个点位",
    "no_catalog_scenes_in_regional_archive":"区域历史条件下未返回场景；仅限本次检索条件",
    "decoded_geotiff_subset":"小范围GeoTIFF已下载并用栅格工具打开",
    "small_science_subset_downloaded":"已下载小范围科学栅格；不等同于固废识别已验证",
    "roi_crop_not_implemented_for_source":"现有适配器未实现该产品的小范围裁切",
    "provider_science_download_requires_user_account":"科学文件需要提供商账户；依据目录声明，本次未下载受保护文件",
    "sar_preview_auth_required":"真实预览请求被鉴权/跳转限制阻断",
    "source_catalog_query_not_implemented":"此来源尚未接入免凭据场景查询",
    "jaxa_registered_account_not_configured":"JAXA年际镶嵌下载需要注册账户，当前未配置",
    "regional_scene_not_verified_over_primary_point":"仅检出区域内场景，未确认其覆盖主代表点，未盲目裁切",
    "no_candidate_scene_to_test":"没有候选场景，科学文件能否下载尚未验证",
    "spectral_provider_timeout":"光谱提供商查询超时",
    "spectral_provider_unavailable":"光谱提供商响应或网络暂不可用",
    "spectral_provider_error":"光谱提供商返回错误；不是零覆盖结论",
    "bounded_probe_deadline_exceeded":"本次有界探测已超时，未无限重试",
    "actual_catalog_query_returned":"真实场景查询已返回有效结构",
    "omap_placeholder_grid":"完整解码后确认是缺瓦片占位网格，不属于有效遥感影像",
    "provider_notice_tile":"目视确认是重复二维码/图商提示牌，不是影像；具体图源故障原因尚未确认",
    "provider_query_failed_not_no_coverage":"查询失败导致覆盖未知；不记为无场景",
    "shared_desktop_dependency_unavailable":"共享奥维桌面连接不可用，未对每个依赖图源重复取图",
}


def text(value) -> str:
    return html.escape(str(value), quote=True)


def status(checks: list, capability: str) -> str:
    records=[item for item in checks if item["capability"]==capability]
    if not records:
        return "<span class='unknown'>尚未验证</span>"
    item=records[-1];state=item["state"]
    return f"<span class='{text(state)}'>{text(STATES.get(state,state))}</span>"


def render(source: Path, destination: Path) -> dict:
    data=json.loads(source.read_text(encoding="utf-8"));rows=data["sources"];profile=data["sample_profile"]
    counts={group:sum(row["group"]==group for row in rows) for group in LABELS}
    downloaded=sum(any(item["capability"]=="download" and item["state"]=="passed" for item in row["checks"]) for row in rows)
    sections=["<h1>成都代表点图源可用性实测</h1>",
              f"<p>核验时间：{text(data['checked_at'])}。共{text(len(rows))}个来源，真实下载成功{text(downloaded)}个来源。</p>",
              "<p>本表按清单中的代表点核验来源能力，<strong>不是36个点位逐点全覆盖核验</strong>。光学入口属于底图/供应商，不能把每个入口改称某颗卫星。拍摄日期、原生分辨率或坐标变换未提供时保持未知。</p>",
              "<p>原表第G/H列均写“中心经度”；本次按数值范围将H列作为候选纬度。原表坐标系及面积单位未声明，未改写原表，未据此计算面积。按WGS84数值请求API只核验接口，不证明影像已与图斑配准。</p>",
              "<h2>样本与检索范围</h2><ul>"]
    for sample in profile["samples"]:
        sections.append(f"<li>{text(sample['id'])} · 原表第{text(sample['row'])}行 · {text(sample['district'])} · 候选经纬度{text([sample['longitude'],sample['latitude']])} · 请求范围{text(sample['bbox'])}</li>")
    sections.extend([f"</ul><p>任务时段：{text(profile['time_range'])}。扩大检索包络：{text(profile['regional_bbox'])}。当样本时段无结果时才进行一次区域历史档案查询；区域命中不能证明主代表点或其他35点覆盖。</p>",
                     "<p><a href='source-results.json'>逐源JSON</a> · <a href='chengdu-samples.json'>样本来源JSON</a></p>",
                     "<div class='table'><table><thead><tr><th>组/来源</th><th>接口</th><th>目录覆盖</th><th>文件获取</th><th>裁切/预览</th><th>时间和空间证据</th><th>失败原因及处理办法</th><th>取图证据</th></tr></thead><tbody>"])
    for row in rows:
        checks=row["checks"];spec=row["catalog_metadata"]
        name=spec.get("name",row["source"])
        conditions=[]
        for item in checks:
            if item["capability"]=="coverage":
                conditions.append(f"{STATES.get(item['state'],item['state'])}：{item.get('time_range')}；bbox={item.get('bbox')}；{REASONS.get(item['reason_code'],item['reason_code'])}")
        if row["group"]=="optical":
            conditions.append("当前供应商镶嵌图；不支持用户指定2026年1—4月成像时相。全图拍摄日期/传感器/原生分辨率未证实。")
            for item in checks:
                if item["capability"]=="download" and item["scope"]=="sample":
                    conditions.append("样本请求bbox="+str(item.get("bbox")))
            if row.get("bbox_semantics"):
                conditions.append("ArcGIS同bbox512/1024输出范围一致="+str(row["bbox_semantics"].get("bbox_extent_same"))+"；实际范围可能为适应纵横比扩张。")
        if row.get("sample_scene"):
            conditions.append("用于验证的场景："+str(row["sample_scene"].get("id"))+"；真实拍摄时间="+str(row["sample_scene"].get("acquired_at")))
        reasons=list(dict.fromkeys(REASONS.get(item["reason_code"],item["reason_code"]) for item in checks if item["state"]!="passed"))
        states={item["state"] for item in checks}
        remedies=[]
        if any(item["reason_code"]=="omap_permission_denied" for item in checks):remedies.append("复核本机奥维账户及该图源授权；授权改变后主动刷新缓存，不重复向模型试探。")
        if "credentials_required" in states and row["group"]!="optical":remedies.append("在提供商官方渠道开通/登录账户后再接下载适配；当前不假装已下载受保护科学文件。")
        if "failed" in states:remedies.append("暂态网络或超时短期缓存；到期或人工刷新后再核验，不能改写成永久不可用。")
        if any(item["reason_code"] in {"omap_placeholder_grid","provider_notice_tile"} for item in checks):remedies.append("保留失败图作为核验材料，不送识别模型；待图源配置/瓦片加载恢复后再重新取图。PNG能解码并不代表影像有效。")
        if any(item["state"]=="no_coverage" for item in checks):remedies.append("核对所需时段与点位，必要时缩小/扩大检索；未知AOI继续标未知，不全域排除来源。")
        if "unsupported" in states:remedies.append("历史时相和科学裁切按接口支持能力分别处理，底图不能替代指定日期的科学影像。")
        if not reasons:reasons=["本次已验证的能力通过；仍须遵守样本范围与有效期。"]
        links=[]
        for artifact in row.get("artifacts",[]):
            path=Path(artifact["artifact_path"])
            links.append(f"<a href='{text(path.name)}'>{text(path.name)}</a>（{text(artifact['bytes'])}字节）")
        sections.append("<tr>"+f"<td>{text(LABELS[row['group']])}<br><b>{text(row['source'])}</b><br>{text(name)}</td>"
            +f"<td>{status(checks,'interface')}</td><td>{status(checks,'coverage')}</td><td>{status(checks,'download')}</td>"
            +f"<td>裁切：{status(checks,'crop')}<br>预览：{status(checks,'preview')}</td>"
            +"<td>"+"<br>".join(text(value) for value in conditions)+"</td>"
            +"<td>"+"<br>".join(text(value) for value in reasons+remedies)+"</td>"
            +"<td>"+("<br>".join(links) or "无本次下载文件")+"</td></tr>")
    sections.extend(["</tbody></table></div><h2>缓存与失效</h2>",
                     "<p>目录健康6小时，临时失败15分钟，账户/图源权限24小时，未实现功能7天。覆盖记录绑定实际bbox和时段，新点位保持未知；端点或凭证摘要变化不复用。显式刷新会失效旧记录，保留审计轨迹。过期失败不能永久排除来源。</p>",
                     "<p>接口可查、足迹相交、RGB预览、科学文件下载四者分开。表中“需要账户”可能依据提供商目录声明；只有带本次下载文件及校验的项目才记为文件获取通过。</p>"])
    style="body{font:14px/1.6 system-ui,'Microsoft YaHei',sans-serif;color:#18332d;margin:30px}h1,h2{color:#075d4d}p{max-width:1300px}.table{overflow:auto}table{border-collapse:collapse;width:100%;min-width:1400px}td,th{border:1px solid #d6dfda;padding:11px;vertical-align:top;overflow-wrap:anywhere}th{background:#e3eee7;position:sticky;top:0}tr:nth-child(even){background:#f5f8f5}.passed{color:#096341;font-weight:600}.failed,.blocked{color:#9c3c19}.unknown{color:#626c66}.credentials_required{color:#80580e}.unsupported,.no_coverage{color:#5b5887}a{color:#106856}"
    destination.write_text("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>成都图源实测核验表</title><style>"+style+"</style><body>"+"\n".join(sections)+"</body></html>",encoding="utf-8")
    markdown=["# 成都代表点图源可用性实测", "", "代表点核验，不代表36点全覆盖；坐标系及原表面积单位未知。PNG能解码不代表影像有效。", "", "|组|来源|接口|覆盖|文件获取|裁切|主要限制|", "|---|---|---|---|---|---|---|"]
    for row in rows:
        latest={item["capability"]:item for item in row["checks"]}
        reason="；".join(dict.fromkeys(REASONS.get(item["reason_code"],item["reason_code"]) for item in row["checks"] if item["state"]!="passed"))
        markdown.append("|"+"|".join([LABELS[row["group"]],row["source"],*[STATES.get(latest.get(capability,{}).get("state"),"尚未验证") for capability in ("interface","coverage","download","crop")],reason])+"|")
    destination.with_suffix(".md").write_text("\n".join(markdown)+"\n",encoding="utf-8")
    return {"source_count":len(rows),"downloaded_source_count":downloaded,"group_counts":counts,"html_path":str(destination.resolve())}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input",type=Path,default=Path("outputs/source-audit/source-results.json"))
    parser.add_argument("--output",type=Path,default=Path("outputs/source-audit/source-report.html"))
    args=parser.parse_args()
    print(json.dumps(render(args.input,args.output),ensure_ascii=True))
