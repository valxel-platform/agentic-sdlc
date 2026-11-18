import { render, type RenderResult } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactElement } from "react";

/** Render inside a MemoryRouter. `path` is the route pattern the element
 * mounts at (needed for pages that read useParams); `route` is the
 * starting URL. */
export function renderWithRouter(
  ui: ReactElement,
  opts: { route?: string; path?: string } = {},
): RenderResult {
  const { route = "/", path = "*" } = opts;
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route path={path} element={ui} />
      </Routes>
    </MemoryRouter>,
  );
}
