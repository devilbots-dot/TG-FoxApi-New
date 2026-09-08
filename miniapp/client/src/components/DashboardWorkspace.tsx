/**
 * TgFox dashboard UX: a real-data-only command centre that leads directly to
 * focused Buy, Sell, Deposit, Withdrawal, and Activity journeys.
 */
import { ArrowUpRight, BadgeCheck, CircleDollarSign, Clock3, HandCoins, History, PackageOpen, ShieldCheck, ShoppingBag, Smartphone, WalletCards } from "lucide-react";
import { type Country, type Profile, type RecordResponse } from "@/lib/api";

type DashboardAction = "buy" | "sell" | "wallet" | "transactions";
const cash = (value: number) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 }).format(value || 0);
const date = (value: string) => value ? new Intl.DateTimeFormat("en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value)) : "—";

export function DashboardWorkspace({ profile, countries, records, onNavigate }: { profile: Profile; countries: Country[]; records: RecordResponse | null; onNavigate: (action: DashboardAction) => void }) {
  const availableCountries = countries.filter((country) => country.stock > 0 && !country.disabled && !country.is_full);
  const liveAccounts = availableCountries.reduce((total, country) => total + Math.max(0, country.stock), 0);
  const recentOrders = (records?.purchases || []).slice(0, 3);

  // Rank progress is already calculated by the server. We only display it here.
  const reward = profile.next_reward || { rank: null, goal: 0, progress: 0, remaining: 0 };
  const goal = Math.max(0, Number(reward.goal || 0));
  const progress = Math.max(0, Number(reward.progress || 0));
  const remaining = Math.max(0, Number(reward.remaining || 0));
  const progressPct = goal > 0 ? Math.min(100, Math.round((progress / goal) * 100)) : 100;
  const currentRank = profile.rank || "VIP1";
  const nextRank = reward.rank || null;

  return <section className="panel-enter max-w-5xl">
    {/* Premium account hero: lightweight CSS animation instead of an external video. */}
    <section className="tgfox-vip-hero relative overflow-hidden rounded-[28px] bg-[#173F82] p-5 text-white shadow-[0_18px_40px_rgba(23,105,245,.18)] sm:p-7">
      <style>{`
        @keyframes tgfoxSlowGlow {
          0%, 100% { transform: translate3d(-8%, -4%, 0) scale(1); opacity: .28; }
          50% { transform: translate3d(7%, 5%, 0) scale(1.12); opacity: .48; }
        }
        @keyframes tgfoxLightSweep {
          0% { transform: translateX(-140%) rotate(18deg); opacity: 0; }
          18% { opacity: .10; }
          55% { opacity: .07; }
          100% { transform: translateX(180%) rotate(18deg); opacity: 0; }
        }
        @keyframes tgfoxFloatDot {
          0%, 100% { transform: translate3d(0, 0, 0); opacity: .16; }
          50% { transform: translate3d(0, -12px, 0); opacity: .30; }
        }
        .tgfox-vip-glow { animation: tgfoxSlowGlow 11s ease-in-out infinite; }
        .tgfox-vip-sweep { animation: tgfoxLightSweep 9s ease-in-out infinite; }
        .tgfox-vip-dot { animation: tgfoxFloatDot 6s ease-in-out infinite; }
        @media (prefers-reduced-motion: reduce) {
          .tgfox-vip-glow, .tgfox-vip-sweep, .tgfox-vip-dot { animation: none !important; }
        }
      `}</style>

      <div className="pointer-events-none absolute -right-16 -top-20 h-52 w-52 rounded-full border-[24px] border-white/10" />
      <div className="tgfox-vip-glow pointer-events-none absolute -right-12 bottom-[-100px] h-64 w-64 rounded-full bg-[#79B1FF]/30 blur-3xl" />
      <div className="tgfox-vip-sweep pointer-events-none absolute -left-1/3 top-[-55%] h-[210%] w-24 bg-white/20 blur-2xl" />
      <span className="tgfox-vip-dot pointer-events-none absolute right-[28%] top-10 h-2 w-2 rounded-full bg-white/40" />
      <span className="tgfox-vip-dot pointer-events-none absolute right-[13%] top-24 h-1.5 w-1.5 rounded-full bg-white/35" style={{ animationDelay: "-2s" }} />

      <div className="relative">
        <p className="flex items-center gap-2 text-[10px] font-bold tracking-[.16em] text-[#BFD8FF]"><ShieldCheck size={14} /> VERIFIED TG FOX ACCOUNT</p>

        {/* Greeting and name are intentionally separated so the name stays clean on small phones. */}
        <div className="mt-3">
          <p className="text-[24px] font-bold leading-tight tracking-[-.04em] sm:text-[28px]">Good to see you,</p>
          <h1 className="mt-1 max-w-full truncate text-[20px] font-extrabold leading-tight tracking-[-.035em] text-white sm:text-[23px]">
            {profile.user.first_name || "TgFox user"}<span className="font-bold text-white/75">.</span>
          </h1>
        </div>

        <p className="mt-2 max-w-xl text-xs leading-5 text-white/75">Your live wallet, market availability, and account activity are ready below.</p>

        <div className="mt-5 grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
          <div className="min-w-0 rounded-2xl border border-white/15 bg-white/10 p-4 backdrop-blur-sm">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-[9px] font-bold tracking-[.14em] text-white/65">VIP RANK</p>
                <p className="mt-1 text-lg font-extrabold tracking-tight">{currentRank}</p>
              </div>
              {nextRank ? (
                <div className="text-right">
                  <p className="text-[9px] font-bold tracking-[.12em] text-white/55">NEXT LEVEL</p>
                  <p className="mt-1 text-sm font-bold text-[#D7E7FF]">{nextRank}</p>
                </div>
              ) : (
                <span className="rounded-full bg-white/12 px-3 py-1.5 text-[9px] font-bold tracking-wide text-white/75">MAX RANK</span>
              )}
            </div>

            <div className="mt-3 h-2 overflow-hidden rounded-full bg-black/20 ring-1 ring-white/10">
              <div
                className="h-full rounded-full bg-gradient-to-r from-[#8CC4FF] via-white to-[#A9D5FF] shadow-[0_0_14px_rgba(255,255,255,.45)] transition-[width] duration-700 ease-out"
                style={{ width: `${progressPct}%` }}
              />
            </div>

            <div className="mt-2 flex items-center justify-between gap-3 text-[9px] text-white/65">
              <span><b className="text-white">${progress.toFixed(2)}</b> spent</span>
              {nextRank && goal > 0 ? (
                <span><b className="text-white">${goal.toFixed(2)}</b> needed</span>
              ) : (
                <span>Rank complete</span>
              )}
            </div>
          </div>

          <div className="rounded-2xl border border-white/15 bg-white/10 px-4 py-3 backdrop-blur-sm sm:min-w-[150px]">
            <p className="text-[9px] font-bold tracking-[.14em] text-white/65">AVAILABLE BALANCE</p>
            <p className="mt-1 text-2xl font-bold tabular-nums">{cash(profile.balance)}</p>
            {nextRank && remaining > 0 && <p className="mt-1 text-[9px] text-white/60">${remaining.toFixed(2)} to {nextRank}</p>}
          </div>
        </div>
      </div>
    </section>

    <section className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4"><Action icon={<CircleDollarSign size={20} />} title="Add funds" note="Deposit methods" action="Deposit" onClick={() => onNavigate("wallet")} emphasis /><Action icon={<Smartphone size={20} />} title="Buy account" note={`${availableCountries.length} countries live`} action="Browse" onClick={() => onNavigate("buy")} /><Action icon={<HandCoins size={20} />} title="Sell account" note="Secure bot review" action="Sell" onClick={() => onNavigate("sell")} /><Action icon={<ArrowUpRight size={20} />} title="Withdraw" note="Wallet payout" action="Withdraw" onClick={() => onNavigate("wallet")} /></section>

    <section className="mt-5 grid gap-4 lg:grid-cols-[1.1fr_.9fr]">
      <div className="thin-card rounded-[26px] p-5"><div className="flex items-start justify-between gap-4"><div><p className="flex items-center gap-2 text-[10px] font-bold tracking-[.14em] text-[#1769F5]"><WalletCards size={15} /> ACCOUNT SNAPSHOT</p><p className="mt-2 text-xl font-bold text-[#263247]">Control your next move</p><p className="mt-1 text-xs leading-5 text-[#758198]">Every value comes from the verified TgFox server.</p></div><span className="rounded-full bg-[#E8F8EF] px-3 py-1.5 text-[10px] font-bold text-[#278A5C]">{currentRank}</span></div><div className="mt-5 grid grid-cols-2 gap-3"><Metric icon={<ShoppingBag size={17} />} label="Account orders" value={String(profile.counts.purchases || 0)} note="Recorded purchases" /><Metric icon={<HandCoins size={17} />} label="Seller requests" value={String(profile.counts.sales || 0)} note="Recorded sales" /><Metric icon={<CircleDollarSign size={17} />} label="Spent on accounts" value={cash(profile.buy_spent)} note="Purchase total" /><Metric icon={<BadgeCheck size={17} />} label="Seller earnings" value={cash(profile.sell_earned)} note="Sale total" /></div></div>
      <div className="rounded-[26px] bg-[#ECF9F5] p-5"><div className="flex items-center justify-between"><div><p className="flex items-center gap-2 text-[10px] font-bold tracking-[.14em] text-[#198563]"><PackageOpen size={15} /> LIVE MARKET</p><p className="mt-2 text-xl font-bold text-[#24453E]">{availableCountries.length} countries available</p></div><span className="grid h-11 w-11 place-items-center rounded-2xl bg-white text-[#198563]"><Smartphone size={20} /></span></div><div className="mt-5 grid grid-cols-2 gap-3"><MiniMetric label="Available accounts" value={String(liveAccounts)} /><MiniMetric label="Lowest live price" value={availableCountries.length ? cash(Math.min(...availableCountries.map((country) => country.final_price))) : "—"} /></div><div className="mt-4 flex gap-2 overflow-x-auto pb-1">{availableCountries.slice(0, 3).map((country) => <span key={country.code} className="shrink-0 rounded-xl border border-[#CDECE1] bg-white px-3 py-2 text-[10px] font-bold text-[#25735E]">{country.flag} {country.code}</span>)}{!availableCountries.length && <p className="text-xs leading-5 text-[#557C70]">Inventory is temporarily unavailable. Check again later.</p>}</div><button onClick={() => onNavigate("buy")} className="pressable mt-5 flex w-full items-center justify-center gap-2 rounded-2xl border border-[#BCE5D8] bg-white py-3 text-xs font-bold text-[#198563]">Open live account market <ArrowUpRight size={15} /></button></div>
    </section>

    <section className="thin-card mt-5 rounded-[26px] p-5"><div className="flex items-center justify-between gap-3"><div><p className="flex items-center gap-2 text-[10px] font-bold tracking-[.14em] text-[#1769F5]"><History size={15} /> RECENT ACCOUNT ACTIVITY</p><p className="mt-2 text-lg font-bold text-[#263247]">Latest orders</p></div><button onClick={() => onNavigate("transactions")} className="pressable rounded-xl bg-[#EAF2FF] px-3 py-2 text-[10px] font-bold text-[#1769F5]">Open activity</button></div><div className="mt-4 divide-y divide-[#EAF0F6]">{recentOrders.map((order) => <div key={order.id} className="grid grid-cols-[1fr_auto] items-center gap-4 py-3"><div className="min-w-0"><p className="truncate text-xs font-bold text-[#2B374A]">{order.account_id || order.id}</p><p className="mt-1 text-[10px] text-[#7C899A]">{date(order.at)} · {order.id}</p></div><div className="text-right"><p className="text-xs font-bold text-[#2B374A]">{cash(order.amount)}</p><p className="mt-1 text-[9px] font-bold text-[#1769F5]">{(order.status || "pending").toUpperCase()}</p></div></div>)}{!recentOrders.length && <div className="flex items-center gap-3 py-5"><Clock3 size={19} className="text-[#1769F5]" /><p className="text-xs leading-5 text-[#718096]">No account order is recorded yet. Start with the live market when you are ready.</p></div>}</div></section>
  </section>;
}

function Action({ icon, title, note, action, onClick, emphasis = false }: { icon: React.ReactNode; title: string; note: string; action: string; onClick: () => void; emphasis?: boolean }) { return <button onClick={onClick} className={`pressable thin-card min-h-[132px] rounded-[22px] p-4 text-left transition hover:-translate-y-0.5 ${emphasis ? "border-[#BFD8FF] bg-[#F2F7FF]" : "bg-white hover:border-[#BFD8FF]"}`}><span className="grid h-10 w-10 place-items-center rounded-xl bg-[#EAF2FF] text-[#1769F5]">{icon}</span><p className="mt-4 text-sm font-bold text-[#263247]">{title}</p><p className="mt-1 text-[10px] text-[#7A879A]">{note}</p><span className="mt-3 inline-flex items-center gap-1 text-[10px] font-bold text-[#1769F5]">{action} <ArrowUpRight size={13} /></span></button>; }
function Metric({ icon, label, value, note }: { icon: React.ReactNode; label: string; value: string; note: string }) { return <div className="rounded-2xl border border-[#E3EAF4] bg-[#FBFDFF] p-4"><span className="text-[#1769F5]">{icon}</span><p className="mt-3 text-[10px] font-bold text-[#728096]">{label.toUpperCase()}</p><p className="mt-1 truncate text-base font-bold text-[#263247]">{value}</p><p className="mt-1 text-[9px] text-[#97A1AF]">{note}</p></div>; }
function MiniMetric({ label, value }: { label: string; value: string }) { return <div className="rounded-2xl border border-[#D0EDE3] bg-white/80 p-3"><p className="text-[9px] font-bold tracking-wide text-[#6B9388]">{label.toUpperCase()}</p><p className="mt-2 text-base font-bold text-[#1D6452]">{value}</p></div>; }
