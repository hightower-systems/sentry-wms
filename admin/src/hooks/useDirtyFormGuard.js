import { useEffect } from 'react';
import { useBlocker } from 'react-router-dom';

/**
 * v1.4.2 #94: guard intra-app navigation when a form has unsaved
 * changes. Uses react-router v7's `useBlocker` to intercept any
 * Link / NavLink / navigate() call away from the current route and
 * prompt the operator before the route actually changes.
 *
 * `beforeunload` already covers window-close and full reload, so this
 * hook only needs to handle in-app links (sidebar, breadcrumb, browser
 * back inside the SPA).
 *
 * Usage:
 *   const [dirty, setDirty] = useState(false);
 *   useDirtyFormGuard(dirty);
 *   ...
 *
 * Scope: any page that wants the same behaviour. Settings is the only
 * caller in v1.4.2; if other pages grow form state in v1.5+ they can
 * drop the hook in the same way.
 */
export function useDirtyFormGuard(isDirty, message = 'You have unsaved changes. Leave anyway?') {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      isDirty && currentLocation.pathname !== nextLocation.pathname,
  );

  useEffect(() => {
    if (blocker.state !== 'blocked') return;
    // eslint-disable-next-line no-alert
    const proceed = window.confirm(message);
    if (proceed) {
      blocker.proceed();
    } else {
      blocker.reset();
    }
  }, [blocker, message]);
}
