import {t,getUiLanguage,useI18n} from './i18n.mjs';
import React,{useEffect,useState} from 'react';
import {api} from './api';
import {Logo} from './ChatParts';
import UserSettings from './UserSettings';
import Admin from './Admin';
import {usePreferences} from './preferences';
import {canOpenSystemManagement,settingsDestination,notifyWorkSettings} from './settingsNavigation.mjs';
import './shared-settings.css';

// This entry never mounts the QA app, its conversation loader or polling loops.
export default function SharedSettingsEntry(){
 useI18n();

 const [user,setUser]=useState(undefined),[failure,setFailure]=useState(''),[destination,setDestination]=useState(null),[account,setAccount]=useState(false);
 useEffect(()=>{let live=true;api('/me').then(value=>{if(live){setUser(value);const target=settingsDestination(window.location.search,value);setDestination(target);}}).catch(error=>{if(live){setUser(null);setFailure(error.status===401?"登录状态已失效，请重新登录。":error.message);if(error.status===401)notifyWorkSettings('wuyu.settings.auth-required');}});return()=>{live=false;};},[]);
 const sharedPrefs=usePreferences(user);
 const close=()=>notifyWorkSettings('wuyu.settings.close');
 const updateUser=value=>{setUser(value);notifyWorkSettings('wuyu.settings.user-updated');};
 const openAdmin=tab=>{if(canOpenSystemManagement(user)){setAccount(false);setDestination({view:'admin',tab:tab==='quota'?'quota':'overview'});}};
 async function logout(){try{await api('/auth/logout',{method:'POST',body:'{}'});setUser(null);setFailure("已退出登录。");notifyWorkSettings('wuyu.settings.auth-required');}catch(error){setFailure(error.message);}}
 if(user===undefined)return <main className="shared-settings-loading"><Logo/><p>{t("正在加载系统设置…")}</p></main>;
 if(!user)return <main className="shared-settings-loading"><Logo/><p role="alert">{t(failure)}</p><button className="secondary" onClick={()=>notifyWorkSettings('wuyu.settings.auth-required')}>{t("重新登录")}</button><button className="secondary" onClick={close}>{t("关闭")}</button></main>;
 const dialog=<UserSettings user={user} initialTab={account||destination.tab} prefs={sharedPrefs} context="work" onUser={updateUser} onClose={account?()=>setAccount(false):close} onLogout={logout} onAdmin={openAdmin}/>;
 return <div className="shared-settings-entry">{destination.view==='admin'&&canOpenSystemManagement(user)?<><Admin key={destination.tab} user={user} initialTab={destination.tab} context="work" onSettings={()=>setAccount('account')} onClose={close} onSaved={()=>notifyWorkSettings('wuyu.settings.user-updated')}/>{account&&dialog}</>:dialog}</div>;
}
