"""Run one real portal task against the isolated acceptance account."""
import json
from pathlib import Path
import sys
import time
import httpx

ROOT=Path(__file__).resolve().parents[1]


def main():
    origin="http://127.0.0.1:3081"
    credentials=json.loads((ROOT/"outputs/portal-smoke-credentials.json").read_text(encoding="utf-8"))
    with httpx.Client(trust_env=False,timeout=30,headers={"Origin":origin}) as client:
        login=client.post(origin+"/portal/api/login",json={key:credentials[key] for key in ("username","password")})
        login.raise_for_status()
        session=client.get(origin+"/portal/api/session").json()
        client.headers["X-CSRF-Token"]=session["csrf_token"]
        request={"objective":"核查104.461117 30.858473点位是否存在疑似固废倾倒或堆存。先获取同点位的多个可用来源逐图分析，再根据覆盖或细节不足追加周边或局部影像，最后给出综合结论与依据。",
                 "center":[104.461117,30.858473],"acquire_optical":True,"coordinate_system":"unknown","search_policy":False}
        response=client.post(origin+"/portal/api/tasks",json=request)
        response.raise_for_status()
        identity=response.json()["id"]
        (ROOT/"outputs/wuyu-multisource-20261017-task.json").write_text(json.dumps({"id":identity,"origin":origin,"request":request},ensure_ascii=False,indent=2),encoding="utf-8")
        print("Real acceptance task submitted: "+identity,flush=True)
        started=time.monotonic();last=None
        while time.monotonic()-started<900:
            result=client.get(origin+"/portal/api/tasks/"+identity)
            result.raise_for_status();task=result.json()
            progress=(task["status"],len(task.get("images",[])),tuple((s["id"],s["status"]) for s in task.get("stages",[])))
            if progress!=last:
                print(json.dumps({"status":progress[0],"images":progress[1],"stages":progress[2]},ensure_ascii=False),flush=True);last=progress
            if task["status"] not in ("planning","queued","running","pending"):
                if task["status"]!="succeeded":
                    raise RuntimeError("Real task ended without report: "+task.get("error",task["status"]))
                break
            time.sleep(15)
        else:raise TimeoutError("Acceptance task remains active; it was not resubmitted")
        baseline=(task.get("selection") or {}).get("baseline",{})
        verification={"status":task["status"],"task_id":identity,"elapsed_seconds":round(time.monotonic()-started,1),
            "image_count":len(task["images"]),"sources":[i["metadata"]["source"] for i in task["images"]],
            "baseline":baseline,"coverage_decisions":(task.get("adaptive_review") or {}).get("coverage_decisions",[]),
            "fusion":task.get("fusion"),"narrative":task.get("narrative"),"science":task.get("scientific_evidence",[])}
        (ROOT/"outputs/wuyu-multisource-20261017-verification.json").write_text(json.dumps(verification,ensure_ascii=False,indent=2),encoding="utf-8")
        assert baseline.get("all_baseline_acquired_before_recognition") is True
        assert task.get("narrative",{}).get("summary")
        print(json.dumps({"status":"passed","image_count":verification["image_count"],"source_groups":baseline.get("acquired_source_groups"),"judgment":task.get("fusion",{}).get("judgment")},ensure_ascii=False),flush=True)


if __name__=="__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
