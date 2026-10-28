import { NavLink, Outlet } from "react-router-dom";

export function Layout() {
  return (
    <>
      <a className="skip-link" href="#main">Skip to content</a>
      <header className="app-header">
        <strong>Skill Hub</strong>
        <nav aria-label="Main">
          <NavLink to="/">Catalog</NavLink>
          <NavLink to="/matrix">Eval matrix</NavLink>
          <NavLink to="/run">Run eval</NavLink>
        </nav>
      </header>
      <main id="main" tabIndex={-1}>
        <Outlet />
      </main>
    </>
  );
}
