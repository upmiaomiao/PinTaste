/** A conversation groups task receipts; it never merges users or drops a turn. */
export const conversationId = task=>task?.conversation_id||task?.id||'';
export function mergeTurns(previous=[], incoming=[]) {
  const records=new Map();
  for(const turn of [...previous,...incoming])if(turn?.id)records.set(turn.id,{...records.get(turn.id),...turn});
  return [...records.values()].sort((a,b)=>{
    const aRevision=Number(a.core_report?.revision),bRevision=Number(b.core_report?.revision);
    if(Number.isFinite(aRevision)&&Number.isFinite(bRevision)&&aRevision!==bRevision)return aRevision-bRevision;
    if(a.parent_task_id===b.id)return 1;
    if(b.parent_task_id===a.id)return -1;
    const aTime=Date.parse(a.created_at),bTime=Date.parse(b.created_at);
    return Number.isFinite(aTime)&&Number.isFinite(bTime)?aTime-bTime:0;
  });
}
export function upsertConversation(summaries=[], task, title='') {
  const id=conversationId(task);
  const old=summaries.find(row=>conversationId(row)===id);
  const entry={...old,...task,id:task.id,latest_task_id:task.id,conversation_id:id,
    turn_count:task.turn_count||((old?.turn_count||1)+(old&&old.id!==task.id?1:0)),
    title:old?.title||title||task.conversation_title||task.title||task.task?.objective||task.objective||'新的对话',
    objective:old?.objective||title||task.conversation_title||task.task?.objective||task.objective||'',
    created_at:old?.created_at||task.created_at};
  return old?summaries.map(row=>conversationId(row)===id?entry:row):[entry,...summaries];
}
