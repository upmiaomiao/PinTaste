"""Verify Linux science routes without a desktop or public HTTP listener."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys


async def main():
    root=Path(sys.argv[1]).resolve();stage=Path(sys.argv[2]).resolve();stage.mkdir(parents=True,exist_ok=True)
    sys.path.insert(0,str(root))
    os.environ["OMAP_WORKER_API_KEY"]=secrets.token_urlsafe(32)
    os.environ.pop("OMAP_PSK",None);os.environ.pop("OMAP_WS_URL",None)
    from omap_api.app import create_app
    import httpx
    app=create_app(output_dir=stage/"files")
    report={"platform":"linux","desktop_configured":False,"real_provider_queries":True,"checks":[]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://localhost") as client:
        response=await client.get("/api/sar/sources")
        report["checks"].append({"route":"unauthenticated_science","status_code":response.status_code,"passed":response.status_code==401})
        client.headers["Authorization"]="Bearer "+os.environ["OMAP_WORKER_API_KEY"]
        for path in ("/health","/api/optical/sources","/api/spectral/sources","/api/sar/sources"):
            response=await client.get(path)
            body=response.json()
            report["checks"].append({"route":path,"status_code":response.status_code,"passed":response.status_code==200,
                "source_count":len(body.get("sources",[])),"desktop_status":body.get("desktop_status")})
        async def query(path,payload):
            response=await client.post(path,json=payload)
            body=response.json()
            scenes=body.get("scenes",body.get("items",[]))
            error=body.get("detail",{})
            report["checks"].append({"route":path,"status_code":response.status_code,"passed":response.status_code==200,
                "scene_count":len(scenes) if isinstance(scenes,list) else None,
                "error_code":error.get("code") if isinstance(error,dict) else None,
                "dates":[row.get("acquired_at") for row in scenes] if isinstance(scenes,list) else []})
        bbox=[104.05,30.66,104.07,30.68]
        await asyncio.gather(query("/api/spectral/search",{"source":"sentinel2","bbox":bbox,"datetime":"2024-01-01/2024-12-31","limit":1}),
            query("/api/sar/search",{"source":"sentinel1-rtc","bbox":bbox,"start":"2024-01-01T00:00:00Z","end":"2024-12-31T23:59:59Z","limit":1}))
    report["status"]="passed" if all(check["passed"] for check in report["checks"]) else "partial"
    (stage/"verification.json").write_text(json.dumps(report,indent=2));print(json.dumps(report))


if __name__=="__main__":asyncio.run(main())
