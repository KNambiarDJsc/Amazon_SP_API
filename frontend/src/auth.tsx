import { createContext, useContext, useEffect, useSyncExternalStore, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Navigate, useLocation } from "react-router-dom";
import { credentials } from "./api/client";

const AuthContext = createContext<{ key: string | null; signOut: () => void }>({
  key: null,
  signOut: () => {},
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const key = useSyncExternalStore(credentials.subscribe, credentials.get);
  const qc = useQueryClient();
  // Whether the user signed out or the server rejected the key, nothing from this seller may
  // linger in memory for the next person.
  useEffect(() => {
    if (!key) qc.clear();
  }, [key, qc]);
  const signOut = () => credentials.clear();
  return <AuthContext.Provider value={{ key, signOut }}>{children}</AuthContext.Provider>;
}

export const useAuth = () => useContext(AuthContext);

export function RequireAuth({ children }: { children: ReactNode }) {
  const { key } = useAuth();
  const location = useLocation();
  if (!key) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  return <>{children}</>;
}
