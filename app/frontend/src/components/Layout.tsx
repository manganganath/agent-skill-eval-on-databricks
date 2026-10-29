import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { api } from "../api/client";
import type { Me } from "../api/types";
import { useAsync } from "../hooks/useAsync";

// Four tiles, one filled: the catalog grid and the eval matrix in one mark. Decorative only.
function BrandMark() {
  return (
    <svg className="brand-mark" viewBox="0 0 24 24" width="24" height="24" aria-hidden="true" focusable="false">
      <rect x="3" y="3" width="8" height="8" rx="2" />
      <rect x="13" y="3" width="8" height="8" rx="2" opacity="0.3" />
      <rect x="3" y="13" width="8" height="8" rx="2" opacity="0.3" />
      <rect x="13" y="13" width="8" height="8" rx="2" opacity="0.6" />
    </svg>
  );
}

// The signed-in user. If the lookup fails the header simply shows no user; pages report their own errors.
function SignedInUser() {
  const me = useAsync<Me>(() => api.me(), []);
  if (!me.data) return null;
  const { user, is_reviewer, is_author } = me.data;
  return (
    <div className="app-user">
      <span className="avatar" aria-hidden="true">{user.charAt(0).toUpperCase()}</span>
      <span className="app-user-name" title={user}><span className="visually-hidden">Signed in as </span>{user}</span>
      {(is_reviewer || is_author) && (
        <Link to="/access" className="badge badge-accent" title="Who can do what">{is_reviewer ? "Reviewer" : "Author"}</Link>
      )}
    </div>
  );
}

export function Layout() {
  const { pathname } = useLocation();
  // A skill page (live, candidate or draft) and the draft upload belong to the catalog, so the tab stays marked.
  const inCatalog = ["/skills/", "/candidates/", "/drafts/"].some((p) => pathname.startsWith(p));
  return (
    <>
      <a className="skip-link" href="#main">Skip to content</a>
      <header className="app-header">
        <div className="app-header-inner">
          <span className="brand"><BrandMark />Skill Hub</span>
          <nav aria-label="Main" className="app-nav">
            <NavLink to="/" end className={({ isActive }) => (isActive || inCatalog ? "active" : undefined)}>
              Catalog
            </NavLink>
            <NavLink to="/matrix">Eval matrix</NavLink>
            <NavLink to="/run">Run eval</NavLink>
            <NavLink to="/access">Access</NavLink>
          </nav>
          <SignedInUser />
        </div>
      </header>
      <main id="main" tabIndex={-1}>
        <Outlet />
      </main>
    </>
  );
}
