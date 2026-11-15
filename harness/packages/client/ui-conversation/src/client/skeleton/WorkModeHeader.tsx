import type { ConversationSlotProps } from '../contract/slots.ts'
import type { ReactNode } from 'react'
import css from './WorkModeHeader.module.css'

/** Work identity and account controls remain within the work application. */
export function WorkModeHeader({ t, account }: { t: ConversationSlotProps['t']; account?: ReactNode }) {
  return (
    <header className={css.root} data-wuyu-mode="work">
      <div className={css.identity}>
        <span className={css.name}>{t('work.name')}</span>
        <span className={css.domain}>{t('work.domain')}</span>
      </div>
      <div className={css.utilities}>{account}</div>
    </header>
  )
}
