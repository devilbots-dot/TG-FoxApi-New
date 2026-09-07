/**
 * TgFox Blue Console routing shell. Every financial and marketplace surface is
 * a focused lazy workspace; the dashboard contains server-provided data only.
 */
import { lazy, Suspense, useEffect, useState } from "react";
import { AlertTriangle, ChevronDown, CircleDollarSign, Copy, Grid2X2, HandCoins, History, KeyRound, LayoutDashboard, LoaderCircle, Menu, Settings, ShieldCheck, Smartphone, UserRound, X } from "lucide-react";
import { toast } from "sonner";
import { TgFoxMark } from "@/components/TgFoxMark";
import ErrorBoundary from "@/components/ErrorBoundary";
import { clearStoredApiKey, hasStoredApiKey, TgFoxApiError, type Country, type Profile, type RecordResponse, tgfoxApi } from "@/lib/api";
import { haptic, prepareTelegram } from "@/lib/tg";

type View = "dashboard" | "buy" | "sell" | "wallet" | "deposit" | "withdraw" | "transactions" | "community" | "profile";
type LoadError = { message: string; status?: number; requestId?: string };

const DashboardWorkspace = lazy(() => import("@/components/DashboardWorkspace").then((module) => ({ default: module.DashboardWorkspace })));
const BuyAccountWorkspace = lazy(() => import("@/components/BuyAccountWorkspace").then((module) => ({ default: module.BuyAccountWorkspace })));
const SellAccountWorkspace = lazy(() => import("@/components/SellAccountWorkspace").then((module) => ({ default: module.SellAccountWorkspace })));
const WalletWorkspace = lazy(() => import("@/components/WalletWorkspace").then((module) => ({ default: module.WalletWorkspace })));
const DepositWorkspace = lazy(() => import("@/components/DepositWorkspace").then((module) => ({ default: module.DepositWorkspace })));
const WithdrawalWorkspace = lazy(() => import("@/components/WithdrawalWorkspace").then((module) => ({ default: module.WithdrawalWorkspace })));
const ActivityWorkspace = lazy(() => import("@/components/ActivityWorkspace").then((module) => ({ default: module.ActivityWorkspace })));
const CommunityWorkspace = lazy(() => import("@/components/CommunityWorkspace").then((module) => ({ default: module.CommunityWorkspace })));

