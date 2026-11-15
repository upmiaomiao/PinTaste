"""One bounded model workflow on a geocoded village context, not a located site."""
import asyncio
import base64
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from wuyu_harness.config import Settings,ModelConfig
from wuyu_harness.data import DataClient
from wuyu_harness.service import Service
from wuyu_harness.schemas import ImageMetadata,UploadRequest,TaskRequest,TaskKind


async def main():
    folder=ROOT/"outputs/portal"
    with sqlite3.connect((folder/"portal.sqlite3").as_uri()+"?mode=ro",uri=True) as db:
        ciphertext=db.execute("SELECT ciphertext FROM config WHERE id=1").fetchone()[0]
    cfg=json.loads(Fernet((folder/".config.key").read_bytes()).decrypt(ciphertext))
    geo=json.loads((ROOT/"outputs/unified-image-integration/complaint-geocode-20261025.json").read_text(encoding="utf8"))
    candidate=next((row for row in geo["candidates"] if row["method"]=="geocode" and row["match_level"]=="村庄" and row["name"]=="云南省玉溪市通海县里山乡五山村"),None)
    if candidate is None:raise SystemExit("Grounded village candidate absent; no imagery or model calls made")
    center=candidate["center_wgs84"]
    models={role:ModelConfig(**{**body,"input_modalities":tuple(body["input_modalities"]),
        "max_tokens":min(body["max_tokens"],4096),"timeout":min(body["timeout"],240)}) for role,body in cfg["models"].items()}
    output=ROOT/"outputs/unified-image-integration"/("village-live-"+uuid.uuid4().hex[:10])
    settings=Settings(output_dir=output,models=models,max_assets=2,concurrency=1,rs_output_format=cfg["task_policy"]["rs_output_format"])
    service=Service(settings)
    await service.data.aclose()
    private=dotenv_values(ROOT/".local/unified-access.env")
    data=cfg["data"]
    service.data=DataClient(data["base_url"],data["api_key"],data["mode"],unified_config={"enabled":True,
        "base_url":private["WUYU_UNIFIED_BASE_URL"],"api_key":private["WUYU_UNIFIED_API_KEY"]})
    service.pipeline.data=service.data
    await service.start()
    try:
        assets=[]
        for params in ({"source":"gaofen2026"},{"source":"century","year":2024}):
            result=await service.data.execute("optical.latest",{**params,"lon":center[0],"lat":center[1],"zoom":17,"width":1024,"height":1024})
            content,mime=await service.data.download(result["image_url"])
            meta=result["metadata"]
            uploaded=service.store.upload(UploadRequest(filename=params["source"]+"-village-context.png",content_base64=base64.b64encode(content).decode(),
                metadata=ImageMetadata(role="local_context",source=params["source"],metadata_scope=meta["metadata_scope"],period_label=str(params.get("year","当前底图组，拍摄日期未核实")))))
            asset=service.store.get(uploaded["id"],"image")
            asset.update(data_api_metadata=meta,geographic_basis={**candidate,"scope":"village_context_only","precise_complaint_location_verified":False})
            service.store.put("image",asset);assets.append(uploaded["id"])
        task=TaskRequest(objective="对云南省玉溪市通海县里山乡五山村做村域背景遥感筛查。提供的地址还包含香胡路，但道路和被投诉企业未精确定位，当前图幅仅以村庄地理编码中心取景，不是被投诉企业或排放口。仅描述实际可见的地物、疑似露天堆物及周边场景；材料涉及液体废物线索，卫星图不能确认液体成分、化学污染、危险属性、违法排放，也不能据村域背景定位匿名企业。两份底图版次和OCR显示日期不等于经核实的整幅拍摄时相，不能作变化或清理结论。本次关闭联网检索和追加取图，重点验证真实模型判读、误报复核和报告是否遵守证据边界。",
            task_types=[TaskKind.DISCOVERY,TaskKind.ENVIRONMENT],center=tuple(center),asset_ids=assets,acquire_optical=False,
            online_supplement="uploaded_only",search_policy=False,consult_shensi=False,adaptive_review=False)
        run=service.pipeline.submit(task)
        await asyncio.wait_for(service.pipeline.pending[run["id"]],600)
        record=service.store.get(run["id"],"run")
        package_path=service.store.run_dir(run["id"])/"finding-package.json"
        package=json.loads(package_path.read_text(encoding="utf8")) if package_path.exists() else {}
        report={"status":record["status"],"run_id":run["id"],"engine_dir":str(output),"precise_complaint_site_located":False,
            "grounded_area":candidate["name"],"model_call_roles":[row.get("role") for row in package.get("model_calls",[])],
            "recognition_status":package.get("recognition_status"),"narrative_status":package.get("narrative_status"),
            "image_review_states":[row.get("optical_false_positive_review",{}).get("status") for row in package.get("image_assessments",[])],
            "result_status":package.get("status"),"artifacts":record.get("artifacts",[]),"warnings":package.get("warnings",[])}
        (ROOT/"outputs/unified-image-integration/village-live-verification-20261025.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf8")
        print(json.dumps(report,ensure_ascii=True))
    finally:await service.close()


if __name__=="__main__":asyncio.run(main())
