import { NavLink, Outlet } from "react-router-dom";
import { useMe } from "../api/queries";
import { useAuth } from "../auth";
import { Badge, Button } from "./ui";

const link = ({ isActive }: { isActive: boolean }) =>
  `rounded-md px-3 py-1.5 text-sm font-medium ${isActive ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-100"}`;

export function Layout() {
  const { signOut } = useAuth();
  const me = useMe();
  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl flex-col gap-2 px-4 py-3 md:flex-row md:items-center md:justify-between md:gap-3">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <span className="whitespace-nowrap text-base font-semibold tracking-tight">
              Eligibility Intelligence
            </span>
            <nav aria-label="Main" className="flex flex-wrap gap-1">
              <NavLink to="/" end className={link}>
                Jobs
              </NavLink>
              <NavLink to="/jobs/new" className={link}>
                New check
              </NavLink>
              <NavLink to="/amazon" className={link}>
                Amazon connection
              </NavLink>
            </nav>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-sm text-slate-600 md:gap-3">
            {me.data && (
              <>
                <span className="hidden lg:inline">{me.data.name}</span>
                <Badge tone={me.data.amazon_mode === "live" ? "green" : "amber"}>
                  {me.data.amazon_mode === "live" ? "Live Amazon data" : "Demo data (mock)"}
                </Badge>
                <Badge tone={me.data.connected_to_amazon ? "green" : "slate"}>
                  {me.data.connected_to_amazon ? "Amazon connected" : "Amazon not connected"}
                </Badge>
              </>
            )}
            <Button variant="ghost" onClick={signOut}>
              Sign out
            </Button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}
