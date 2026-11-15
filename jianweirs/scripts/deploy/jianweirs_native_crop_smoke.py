"""Actual bounded native S2 and SAR delivery from a Linux science worker."""
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
    os.environ["OMAP_SPECTRAL_SUBSET_DIR"]=str(stage/"worker-subsets")
    os.environ["OMAP_SAR_OUTPUT_DIR"]=str(stage/"worker-sar")
    os.environ.pop("OMAP_PSK",None);os.environ.pop("OMAP_WS_URL",None)
    from omap_api.app import create_app
    from wuyu_harness.data import DataClient,DataClientError
    from wuyu_harness.science import acquire_native_s2
    import httpx
    import rasterio
    from rasterio.io import MemoryFile
    app=create_app(output_dir=stage/"worker-optical")
    report={"platform":"linux","models_called":False,"requested_bbox":[104.058,30.668,104.062,30.672],"checks":[]}
    bbox=report["requested_bbox"]
    def inspect(path):
        with rasterio.open(path) as ds:
            return {"crs":str(ds.crs),"transform":list(ds.transform)[:6],"width":ds.width,"height":ds.height,
                "bands":list(ds.descriptions),"count":ds.count,"dtype":list(ds.dtypes),"nodata":str(ds.nodata),
                "pixel_spacing":list(ds.res),"valid_fraction_by_band":[float((ds.read_masks(i)>0).mean()) for i in range(1,ds.count+1)]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://localhost") as http:
        data=DataClient(base_url="http://localhost",api_key=os.environ["OMAP_WORKER_API_KEY"],mode="worker",client=http,unified_config={"enabled":False},max_wait_seconds=600)
        spectral=await data.execute("multispectral.search",{"source":"sentinel2","bbox":bbox,"datetime":"2024-01-01/2024-12-31","max_cloud_cover":20,"limit":3})
        scenes=spectral.get("scenes",spectral.get("items",[]))
        scenes.sort(key=lambda scene:float(scene.get("cloud_cover") or 0))
        if scenes:
            native=await acquire_native_s2(data,scenes[0],bbox,stage/"native-sentinel2",asset_ids=("visual","SCL","B04","B08","product-metadata"))
            report["checks"].append({"operation":"S2_native_COG_window","scene_id":scenes[0]["id"],"acquired_at":scenes[0].get("acquired_at"),
                "passed":all(native["assets"].get(key,{}).get("status")=="completed" for key in ("visual","SCL","B04","B08")),"native":native})
            try:
                result=await data.execute("multispectral.subset",{"source":"sentinel2","scene_id":scenes[0]["id"],"bbox":bbox,"assets":["B04","B08"],"width":64,"height":64})
                content,_=await data.download(result["file_url"]);target=stage/"S2-api-subset.tif";target.write_bytes(content)
                report["checks"].append({"operation":"multispectral.subset","passed":True,"bytes":len(content),"raster":inspect(target),
                    "resampling":result["metadata"].get("resampling"),"values":result["metadata"].get("values"),"scale_and_offset_applied":result["metadata"].get("scale_and_offset_applied")})
            except DataClientError as error:report["checks"].append({"operation":"multispectral.subset","passed":False,"code":error.code})
        sar=await data.execute("sar.search",{"source":"sentinel1-rtc","bbox":bbox,"start":"2024-01-01T00:00:00Z","end":"2024-12-31T23:59:59Z","limit":1})
        scenes=sar.get("scenes",sar.get("items",[]))
        if scenes:
            try:
                result=await data.execute("sar.crop",{"source":"sentinel1-rtc","item_id":scenes[0]["id"],"bbox":bbox,"polarizations":["vv","vh"]})
                content,_=await data.download(result["file_url"]);target=stage/"S1-native-VV-VH.tif";target.write_bytes(content)
                report["checks"].append({"operation":"sar.crop","passed":True,"bytes":len(content),"raster":inspect(target),
                    "units":result.get("units"),"georeferenced":result.get("georeferenced"),"data_downloaded":result.get("data_downloaded"),"acquired_at":result.get("acquired_at")})
            except DataClientError as error:report["checks"].append({"operation":"sar.crop","passed":False,"code":error.code})
    report["status"]="passed" if len(report["checks"])==3 and all(row["passed"] for row in report["checks"]) else "partial"
    (stage/"verification.json").write_text(json.dumps(report,indent=2));print(json.dumps({"status":report["status"],"checks":[{"operation":row["operation"],"passed":row["passed"],"code":row.get("code")} for row in report["checks"]]}))


if __name__=="__main__":asyncio.run(main())
