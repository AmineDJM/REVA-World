import { lazy, Suspense } from "react";
import { Navigate, NavLink, Route, Routes, useLocation } from "react-router";
import { V3Loading } from "./components/States";
import { V3_SCREENS } from "./screens";
import "./v3.css";

const HomePage = lazy(() => import("./pages/HomePage"));
const NeuronesPage = lazy(() => import("./pages/NeuronesPage"));
const CheminPage = lazy(() => import("./pages/CheminPage"));
const FrisePage = lazy(() => import("./pages/FrisePage"));
const AtlasPage = lazy(() => import("./pages/AtlasPage"));
const DossierPage = lazy(() => import("./pages/DossierPage"));
const DemanderPage = lazy(() => import("./pages/DemanderPage"));
const RelationPage = lazy(() => import("./pages/RelationPage"));
const CausesPage = lazy(() => import("./causal/CausesPage"));
const FichePage = lazy(() => import("./pages/FichePage"));
const PrevisionsPage = lazy(() => import("./pages/PrevisionsPage"));
const DecisionPage = lazy(() => import("../v5/decision/DecisionPage")); // V5 decision engine (PRED-ENGINE)

/** All V3 screens under /v3/* (one lazy chunk per screen), with their own sub-navigation. */
export default function V3App() {
  const { pathname } = useLocation();
  return (
    <div className="v3" lang="fr">
      <nav className="v3-subnav" aria-label="Planète (V3)">
        <ul>
          <li>
            <NavLink
              to="/v3"
              end
              className={({ isActive }) => (isActive ? "active" : undefined)}
            >
              Planète
            </NavLink>
          </li>
          {V3_SCREENS.map((s) => {
            const base = s.to.split("?")[0]!.split("/").slice(0, 3).join("/");
            const active = pathname.startsWith(base);
            return (
              <li key={s.to}>
                <NavLink
                  to={s.to}
                  className={() => (active ? "active" : "")}
                  aria-current={active ? "page" : undefined}
                >
                  {s.short}
                </NavLink>
              </li>
            );
          })}
        </ul>
      </nav>
      <Suspense fallback={<V3Loading what="de l’écran" />}>
        <Routes>
          <Route index element={<HomePage />} />
          <Route path="neurones" element={<NeuronesPage />} />
          <Route path="fiche" element={<FichePage />} />
          <Route path="chemin" element={<CheminPage />} />
          <Route path="frise" element={<FrisePage />} />
          <Route path="atlas" element={<AtlasPage />} />
          <Route path="pays" element={<Navigate to="/v3/pays/FRA" replace />} />
          <Route path="pays/:iso3" element={<DossierPage />} />
          <Route path="relation" element={<RelationPage />} />
          <Route path="causes" element={<CausesPage />} />
          <Route path="demander" element={<DemanderPage />} />
          <Route path="decision" element={<DecisionPage />} />
          <Route path="previsions" element={<PrevisionsPage />} />
          <Route
            path="*"
            element={
              <p>Écran inconnu. Choisissez un écran dans la navigation.</p>
            }
          />
        </Routes>
      </Suspense>
    </div>
  );
}
