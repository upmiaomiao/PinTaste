/** Brand assets from the original Wuyu AI platform; layout owns typography. */
import type { PropsLocale } from '@deepseek-ai/dsh-client-ui-slots'

export function WuyuBrandName({ t }: PropsLocale<'bananaPanel'>) { return <span>{t('work.brand')}</span> }
export function WuyuBrandMark({size,className}:{size:number;className?:string|undefined}) {return <img src="/login/assets/wuyu-logo.png" alt="" width={size} height={size} className={className} style={{objectFit:'contain'}} />}
