import { useEffect } from 'react';

/**
 * v1.4.2 #100: guard a form's unsaved changes during browser-level
 * exits (close tab, refresh, navigate the address bar away). The
 * initial v1.4.2 #94 implementation also tried to guard intra-SPA
 * navigation via react-router's `useBlocker`, but useBlocker requires
 * the data-router setup (`createBrowserRouter` + `RouterProvider`).
 * The admin panel uses the older declarative `<BrowserRouter>` +
 * `<Routes>` pattern, so useBlocker threw "useBlocker must be used
 * within a data router" on every Settings mount and the ErrorBoundary
 * caught it -- the page was 100% broken.
 *
 * Reverted to browser-level only: this hook installs a
 * `beforeunload` listener that sets preventDefault + returnValue when
 * the caller's `isDirty` flag is true. Matches the behaviour that
 * existed before #94. Intra-SPA navigation (sidebar clicks while a
 * form is dirty) is NOT guarded -- that is a larger design decision
 * (router migration, cross-component context/events, or a
 * Sidebar-owned guard) deferred to v1.5.
 *
 * Return: void. No `confirmNavigation` helper is exposed; v1.4.2
 * has no caller that can actually wrap its own navigation sites
 * (Settings' only nav-out paths are in the Sidebar, a sibling).
 * Adding the helper with no call sites would be dead API.
 *
 * Usage:
 *   const [dirty, setDirty] = useState(false);
 *   useDirtyFormGuard(dirty);
 */
export function useDirtyFormGuard(isDirty, _message) {
  useEffect(() => {
    function handleBeforeUnload(e) {
      if (!isDirty) return;
      e.preventDefault();
      // Legacy browsers require a truthy returnValue string; modern
      // browsers ignore the custom text and show their own copy.
      e.returnValue = '';
    }
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [isDirty]);
}
