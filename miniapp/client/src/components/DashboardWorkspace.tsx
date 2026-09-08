/**
 * TgFox dashboard UX: production-safe dashboard card using verified profile data.
 * Visual-only enhancement: no API, navigation, or business logic changes.
 */
import { ArrowUpRight, BadgeCheck, CircleDollarSign, Clock3, Crown, HandCoins, History, PackageOpen, ShieldCheck, ShoppingBag, Smartphone, TrendingUp, WalletCards } from "lucide-react";
import { type Country, type Profile, type RecordResponse } from "@/lib/api";

type DashboardAction = "buy" | "sell" | "wallet" | "transactions";
const cash = (value: number) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 }).format(value || 0);
const date = (value: string) => value ? new Intl.DateTimeFormat("en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value)) : "—";

export function DashboardWorkspace({ profile, countries, records, onNavigate }: { profile: Profile; countries: Country[]; records: RecordResponse | null; onNavigate: (action: DashboardAction) => void }) {
  const availableCountries = countries.filter((country) => country.stock > 0 && !country.disabled && !country.is_full);
  const liveAccounts = availableCountries.reduce((total, country) => total + Math.max(0, country.stock), 0);
  const recentOrders = (records?.purchases || []).slice(0, 3);

  const nextReward = profile.next_reward || { rank: null, goal: 0, progress: profile.buy_spent || 0, remaining: 0 };
  const goal = Math.max(0, Number(nextReward.goal || 0));
  const progress = Math.max(0, Number(nextReward.progress ?? profile.buy_spent ?? 0));
  const progressPercent = goal > 0 ? Math.min(100, Math.round((Math.min(progress, goal) / goal) * 100)) : 100;
  const currentRank = (profile.rank || "VIP1").toUpperCase();
  const nextRank = nextReward.rank ? String(nextReward.rank).toUpperCase() : null;
  const isMaxRank = !nextRank;

  return <section className="panel-enter max-w-5xl">
    <section className="vip-dashboard-card relative overflow-hidden rounded-[30px] bg-[#173F82] p-5 text-white shadow-[0_22px_55px_rgba(23,63,130,.24)] sm:p-7">
      <div className="vip-grid absolute inset-0 opacity-[.16]" />
      <div className="vip-orb vip-orb-one absolute -right-16 -top-20 h-64 w-64 rounded-full border-[34px] border-white/[.07]" />
      <div className="vip-orb vip-orb-two absolute -bottom-28 left-[42%] h-52 w-52 rounded-full bg-[#78B0FF]/[.12] blur-3xl" />
      <div className="vip-sheen absolute inset-y-0 -left-1/3 w-1/3 bg-gradient-to-r from-transparent via-white/[.07] to-transparent" />

      <div className="relative z-10">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-[10px] font-bold tracking-[.18em] text-[#C7DDFF]">
              <ShieldCheck size={15} /> VERIFIED TG FOX ACCOUNT
            </div>
            <p className="mt-5 text-[13px] font-semibold tracking-[.08em] text-white/65">GOOD TO SEE YOU</p>
            <h1 className="mt-1 truncate text-[25px] font-bold tracking-[-.035em] sm:text-[29px]">{profile.user.first_name || "TgFox user"}</h1>
            {profile.user.username && <p className="mt-1 truncate text-[11px] font-medium text-white/55">@{profile.user.username}</p>}
          </div>

          <div className="shrink-0 rounded-2xl border border-white/[.14] bg-white/[.09] px-3 py-2.5 backdrop-blur-md">
            <div className="flex items-center gap-1.5 text-[9px] font-bold tracking-[.13em] text-white/60"><Crown size={12} /> VIP RANK</div>
            <p className="mt-1 text-sm font-extrabold tracking-wide text-white">{currentRank}</p>
          </div>
        </div>

        <div className="mt-6 grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
          <div className="rounded-[22px] border border-white/[.13] bg-[#0D326F]/40 p-4 backdrop-blur-sm">
            <div className="flex items-center justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2">
                <span className="grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-white/[.10] text-[#D9E8FF]"><TrendingUp size={15} /></span>
                <div className="min-w-0">
                  <p className="text-[9px] font-bold tracking-[.14em] text-white/55">VIP PROGRESS</p>
                  <p className="mt-0.5 truncate text-xs font-bold text-white">{isMaxRank ? "Maximum rank unlocked" : <>Next: {nextRank}</>}</p>
                </div>
              </div>
              <span className="shrink-0 text-xs font-extrabold tabular-nums text-white">{progressPercent}%</span>
            </div>

            <div className="mt-3 h-2 overflow-hidden rounded-full bg-white/[.12]">
              <div className="vip-progress-fill h-full rounded-full bg-gradient-to-r from-[#8CC2FF] via-white to-[#8CC2FF] shadow-[0_0_16px_rgba(170,210,255,.45)] transition-[width] duration-700" style={{ width: `${progressPercent}%` }} />
            </div>

            <div className="mt-2.5 flex items-center justify-between gap-3 text-[9px] font-medium text-white/50">
              <span>{cash(Math.min(progress, goal || progress))} spent</span>
              {isMaxRank ? <span className="font-bold text-[#D7E8FF]">MAX RANK</span> : <span>{cash(goal)} goal</span>}
            </div>
          </div>

          <div className="rounded-[22px] border border-white/[.15] bg-white/[.10] px-5 py-4 backdrop-blur-md sm:min-w-[145px]">
            <p className="text-[9px] font-bold tracking-[.14em] text-white/55">AVAILABLE BALANCE</p>
            <p className="mt-1 text-[25px] font-extrabold tracking-[-.035em] tabular-nums">{cash(profile.balance)}</p>
            {!isMaxRank && <p className="mt-1 text-[9px] font-medium text-white/50">{cash(Math.max(0, Number(nextReward.remaining || 0)))} to {nextRank}</p>}
          </div>
        </div>
      </div>
    </section>

    <section className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4"><Action icon={<CircleDollarSign size={20} />} title="Add funds" note="Deposit methods" action="Deposit" onClick={() => onNavigate("wallet")} emphasis /><Action icon={<Smartphone size={20} />} title="Buy account" note={`${availableCountries.length} countries live`} action="Browse" onClick={() => onNavigate("buy")} /><Action icon={<HandCoins size={20} />} title="Sell account" note="Secure bot review" action="Sell" onClick={() => onNavigate("sell")} /><Action icon={<ArrowUpRight size={20} />} title="Withdraw" note="Wallet payout" action="Withdraw" onClick={() => onNavigate("wallet")} /></section>

    <section className="mt-5 grid gap-4 lg:grid-cols-[1.1fr_.9fr]">
      <div className="thin-card rounded-[26px] p-5"><div className="flex items-start justify-between gap-4"><div><p className="flex items-center gap-2 text-[10px] font-bold tracking-[.14em] text-[#1769F5]"><WalletCards size={15} /> ACCOUNT SNAPSHOT</p><p className="mt-2 text-xl font-bold text-[#263247]">Control your next move</p><p className="mt-1 text-xs leading-5 text-[#758198]">Every value comes from the verified TgFox server.</p></div><span className="rounded-full bg-[#E8F8EF] px-3 py-1.5 text-[10px] font-bold text-[#278A5C]">{profile.rank || "ACTIVE"}</span></div><div className="mt-5 grid grid-cols-2 gap-3"><Metric icon={<ShoppingBag size={17} />} label="Account orders" value={String(profile.counts.purchases || 0)} note="Recorded purchases" /><Metric icon={<HandCoins size={17} />} label="Seller requests" value={String(profile.counts.sales || 0)} note="Recorded sales" /><Metric icon={<CircleDollarSign size={17} />} label="Spent on accounts" value={cash(profile.buy_spent)} note="Purchase total" /><Metric icon={<BadgeCheck size={17} />} label="Seller earnings" value={cash(profile.sell_earned)} note="Sale total" /></div></div>
      <div className="rounded-[26px] bg-[#ECF9F5] p-5"><div className="flex items-center justify-between"><div><p className="flex items-center gap-2 text-[10px] font-bold tracking-[.14em] text-[#198563]"><PackageOpen size={15} /> LIVE MARKET</p><p className="mt-2 text-xl font-bold text-[#24453E]">{availableCountries.length} countries available</p></div><span className="grid h-11 w-11 place-items-center rounded-2xl bg-white text-[#198563]"><Smartphone size={20} /></span></div><div className="mt-5 grid grid-cols-2 gap-3"><MiniMetric label="Available accounts" value={String(liveAccounts)} /><MiniMetric label="Lowest live price" value={availableCountries.length ? cash(Math.min(...availableCountries.map((country) => country.final_price))) : "—"} /></div><div className="mt-4 flex gap-2 overflow-x-auto pb-1">{availableCountries.slice(0, 3).map((country) => <span key={country.code} className="shrink-0 rounded-xl border border-[#CDECE1] bg-white px-3 py-2 text-[10px] font-bold text-[#25735E]">{country.flag} {country.code}</span>)}{!availableCountries.length && <p className="text-xs leading-5 text-[#557C70]">Inventory is temporarily unavailable. Check again later.</p>}</div><button onClick={() => onNavigate("buy")} className="pressable mt-5 flex w-full items-center justify-center gap-2 rounded-2xl border border-[#BCE5D8] bg-white py-3 text-xs font-bold text-[#198563]">Open live account market <ArrowUpRight size={15} /></button></div>
    </section>

    <section className="thin-card mt-5 rounded-[26px] p-5"><div className="flex items-center justify-between gap-3"><div><p className="flex items-center gap-2 text-[10px] font-bold tracking-[.14em] text-[#1769F5]"><History size={15} /> RECENT ACCOUNT ACTIVITY</p><p className="mt-2 text-lg font-bold text-[#263247]">Latest orders</p></div><button onClick={() => onNavigate("transactions")} className="pressable rounded-xl bg-[#EAF2FF] px-3 py-2 text-[10px] font-bold text-[#1769F5]">Open activity</button></div><div className="mt-4 divide-y divide-[#EAF0F6]">{recentOrders.map((order) => <div key={order.id} className="grid grid-cols-[1fr_auto] items-center gap-4 py-3"><div className="min-w-0"><p className="truncate text-xs font-bold text-[#2B374A]">{order.account_id || order.id}</p><p className="mt-1 text-[10px] text-[#7C899A]">{date(order.at)} · {order.id}</p></div><div className="text-right"><p className="text-xs font-bold text-[#2B374A]">{cash(order.amount)}</p><p className="mt-1 text-[9px] font-bold text-[#1769F5]">{(order.status || "pending").toUpperCase()}</p></div></div>)}{!recentOrders.length && <div className="flex items-center gap-3 py-5"><Clock3 size={19} className="text-[#1769F5]" /><p className="text-xs leading-5 text-[#718096]">No account order is recorded yet. Start with the live market when you are ready.</p></div>}</div></section>

    <style>{`
      .vip-dashboard-card { isolation: isolate; }
      .vip-grid {
        background-image: linear-gradient(rgba(255,255,255,.18) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.18) 1px, transparent 1px);
        background-size: 34px 34px;
        mask-image: linear-gradient(to bottom right, black, transparent 78%);
      }
      .vip-orb-one { animation: vip-orbit-one 18s ease-in-out infinite; }
      .vip-orb-two { animation: vip-orbit-two 13s ease-in-out infinite; }
      .vip-sheen { animation: vip-sheen 8s ease-in-out infinite; transform: skewX(-18deg); }
      .vip-progress-fill { background-size: 180% 100%; animation: vip-progress-shimmer 4s linear infinite; }
      @keyframes vip-sheen { 0%, 55% { transform: translateX(-15%) skewX(-18deg); opacity: 0; } 65% { opacity: 1; } 82%, 100% { transform: translateX(470%) skewX(-18deg); opacity: 0; } }
      @keyframes vip-orbit-one { 0%, 100% { transform: translate3d(0,0,0) rotate(0deg); } 50% { transform: translate3d(-18px,14px,0) rotate(7deg); } }
      @keyframes vip-orbit-two { 0%, 100% { transform: translate3d(0,0,0) scale(1); } 50% { transform: translate3d(24px,-14px,0) scale(1.08); } }
      @keyframes vip-progress-shimmer { from { background-position: 180% 0; } to { background-position: -20% 0; } }
      @media (prefers-reduced-motion: reduce) { .vip-orb-one, .vip-orb-two, .vip-sheen, .vip-progress-fill { animation: none !important; } }
    `}</style>
  </section>;
}

function Action({ icon, title, note, action, onClick, emphasis = false }: { icon: React.ReactNode; title: string; note: string; action: string; onClick: () => void; emphasis?: boolean }) { return <button onClick={onClick} className={`pressable thin-card min-h-[132px] rounded-[22px] p-4 text-left transition hover:-translate-y-0.5 ${emphasis ? "border-[#BFD8FF] bg-[#F2F7FF]" : "bg-white hover:border-[#BFD8FF]"}`}><span className="grid h-10 w-10 place-items-center rounded-xl bg-[#EAF2FF] text-[#1769F5]">{icon}</span><p className="mt-4 text-sm font-bold text-[#263247]">{title}</p><p className="mt-1 text-[10px] text-[#7A879A]">{note}</p><span className="mt-3 inline-flex items-center gap-1 text-[10px] font-bold text-[#1769F5]">{action} <ArrowUpRight size={13} /></span></button>; }
function Metric({ icon, label, value, note }: { icon: React.ReactNode; label: string; value: string; note: string }) { return <div className="rounded-2xl border border-[#E3EAF4] bg-[#FBFDFF] p-4"><span className="text-[#1769F5]">{icon}</span><p className="mt-3 text-[10px] font-bold text-[#728096]">{label.toUpperCase()}</p><p className="mt-1 truncate text-base font-bold text-[#263247]">{value}</p><p className="mt-1 text-[9px] text-[#97A1AF]">{note}</p></div>; }
function MiniMetric({ label, value }: { label: string; value: string }) { return <div className="rounded-2xl border border-[#D0EDE3] bg-white/80 p-3"><p className="text-[9px] font-bold tracking-wide text-[#6B9388]">{label.toUpperCase()}</p><p className="mt-2 text-base font-bold text-[#1D6452]">{value}</p></div>; }
