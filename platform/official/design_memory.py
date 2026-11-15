"""Optional owner-only design preferences, using the work authority's lexical RAG."""
import re
import time
from . import store as db


def retrieve(uid, question):
    if not re.search(r'ppt|幻灯片|汇报设计|版式|排版', question, re.I):
        return []
    from . import integrated_identity as identity, identity_bridge as bridge
    try:
        user=identity.public_user(uid)
        if not user['permissions'].get('work'):
            return []
        session=db.query('SELECT hash FROM sessions WHERE user_id=? AND expires>? ORDER BY expires DESC LIMIT 1',(uid,time.time()),one=True)
        if not session:
            return []
        token=bridge.work_token(session['hash'],timeout=3)
        response=bridge.request('POST','/api/design-memory/retrieve',{'query':question[:4000]},token=token,timeout=3)
        result=response.get('data',{})
        if not isinstance(result,dict) or not isinstance(result.get('rules'),list):
            return []
        return [{key:rule[key] for key in ('id','rule','rule_set_id')} for rule in result['rules'][:8]
            if isinstance(rule,dict) and rule.get('scope')=='personal' and all(isinstance(rule.get(key),str) and len(rule[key])<=500 for key in ('id','rule','rule_set_id'))]
    except identity.IdentityError:
        # Generation remains usable if the optional work RAG is unavailable;
        # never use another identity or copy rules from a different user.
        return []
