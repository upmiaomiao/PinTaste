import { clientBundle } from '../tsdown.client.ts'

/**
 * The published harness (0.1.1-rc.2) predates the `client/store` split: its
 * module table seeds `@deepseek-ai/dsh-client-runtime` for `defineStore`, not
 * `@deepseek-ai/dsh-client-store`. Inline the store engine privately instead —
 * the handle is a duck-typed object (`create()` yields getSnapshot/subscribe/
 * actions), safe to duplicate — so the loader never requires a module the
 * running shell cannot answer.
 */
const STORE = '@deepseek-ai/dsh-client-store'

const base = clientBundle('@deepseek-ai/dsh-client-ui-banana-panel', ['src/index.ts'])

export default (inlineConfig: { env?: unknown }) =>
  base(inlineConfig).map((config) => {
    if (config.name !== '@deepseek-ai/dsh-client-ui-banana-panel/client') return config
    const neverBundle = config.deps?.neverBundle as (specifier: string) => boolean
    const alwaysBundle = config.deps?.alwaysBundle as (specifier: string) => boolean
    return {
      ...config,
      deps: {
        ...config.deps,
        neverBundle: (specifier: string) => specifier !== STORE && neverBundle(specifier),
        alwaysBundle: (specifier: string) => specifier === STORE || alwaysBundle(specifier),
      },
    }
  })
