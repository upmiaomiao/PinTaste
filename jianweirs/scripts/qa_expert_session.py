"""Create/revoke one local UI acceptance session without printing credentials."""
from pathlib import Path
import json
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from wuyu_harness.auth_portal import PortalDB,COOKIE

path=ROOT/'output/playwright/.expert-v4-session.json'
db=PortalDB(ROOT/'outputs/portal')
if '--revoke' in sys.argv:
    if path.is_file():
        state=json.loads(path.read_text('utf-8'))
        for cookie in state.get('cookies',[]):
            if cookie['name']==COOKIE:db.logout(cookie['value'])
        path.unlink()
    print('Acceptance session revoked')
else:
    user=next(u for u in db.users() if u['role']=='admin' and u['active'])
    token,_=db.issue_session(user['id'])
    path.parent.mkdir(exist_ok=True,parents=True)
    path.write_text(json.dumps({'cookies':[{'name':COOKIE,'value':token,'domain':'127.0.0.1','path':'/','httpOnly':True,'secure':False,'sameSite':'Strict','expires':-1}],'origins':[]}),encoding='utf-8')
    print('Local UI acceptance session ready')
