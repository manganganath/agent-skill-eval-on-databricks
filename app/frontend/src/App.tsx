import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { EmptyState } from "./components/EmptyState";
import Catalog from "./pages/Catalog";
import SkillDetail from "./pages/SkillDetail";
import Matrix from "./pages/Matrix";
import RunEval from "./pages/RunEval";
import Switch from "./pages/Switch";

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Catalog />} />
          <Route path="skills/:fqn" element={<SkillDetail />} />
          <Route path="skills/:fqn/switch" element={<Switch />} />
          <Route path="matrix" element={<Matrix />} />
          <Route path="run" element={<RunEval />} />
          <Route path="*" element={<EmptyState title="Page not found" />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
