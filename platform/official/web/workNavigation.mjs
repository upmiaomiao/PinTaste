// Browser origin selects the environment; all work navigation stays same-origin.
export function workDestination(input, origin) {
  const target = new URL(input || '/work', origin);
  if (target.origin !== origin || !['/work', '/work/admin'].includes(target.pathname) || target.hash) {
    throw Error('工作入口地址无效。');
  }
  return target;
}

export function workSsoDestination(input, origin) {
  const target = new URL(input);
  if (target.origin !== origin || target.pathname !== '/work-auth/sso' || target.hash) {
    throw Error('工作入口地址无效。');
  }
  return target;
}
