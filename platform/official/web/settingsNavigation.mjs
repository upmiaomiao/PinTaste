export const personalSettingsTabs=['general','account','work','linked','data','about'];
export function canManageWorkQuota(user){return user?.role==='admin'||user?.permissions?.work_quota_admin===true;}
export function canOpenSystemManagement(user){return user?.role==='admin'||canManageWorkQuota(user);}
export function settingsDestination(search,user){
 const params=new URLSearchParams(search),requested=params.get('settings');
 if(requested==='admin'||requested==='quota'){
  if(!canOpenSystemManagement(user))return {view:'settings',tab:'general'};
  return {view:'admin',tab:requested==='quota'||params.get('admin_tab')==='quota'||user?.role!=='admin'?'quota':'overview'};
 }
 return {view:'settings',tab:personalSettingsTabs.includes(requested)?requested:'general'};
}
export function isEmbeddedWorkSettings(search){return new URLSearchParams(search).get('embed')==='work';}
export function qaHistoryItems(items,filter=''){
 return (Array.isArray(items)?items:[]).filter(item=>item?.mode==='chat'&&typeof item.title==='string'&&item.title.includes(filter));
}
export function notifyWorkSettings(type,extra={}){
 if(window.parent!==window)window.parent.postMessage({type,...extra},window.location.origin);
}
export function openEmbeddedWork(path){
 if(!['/work','/work/admin','/work?templates=review'].includes(path))throw Error('工作入口地址无效。');
 notifyWorkSettings('wuyu.settings.open-work',{path});
}
