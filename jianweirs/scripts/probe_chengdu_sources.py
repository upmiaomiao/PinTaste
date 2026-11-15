"""Bounded, resumable live source checks using read-only Chengdu sample data.

No inference/search models are called. Science downloads use small subsets;
provider credentials are never requested, copied into artifacts, or printed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
from pathlib import Path
import sys
import time

import httpx
from PIL import Image
from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from wuyu_harness.data import DataClient, DataClientError
from wuyu_harness.source_registry import utc_now
from omap_api.image_quality import validate_optical_preview


def crop_bbox(sample: dict, radius: float = .0006) -> list:
    lon, lat = sample["longitude"], sample["latitude"]
    return [lon-radius, lat-radius, lon+radius, lat+radius]


def scene_list(result: dict, *, sar: bool) -> list:
    items=result.get("items" if sar else "scenes")
    if not isinstance(items,list):
        raise DataClientError("invalid_scene_catalog", "Provider catalogue did not contain its required scene list")
    return items


class Probe:
    def __init__(self, data: DataClient, samples: dict, folder: Path, force: bool, only: list[str] | None = None):
        self.data, self.samples, self.folder, self.force = data, samples, folder, force
        self.primary = samples["samples"][0]
        self.only = set(only or [])
        self.period = samples["time_range"]
        self.rows: dict[str, dict] = {}
        self.output = folder / "source-results.json"
        if self.output.exists():
            previous = json.loads(self.output.read_text(encoding="utf-8"))
            if previous.get("endpoint_fingerprint") == data.configuration_fingerprint:
                self.rows = {r["group"]+":"+r["source"]:r for r in previous.get("sources", [])}
        self.lock = asyncio.Lock()
        self.semaphore = asyncio.Semaphore(3)

    async def save(self):
        async with self.lock:
            body = {"schema_version":"1.0", "endpoint_fingerprint":self.data.configuration_fingerprint,
                    "checked_at":utc_now().isoformat(), "sample_profile":self.samples,
                    "sources":list(self.rows.values()),
                    "boundaries":["A basemap provider is not an identified satellite", "Unknown native resolution/acquisition date stay unknown", "A catalogue footprint does not establish cloud-free full ROI coverage", "Input workbook CRS is not declared; numeric coordinates are tested as WGS84 without claiming registration", "Science files are not visual waste-recognition results"]}
            self.folder.mkdir(parents=True, exist_ok=True)
            self.output.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")

    def check(self, row, capability, state, *, operation=None, scope="endpoint", bbox=None, period=None,
              reason="", http_status=None, evidence=None, ttl=None):
        sample_id = None
        if scope == "sample":
            sample_id = next((s["id"] for s in self.samples["samples"] if s["bbox"] == list(bbox)), None)
            sample_id = sample_id or ("chengdu_regional_envelope" if list(bbox) == self.samples["regional_bbox"] else self.primary["id"])
        item = self.data.source_registry.record(
            self.data.configuration_fingerprint, row["group"], row["source"], capability, state,
            operation=operation, scope=scope, bbox=bbox, time_range=period,
            sample_id=sample_id, reason_code=reason,
            status_code=http_status, evidence=evidence, ttl_seconds=ttl)
        row["checks"].append(item)

    def failure(self, row, capability, error, operation, *, scope="endpoint", bbox=None, period=None):
        auth = error.status_code == 401 or any(s in error.code for s in ("auth_required", "login_required", "account_required", "permission_denied"))
        unsupported = any(s in error.code for s in ("unsupported", "not_configured", "source_disabled", "query_disabled"))
        state = "credentials_required" if auth else "unsupported" if unsupported else "failed"
        if auth:
            scope, bbox, period = "endpoint", None, None
        self.check(row, capability, state, operation=operation, scope=scope, bbox=bbox, period=period,
                   reason=error.code, http_status=error.status_code, evidence={"error_class":"DataClientError"})

    async def artifact(self, row, result, name, *, preview=False):
        url=result.get("file_url") or result.get("image_url")
        if not url:
            raise DataClientError("no_local_artifact", "Operation returned no local artifact")
        content,media=await self.data.download(url)
        suffix=".tif" if "tif" in media else ".jpg" if "jpeg" in media else ".png"
        target=self.folder/(name+suffix); target.write_bytes(content)
        detail={"artifact_path":str(target.resolve()),"bytes":len(content),"media_type":media,
                "sha256":hashlib.sha256(content).hexdigest(),"preview_is_analysis_data":False if preview else None}
        if suffix in (".png",".jpg"):
            with Image.open(io.BytesIO(content)) as image:
                image.load();detail["dimensions"]=list(image.size)
            if row["group"]=="optical":
                detail["image_quality"]=validate_optical_preview(content)
                detail["placeholder_check"]=detail["image_quality"]["placeholder_check"]
                if not detail["image_quality"]["valid"]:
                    row.setdefault("artifacts",[]).append(detail)
                    raise DataClientError(detail["image_quality"]["reason"], "Decoded file contains a known non-imagery error tile")
        else:
            try:
                import rasterio
                with rasterio.open(target) as raster:
                    detail.update(dimensions=[raster.width,raster.height],bands=raster.count,crs=str(raster.crs),
                                  valid_fraction=float((raster.dataset_mask()>0).mean()))
            except (ImportError,OSError):
                detail["raster_validation"]="unavailable"
        row.setdefault("artifacts",[]).append(detail)
        return detail

    async def optical(self, row, spec, desktop):
        op="optical.latest"
        if spec.get("requires_desktop", row["source"]!="arcgis") and not desktop["available"]:
            self.check(row,"interface","blocked",operation=op,reason="shared_desktop_dependency_unavailable",
                       evidence={"shared_probe":"GET /api/current","upstream_code":desktop["code"],"individual_capture_attempted":False})
            self.check(row,"download","unknown",operation=op,reason="desktop_capture_not_attempted",ttl=900)
            return
        samples=self.samples["samples"] if row["source"]=="arcgis" else [self.primary]
        for index,sample in enumerate(samples):
            bbox=sample["bbox"]
            try:
                result=await self.data.execute(op,{"source":row["source"],"bbox":bbox,"coord_type":0,
                    "zoom":min(18,(spec.get("map_metadata") or {}).get("max_zoom",18)),"width":512,"height":512,"timeout_seconds":40})
                detail=await self.artifact(row,result,row["source"]+"-"+sample["id"])
                meta=result.get("metadata",{});point=meta.get("point_acquisition_metadata") or {}
                evidence={**detail,"sample_id":sample["id"],"actual_bbox_wgs84":meta.get("actual_bbox_wgs84"),
                          "metadata_scope":point.get("scope","unknown"),"native_resolution_whole_image":None,
                          "acquisition_time_whole_image":None,"coverage_verified":meta.get("coverage_verified",False)}
                self.check(row,"download","passed",operation=op,scope="sample",bbox=bbox,reason="decoded_optical_image",evidence=evidence)
                self.check(row,"interface","passed",operation=op,reason="actual_capture_returned")
                if index==0 and row["source"]=="arcgis":
                    # Same bbox/aspect ratio: pixel size changes, geographic extent should not.
                    second=await self.data.execute(op,{"source":"arcgis","bbox":bbox,"coord_type":0,"zoom":16,
                                                       "width":1024,"height":1024,"timeout_seconds":40})
                    second_detail=await self.artifact(row,second,"arcgis-bbox-1024")
                    evidence2={"requested_bbox":bbox,"512_actual_bbox":meta.get("actual_bbox_wgs84"),
                               "1024_actual_bbox":second.get("metadata",{}).get("actual_bbox_wgs84"),
                               "bbox_extent_same":meta.get("actual_bbox_wgs84")==second.get("metadata",{}).get("actual_bbox_wgs84"),
                               "zoom_parameter_applied":False,"second_dimensions":second_detail.get("dimensions")}
                    row["bbox_semantics"]=evidence2
            except DataClientError as error:
                self.failure(row,"download",error,op,scope="sample",bbox=bbox)
                break
        self.check(row,"historical_selection","unsupported",operation=op,reason="current_mosaic_has_no_requested_date_selection",ttl=604800)

    async def science(self,row,spec):
        group,source=row["group"],row["source"]
        sar=group=="sar";op=group+".search"
        if sar and spec.get("query_enabled") is False:
            self.check(row,"interface","unsupported",operation=op,reason="source_catalog_query_not_implemented",evidence={"catalog_access":spec.get("catalog_access")})
            self.check(row,"download","credentials_required",reason="jaxa_registered_account_not_configured")
            self.check(row,"crop","unsupported",operation="sar.crop",reason="roi_crop_not_implemented_for_source")
            return
        query={"source":source,"bbox":self.primary["bbox"],"limit":2}
        if sar:query.update(start="2026-01-01T00:00:00Z",end="2026-04-30T23:59:59Z")
        else:query["datetime"]=self.period
        try:
            result=await self.data.execute(op,query)
            self.check(row,"interface","passed",operation=op,reason="actual_catalog_query_returned")
            items=scene_list(result,sar=sar)
            self.check(row,"coverage","passed" if items else "no_coverage",operation=op,scope="sample",bbox=query["bbox"],period=self.period,
                       reason="scene_footprints_intersect" if items else "no_catalog_scenes_in_sample_period",evidence={"scene_count":len(items),"sample_ids":[str(i.get("id")) for i in items],"full_roi_usable_coverage_verified":False})
            chosen_period=self.period
            if not items:
                # Regional archive fallback distinguishes absent task-period coverage from an unavailable source.
                query["bbox"]=self.samples["regional_bbox"]
                if sar:query.update(start="2000-01-01T00:00:00Z",end="2026-10-16T23:59:59Z")
                else:query["datetime"]="2022-01-01/2026-10-16"
                chosen_period="2000-01-01/2026-10-16" if sar else query["datetime"]
                result=await self.data.execute(op,query);items=scene_list(result,sar=sar)
                self.check(row,"coverage","passed" if items else "no_coverage",operation=op,scope="sample",bbox=query["bbox"],period=chosen_period,
                           reason="regional_archive_intersection" if items else "no_catalog_scenes_in_regional_archive",evidence={"scene_count":len(items),"full_roi_usable_coverage_verified":False})
            if not items:
                self.check(row,"download","unknown",reason="no_candidate_scene_to_test")
                return
            item=items[0];row["sample_scene"]={"id":item.get("id"),"acquired_at":item.get("acquired_at"),"source":source,
                                                       "search_bbox":query["bbox"],"search_period":chosen_period}
            can_crop=spec.get("crop_enabled") if sar else spec.get("roi_subset_available")
            if can_crop:
                # Only crop scenes that intersect the representative point; a regional-only hit is not assumed to do so.
                if query["bbox"]!=self.primary["bbox"]:
                    self.check(row,"crop","unknown",operation="sar.crop" if sar else "multispectral.subset",reason="regional_scene_not_verified_over_primary_point")
                else:
                    crop_op="sar.crop" if sar else "multispectral.subset"
                    payload={"source":source,"item_id" if sar else "scene_id":item["id"],"bbox":crop_bbox(self.primary)}
                    if sar:payload["polarizations"]=["vv"]
                    else:
                        choices=[name for name in ("B04","B03","B02","red","green","blue") if name in item.get("assets",{})]
                        payload.update(assets=choices[:3] or list(item.get("assets",{}))[:1],width=64,height=64)
                    try:
                        cropped=await self.data.execute(crop_op,payload);detail=await self.artifact(row,cropped,source+"-small-science-subset")
                        self.check(row,"crop","passed",operation=crop_op,scope="sample",bbox=payload["bbox"],period=chosen_period,reason="decoded_geotiff_subset",evidence=detail)
                        self.check(row,"download","passed",scope="sample",bbox=payload["bbox"],period=chosen_period,reason="small_science_subset_downloaded",evidence=detail)
                    except DataClientError as error:self.failure(row,"crop",error,crop_op)
            else:
                crop_op="sar.crop" if sar else ("multispectral.subset" if group=="multispectral" else "hyperspectral.asset")
                self.check(row,"crop","unsupported",operation=crop_op,reason="roi_crop_not_implemented_for_source")
            account=("earthdata" in str(spec.get("data_access","")).lower() or spec.get("download_authentication_required"))
            if account:
                self.check(row,"download","credentials_required",reason="provider_science_download_requires_user_account",
                           evidence={"basis":"published_source_catalog","provider":spec.get("provider"),"no_protected_file_download_attempted":True})
            # One ASF preview verifies public browse access without downloading whole science scenes.
            if sar and source=="sentinel1-grd":
                try:
                    preview=await self.data.execute("sar.preview",{"source":source,"item_id":item["id"]})
                    detail=await self.artifact(row,preview,source+"-preview",preview=True)
                    self.check(row,"preview","passed",operation="sar.preview",scope="sample",bbox=query["bbox"],period=chosen_period,reason="decoded_public_preview",evidence=detail)
                except DataClientError as error:self.failure(row,"preview",error,"sar.preview")
        except DataClientError as error:
            self.failure(row,"interface",error,op)
            self.check(row,"coverage","unknown",scope="sample",bbox=query["bbox"],period=self.period,reason="provider_query_failed_not_no_coverage")

    async def run_source(self,group,spec,desktop):
        source=spec.get("source",spec.get("id"));key=group+":"+source
        if self.only and source not in self.only:
            return
        if self.force:
            self.data.source_registry.invalidate(self.data.configuration_fingerprint,group=group,source=source,reason="explicit_probe_refresh")
        old=self.rows.get(key)
        if old and not self.force and old.get("finished_at"):
            # Registry freshness and exact configuration control resumed checks.
            if all(item.get("expires_at","")>utc_now().isoformat() for item in old.get("checks",[])):
                return
        row={"group":group,"source":source,"catalog_metadata":spec,"started_at":utc_now().isoformat(),"checks":[]}
        self.rows[key]=row
        async with self.semaphore:
            try:
                async with asyncio.timeout(200):
                    if group=="optical":await self.optical(row,spec,desktop)
                    else:await self.science(row,spec)
            except TimeoutError:
                self.check(row,"interface","failed",reason="bounded_probe_deadline_exceeded",evidence={"deadline_seconds":200})
            except Exception as error:
                self.check(row,"interface","unknown",reason="probe_"+type(error).__name__)
        row["finished_at"]=utc_now().isoformat();await self.save()
        print(json.dumps({"source":source,"group":group,"states":{item["capability"]:item["state"] for item in row["checks"]}}),flush=True)

    async def run(self):
        catalog=await self.data.catalog(force_refresh=True)
        desktop={"available":False,"code":"unknown"}
        try:
            body,_,_=await self.data._read("GET","/api/current")
            desktop["available"]=isinstance(self.data._object(body).get("center"),list);desktop["code"]="available" if desktop["available"] else "not_ready"
        except DataClientError as error:desktop["code"]=error.code
        await asyncio.gather(*(self.run_source(group,spec,desktop) for group,body in catalog.get("groups",{}).items() for spec in body.get("sources",[])))
        await self.save()
        lines=["# 成都样本图源实测", "", "每项仅代表记录中的空间、时间及配置；未知覆盖仍可作为候选。", "", "|组|来源|接口|覆盖|下载|裁剪|原因|", "|---|---|---|---|---|---|---|"]
        for row in self.rows.values():
            latest={item["capability"]:item for item in row["checks"]}
            lines.append("|"+"|".join([row["group"],row["source"],*[latest.get(name,{}).get("state","unknown") for name in ("interface","coverage","download","crop")],"; ".join(dict.fromkeys(item["reason_code"] for item in row["checks"] if item["state"]!="passed"))])+"|")
        (self.folder/"source-report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")


async def main(args):
    samples=json.loads(args.samples.read_text(encoding="utf-8"))
    async with DataClient(max_wait_seconds=65) as data:
        await Probe(data,samples,args.output,args.force,args.only).run()


if __name__=="__main__":
    load_dotenv(PROJECT/".env.wuyu",override=False)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples",type=Path,default=PROJECT/"outputs/source-audit/chengdu-samples.json")
    parser.add_argument("--output",type=Path,default=PROJECT/"outputs/source-audit")
    parser.add_argument("--force",action="store_true",help="Ignore fresh saved probes for a deliberate recheck")
    parser.add_argument("--only",nargs="*",help="Recheck selected source IDs without repeating other downloads")
    asyncio.run(main(parser.parse_args()))
