"""Recheck saved source downloads without calling providers or models."""
import argparse
import json
from pathlib import Path
import sys

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))

from omap_api.image_quality import validate_optical_preview
from wuyu_harness.source_registry import SourceRegistry,utc_now


def recheck(path: Path, registry_path: Path, provider_notice_files: list[str]):
    body=json.loads(path.read_text(encoding="utf-8"));registry=SourceRegistry(registry_path)
    fingerprint=body["endpoint_fingerprint"];summaries=[]
    for row in body["sources"]:
        if row["group"]!="optical":continue
        invalid=[]
        for artifact in row.get("artifacts",[]):
            image_path=Path(artifact["artifact_path"])
            quality=validate_optical_preview(image_path.read_bytes())
            result=quality["placeholder_check"]
            artifact["image_quality"]=quality
            artifact["placeholder_check"]=result
            notice=image_path.name in provider_notice_files or quality["reason"]=="provider_notice_tile"
            artifact["manual_review"]="provider_notice_not_imagery" if notice else "not_a_semantic_content_guarantee"
            if not quality["valid"] or notice:
                invalid.append((artifact,"provider_notice_tile" if notice else "omap_placeholder_grid"))
        if invalid:
            registry.invalidate(fingerprint,group="optical",source=row["source"],capability="download",reason="image_quality_recheck_v3")
            # Keep non-image permissions/history evidence, discard false image passes.
            row["checks"]=[c for c in row["checks"] if not(c["state"]=="passed" and c["capability"] in {"download","interface"})]
            for artifact,reason in invalid:
                sample=next((s for s in body["sample_profile"]["samples"] if s["id"] in artifact["artifact_path"]),body["sample_profile"]["samples"][0])
                record=registry.record(fingerprint,"optical",row["source"],"download","failed",operation="optical.latest",
                    scope="sample",bbox=sample["bbox"],sample_id=sample["id"],reason_code=reason,
                    evidence={"artifact_path":artifact["artifact_path"],"sha256":artifact["sha256"],"image_quality":artifact["image_quality"],"manual_review":artifact["manual_review"]})
                row["checks"]=[check for check in row["checks"] if not (check["capability"]=="download" and check.get("bbox")==sample["bbox"] and check.get("operation")=="optical.latest")]
                row["checks"].append(record)
            row["quality_status"]="not_usable_imagery"
        elif row.get("artifacts"):
            row["quality_status"]="known_placeholder_check_passed"
        summaries.append({"source":row["source"],"quality_status":row.get("quality_status","no_image"),"invalid_artifact_count":len(invalid)})
    body["image_quality_rechecked_at"]=utc_now().isoformat()
    path.write_text(json.dumps(body,ensure_ascii=False,indent=2),encoding="utf-8")
    return summaries


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input",type=Path,default=PROJECT/"outputs/source-audit/source-results.json")
    parser.add_argument("--registry",type=Path,default=PROJECT/"outputs/wuyu/source-registry.json")
    parser.add_argument("--provider-notice-file",action="append",default=[])
    args=parser.parse_args()
    print(json.dumps(recheck(args.input,args.registry,args.provider_notice_file),ensure_ascii=True))