const menu: Array<{ id: View; label: string; icon: typeof LayoutDashboard }> = [
  { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { id: "buy", label: "Buy Numbers", icon: Smartphone },
  { id: "sell", label: "Sell Account", icon: HandCoins },
  { id: "wallet", label: "Add Fund", icon: CircleDollarSign },
  { id: "transactions", label: "Transactions", icon: History },
  { id: "community", label: "Account tools", icon: Grid2X2 },
  { id: "profile", label: "Settings", icon: Settings },
];

export default function Home() {
  const [view, setView] = useState<View>("dashboard");
  const [drawer, setDrawer] = useState(false);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [countries, setCountries] = useState<Country[]>([]);
  const [records, setRecords] = useState<RecordResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<LoadError | null>(null);
  const [apiKeySubmitting, setApiKeySubmitting] = useState(false);
  const hasApiKeySession = hasStoredApiKey();
  const refresh = async (verifiedProfile?: Profile) => {
    const me = verifiedProfile ?? await tgfoxApi.profile();
    setProfile(me);
    const [market, history] = await Promise.allSettled([tgfoxApi.countries(), tgfoxApi.records()]);
    if (market.status === "fulfilled") setCountries(market.value.countries);
    else toast.error("Live inventory is temporarily unavailable. You can retry from the dashboard.");
    if (history.status === "fulfilled") setRecords(history.value);
    else toast.error("Order history is temporarily unavailable. You can retry later.");
  };
  useEffect(() => { prepareTelegram(); refresh().catch((error: unknown) => { const message = error instanceof Error ? error.message : "Could not load your TgFox account."; const failure = error instanceof TgFoxApiError ? { message, status: error.status, requestId: error.requestId } : { message }; setLoadError(failure); toast.error(failure.requestId ? `${message} · Request ${failure.requestId}` : message); }).finally(() => setLoading(false)); }, []);

  // Keep the marketplace live after the Mini App is already open. The backend
  // endpoint reads MongoDB inventory directly, so newly uploaded admin stock
  // becomes visible without requiring the user to close/reopen the Mini App.
  useEffect(() => {
    if (view !== "dashboard" && view !== "buy") return;
    let cancelled = false;
    const poll = async () => {
      try {
        const market = await tgfoxApi.countries();
        if (!cancelled) setCountries(market.countries);
      } catch {
        // Keep the last known market on transient polling failures.
      }
    };
    const interval = window.setInterval(poll, 5000);
    return () => { cancelled = true; window.clearInterval(interval); };
  }, [view]);

  const navigate = (next: View) => {
    haptic("light");
    setView(next);
    setDrawer(false);
    // Force an immediate market refresh when the user opens Buy Accounts.
    if (next === "buy") {
      tgfoxApi.countries().then((market) => setCountries(market.countries)).catch(() => undefined);
    }
  };
  const copy = async (value: string) => { try { await navigator.clipboard.writeText(value); toast.success("Copied"); } catch { toast.error("Copy is unavailable in this browser."); } };
  const loginWithApiKey = async (apiKey: string) => { setApiKeySubmitting(true); setLoadError(null); setLoading(true); try { const me = await tgfoxApi.login(apiKey); await refresh(me); toast.success("API key verified. Account loaded."); } catch (error: unknown) { const message = error instanceof Error ? error.message : "Could not verify API key."; const failure = error instanceof TgFoxApiError ? { message, status: error.status, requestId: error.requestId } : { message }; setProfile(null); setLoadError(failure); toast.error(failure.requestId ? `${message} · Request ${failure.requestId}` : message); } finally { setApiKeySubmitting(false); setLoading(false); } };
  const logout = () => { clearStoredApiKey(); setProfile(null); setCountries([]); setRecords(null); setLoadError({ message: "Sign in with your TgFox API key to continue.", status: 401 }); setView("dashboard"); };
  if (loading) return <div className="grid min-h-screen place-items-center bg-[#F7F9FD]"><div className="flex flex-col items-center gap-3"><TgFoxMark size={56} label={false} /><LoaderCircle className="animate-spin text-[#1769F5]" /><p className="text-sm font-medium text-[#7B8497]">{hasApiKeySession ? "Loading secure API-key session" : "Preparing secure account access"}</p></div></div>;
  if (loadError || !profile) return <ApiKeyAccessScreen error={loadError || { message: "Sign in with your TgFox API key.", status: 401 }} submitting={apiKeySubmitting} onSubmit={loginWithApiKey} />;
  return <main className="min-h-screen bg-[#F7F9FD] text-[#202633]"><div className="mx-auto min-h-screen max-w-[1120px] bg-[#F7F9FD]"><TopBar profile={profile} onMenu={() => setDrawer(true)} onProfile={() => navigate("profile")} /><div className="px-4 pb-10 pt-4 sm:px-6"><Suspense fallback={<WorkspaceLoader />}>{view === "dashboard" && <ErrorBoundary resetKey={view} fallback={<WorkspaceRecovery onBack={() => navigate("dashboard")} />}><DashboardWorkspace profile={profile} countries={countries} records={records} onNavigate={navigate} /></ErrorBoundary>}{view === "buy" && <ErrorBoundary resetKey={view} fallback={<WorkspaceRecovery onBack={() => navigate("dashboard")} />}><BuyAccountWorkspace countries={countries} balance={profile.balance} onBack={() => navigate("dashboard")} onOrderCreated={() => refresh().catch(() => undefined)} /></ErrorBoundary>}{view === "sell" && <ErrorBoundary resetKey={view} fallback={<WorkspaceRecovery onBack={() => navigate("dashboard")} />}><SellAccountWorkspace botUsername={profile.bot_username} onBack={() => navigate("dashboard")} /></ErrorBoundary>}{view === "wallet" && <ErrorBoundary resetKey={view} fallback={<WorkspaceRecovery onBack={() => navigate("dashboard")} />}><WalletWorkspace onOpenDeposit={() => navigate("deposit")} onOpenWithdrawal={() => navigate("withdraw")} /></ErrorBoundary>}{view === "deposit" && <ErrorBoundary resetKey={view} fallback={<WorkspaceRecovery onBack={() => navigate("wallet")} />}><DepositWorkspace onBack={() => navigate("wallet")} /></ErrorBoundary>}{view === "withdraw" && <ErrorBoundary resetKey={view} fallback={<WorkspaceRecovery onBack={() => navigate("wallet")} />}><WithdrawalWorkspace onBack={() => navigate("wallet")} /></ErrorBoundary>}{view === "transactions" && <ErrorBoundary resetKey={view} fallback={<WorkspaceRecovery onBack={() => navigate("dashboard")} />}><ActivityWorkspace records={records} liveOrder={null} onCopy={copy} /></ErrorBoundary>}{view === "community" && <ErrorBoundary resetKey={view} fallback={<WorkspaceRecovery onBack={() => navigate("dashboard")} />}><CommunityWorkspace /></ErrorBoundary>}{view === "profile" && <ProfileView profile={profile} onCopy={copy} onLogout={logout} />}</Suspense></div></div><SideDrawer open={drawer} active={view} profile={profile} onClose={() => setDrawer(false)} onNavigate={navigate} /></main>;
}

function WorkspaceLoader() { return <div className="grid min-h-[280px] place-items-center"><div className="flex items-center gap-2 text-sm font-medium text-[#7B8497]"><LoaderCircle size={18} className="animate-spin text-[#1769F5]" />Loading secure workspace</div></div>; }
function WorkspaceRecovery({ onBack }: { onBack: () => void }) { return <div className="grid min-h-[280px] place-items-center"><div className="max-w-sm text-center"><div className="mx-auto grid h-12 w-12 place-items-center rounded-full bg-[#FFF4E5] text-[#D97706]"><AlertTriangle size={22} /></div><h2 className="mt-4 text-base font-bold text-[#202633]">Workspace is temporarily unavailable</h2><p className="mt-2 text-sm leading-6 text-[#6F7A8D]">Your account data and recent actions are safe. Return to the dashboard and try again shortly.</p><button onClick={onBack} className="blue-button pressable mt-5 h-10 rounded-xl px-4 text-sm font-semibold">Return to dashboard</button></div></div>; }
function TopBar({ profile, onMenu, onProfile }: { profile: Profile; onMenu: () => void; onProfile: () => void }) { return <header className="safe-top sticky top-0 z-30 flex h-[68px] items-center justify-between border-b border-[#E6EAF1] bg-white/95 px-4 backdrop-blur sm:px-6"><div className="flex items-center gap-3"><button onClick={onMenu} className="pressable grid h-9 w-9 place-items-center rounded-lg text-[#475166] hover:bg-[#F1F5FB]" aria-label="Open menu"><Menu size={20} /></button><TgFoxMark size={36} /></div><button onClick={onProfile} className="pressable flex items-center gap-2 rounded-lg border border-[#E5EAF3] bg-white px-2 py-1.5" aria-label="Open settings"><span className="grid h-6 w-6 place-items-center rounded-full bg-[#1769F5] text-[10px] font-bold text-white">{profile.user.first_name.slice(0, 1).toUpperCase() || "T"}</span><ChevronDown size={13} className="text-[#6D7688]" /></button></header>; }
function ApiKeyAccessScreen({ error, submitting, onSubmit }: { error: LoadError; submitting: boolean; onSubmit: (apiKey: string) => Promise<void> }) { const [apiKey, setApiKey] = useState(""); return <main className="grid min-h-screen place-items-center bg-[#F7F9FD] px-6 text-center"><div className="thin-card max-w-sm rounded-2xl p-7"><div className="mx-auto grid h-14 w-14 place-items-center rounded-full bg-[#EAF2FF] text-[#1769F5]"><ShieldCheck size={28} /></div><TgFoxMark className="mt-4 justify-center" size={42} /><h1 className="mt-5 text-xl font-bold">Sign in with API key</h1><p className="mt-3 text-sm leading-6 text-[#707A8B]">{error.message}</p><form className="mt-5 space-y-3 text-left" onSubmit={(event) => { event.preventDefault(); void onSubmit(apiKey); }}><label className="block text-[11px] font-semibold text-[#58657B]">Your TgFox API key<input value={apiKey} onChange={(event) => setApiKey(event.target.value)} type="password" autoComplete="off" autoCapitalize="none" spellCheck={false} placeholder="tg_..." className="mt-2 h-11 w-full rounded-xl border border-[#DDE5F1] bg-white px-3 text-sm outline-none focus:border-[#1769F5]" /></label><button disabled={submitting || !apiKey.trim()} className="blue-button pressable flex h-11 w-full items-center justify-center gap-2 rounded-xl text-sm font-semibold disabled:opacity-40">{submitting && <LoaderCircle size={16} className="animate-spin" />}Verify API key</button></form><p className="mt-4 rounded-xl bg-[#F4F8FF] p-3 text-xs leading-5 text-[#4C638F]">Get your key from the bot’s <b>Extra → API Key</b> option. The key is sent only in a secure request header and is kept only until this Mini App tab closes.</p>{error.requestId && <p className="mt-3 text-[10px] font-semibold tracking-wide text-[#7B8497]">Request code: {error.requestId}</p>}</div></main>; }
function ProfileView({ profile, onCopy, onLogout }: { profile: Profile; onCopy: (value: string) => void; onLogout: () => void }) { return <section className="panel-enter"><h1 className="text-[22px] font-bold">Settings</h1><p className="mt-1 text-xs text-[#8A93A4]">Profile and secure account information.</p><div className="thin-card mt-5 rounded-2xl p-5"><div className="flex items-center gap-3"><div className="grid h-12 w-12 place-items-center rounded-full bg-[#1769F5] text-lg font-bold text-white">{profile.user.first_name.slice(0, 1).toUpperCase() || "T"}</div><div><p className="text-sm font-bold">{profile.user.first_name || "TgFox User"}</p><p className="mt-1 text-[10px] text-[#8A93A4]">@{profile.user.username || "tgfox"}</p></div></div><div className="mt-5 divide-y divide-[#E9EDF3] border-t border-[#E9EDF3]"><ProfileLine icon={<UserRound size={17} />} label="Telegram ID" value={String(profile.user.id)} onClick={() => onCopy(String(profile.user.id))} /><ProfileLine icon={<KeyRound size={17} />} label="API access" value="Enabled" /><ProfileLine icon={<ShieldCheck size={17} />} label="Security status" value="Verified" /></div><button onClick={onLogout} className="mt-5 w-full rounded-xl border border-[#F4CCD1] bg-[#FFF6F7] py-2.5 text-xs font-semibold text-[#C94658]">Sign out from this device</button></div></section>; }
function ProfileLine({ icon, label, value, onClick }: { icon: React.ReactNode; label: string; value: string; onClick?: () => void }) { return <button onClick={onClick} className="flex w-full items-center gap-3 py-4 text-left"><span className="text-[#1769F5]">{icon}</span><span className="flex-1 text-xs font-medium">{label}</span><span className="text-xs font-semibold text-[#737D8F]">{value}</span>{onClick && <Copy size={14} className="text-[#1769F5]" />}</button>; }
function SideDrawer({ open, active, profile, onClose, onNavigate }: { open: boolean; active: View; profile: Profile; onClose: () => void; onNavigate: (view: View) => void }) { if (!open) return null; return <div className="fixed inset-0 z-50 bg-[#162033]/25 backdrop-blur-[1px]"><aside className="panel-enter safe-top h-full w-[min(82vw,320px)] bg-white p-4 shadow-2xl"><div className="flex items-center justify-between"><TgFoxMark size={44} /><button onClick={onClose} className="pressable grid h-9 w-9 place-items-center rounded-lg bg-[#F4F6FA] text-[#667184]" aria-label="Close menu"><X size={18} /></button></div><div className="mt-6 rounded-xl bg-[#F4F8FF] p-3"><p className="text-xs font-bold">{profile.user.first_name || "TgFox User"}</p><p className="mt-1 text-[10px] text-[#758196]">ID: {profile.user.id}</p></div><nav className="mt-5 space-y-1">{menu.map((item) => { const Icon = item.icon; const selected = active === item.id; return <button key={item.id} onClick={() => onNavigate(item.id)} className={`pressable flex w-full items-center gap-3 rounded-lg px-3 py-3 text-left text-sm ${selected ? "bg-[#EAF2FF] font-semibold text-[#1769F5]" : "text-[#525E70] hover:bg-[#F6F8FB]"}`}><Icon size={17} />{item.label}</button>; })}</nav></aside></div>; }
