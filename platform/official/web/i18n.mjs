import {useSyncExternalStore} from 'react';
import {getUiLanguage,subscribeUiLanguage,t} from './uiLanguage.mjs';
export * from './uiLanguage.mjs';
export function useI18n(){
 const language=useSyncExternalStore(subscribeUiLanguage,getUiLanguage,getUiLanguage);
 return {language,t};
}
