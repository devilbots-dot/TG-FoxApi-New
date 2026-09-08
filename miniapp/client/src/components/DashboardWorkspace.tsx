/**
 * TgFox dashboard UX: production-safe dashboard card using verified profile data.
 * Visual-only enhancement: no API, navigation, or business logic changes.
 */
import { useEffect, useMemo, useState } from "react";
import { ArrowUpRight, BadgeCheck, CircleDollarSign, Clock3, Crown, HandCoins, History, PackageOpen, ShieldCheck, ShoppingBag, Smartphone, TrendingUp, WalletCards } from "lucide-react";
import { type Country, type Profile, type RecordResponse } from "@/lib/api";

type DashboardAction = "buy" | "sell" | "wallet" | "transactions";
const cash = (value: number) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 }).format(value || 0);
const date = (value: string) => value ? new Intl.DateTimeFormat("en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value)) : "—";

export function DashboardWorkspace({ profile, countries, records, onNavigate }: { profile: Profile; countries: Country[]; records: RecordResponse | null; onNavigate: (action: DashboardAction) => void }) {
  const [flipped, setFlipped] = useState(false);
  const [selectedMetric, setSelectedMetric] = useState<string | null>(null);
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
  const balanceTarget = Math.max(0, Number(profile.balance || 0));
  const [displayBalance, setDisplayBalance] = useState(0);

  useEffect(() => {
    const from = displayBalance;
    const delta = balanceTarget - from;
    if (Math.abs(delta) < 0.005) {
      setDisplayBalance(balanceTarget);
      return;
    }
    const started = performance.now();
    const duration = 900;
    let frame = 0;
    const tick = (now: number) => {
      const t = Math.min(1, (now - started) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      setDisplayBalance(from + delta * eased);
      if (t < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
    // Intentionally react only to the server balance target; this also animates a new deposit.
  }, [balanceTarget]);

  const telegramUser = useMemo(() => {
    try {
      return (window as any)?.Telegram?.WebApp?.initDataUnsafe?.user || null;
    } catch {
      return null;
    }
  }, []);
  const profilePhoto = telegramUser?.photo_url || "";
  const logoSrc = "/branding/tgfox-api-logo.jpg";

  return <section className="panel-enter max-w-5xl">
    <section className={`vip-dashboard-stage ${flipped ? "is-flipped" : ""} relative`} aria-label="VIP account card">
      <div className="vip-card-flip-hint absolute right-3 top-3 z-30 rounded-full border border-white/20 bg-black/20 px-2.5 py-1 text-[8px] font-bold tracking-[.12em] text-white/75 backdrop-blur-md">
        TAP TO FLIP
      </div>
      <div
        className="vip-card-inner relative cursor-pointer"
        role="button"
        tabIndex={0}
        aria-pressed={flipped}
        aria-label={flipped ? "Show account summary" : "Show profile card"}
        onClick={() => setFlipped((value) => !value)}
        onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setFlipped((value) => !value); } }}
      >
        <div className="vip-card-face vip-card-front">
          <div className="vip-dashboard-card vip-glass-card relative overflow-hidden rounded-[30px] p-5 text-white sm:p-7">
            <div className="vip-glass-noise absolute inset-0" />
            <div className="vip-grid absolute inset-0 opacity-[.13]" />
            <div className="vip-orb vip-orb-one absolute -right-20 -top-24 h-72 w-72 rounded-full border-[38px] border-white/[.07]" />
            <div className="vip-orb vip-orb-two absolute -bottom-28 left-[38%] h-56 w-56 rounded-full bg-cyan-300/[.10] blur-3xl" />
            <div className="vip-light-source absolute -right-20 top-4 h-44 w-72 rounded-full bg-cyan-200/[.13] blur-3xl" />
            <div className="vip-border-shimmer absolute inset-0 rounded-[30px]" />
            <div className="vip-sheen absolute inset-y-0 -left-1/3 w-1/3 bg-gradient-to-r from-transparent via-white/[.11] to-transparent" />

            <div className="relative z-10">
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-center gap-2 rounded-full border border-white/[.16] bg-white/[.07] px-3 py-1.5 backdrop-blur-xl">
                  <ShieldCheck size={14} className="text-cyan-100" />
                  <span className="text-[9px] font-extrabold tracking-[.16em] text-white/80">VERIFIED ACCOUNT</span>
                </div>
                <div className="vip-rank-badge relative shrink-0 overflow-hidden rounded-2xl border border-amber-200/[.38] bg-gradient-to-br from-amber-100/[.18] via-yellow-300/[.10] to-white/[.06] px-3.5 py-2 backdrop-blur-xl shadow-[0_0_26px_rgba(250,204,21,.18)]">
                  <div className="vip-gold-sweep absolute inset-y-0 -left-1/2 w-1/2 bg-gradient-to-r from-transparent via-white/40 to-transparent" />
                  <div className="relative flex items-center gap-1.5"><Crown size={13} className="text-amber-200" /><span className="text-[9px] font-black tracking-[.12em] text-amber-100">{currentRank}</span></div>
                  <p className="relative mt-0.5 text-[7px] font-bold tracking-[.14em] text-amber-100/55">VIP RANK</p>
                </div>
              </div>

              <div className="mt-4 text-center">
                <p className="text-[10px] font-extrabold tracking-[.22em] text-white/55">GOOD TO SEE YOU</p>
                <h1 className="mt-1 truncate text-[23px] font-extrabold tracking-[-.035em] sm:text-[28px]">{profile.user.first_name || "TgFox user"}</h1>
                {profile.user.username && <p className="mt-0.5 truncate text-[10px] font-medium text-cyan-50/65">@{profile.user.username}</p>}
              </div>

              <div className="vip-balance-panel mt-3.5 rounded-[20px] border border-white/[.20] bg-[#082A63]/[.48] p-3.5 backdrop-blur-2xl">
                <div className="flex items-end justify-between gap-3">
                  <div>
                    <p className="text-[9px] font-bold tracking-[.17em] text-white/50">AVAILABLE BALANCE</p>
                    <p className="mt-0.5 text-[28px] font-black tracking-[-.045em] tabular-nums sm:text-[32px]">{cash(displayBalance)}</p>
                  </div>
                  <div className="mb-0.5 grid h-9 w-9 place-items-center rounded-xl border border-cyan-100/[.22] bg-cyan-100/[.10] text-cyan-100"><CircleDollarSign size={19} /></div>
                </div>
              </div>

              <div className="vip-progress-panel mt-2.5 rounded-[20px] border border-white/[.16] bg-[#0D3978]/[.42] p-3.5 backdrop-blur-2xl">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex min-w-0 items-center gap-2"><TrendingUp size={15} className="shrink-0 text-cyan-200" /><div><p className="text-[9px] font-black tracking-[.15em] text-white/55">VIP PROGRESS</p><p className="mt-0.5 text-[10px] font-bold text-white/85">{isMaxRank ? "Maximum rank unlocked" : `${nextRank} • ${cash(Math.max(0, Number(nextReward.remaining || 0)))} remaining`}</p></div></div>
                  <span className="text-xs font-black tabular-nums text-cyan-100">{progressPercent}%</span>
                </div>
                <div className="vip-progress-track mt-2.5 h-1.5 overflow-hidden rounded-full bg-white/[.10]">
                  <div className="vip-progress-fill h-full rounded-full" style={{ width: `${progressPercent}%` }} />
                </div>
                <div className="mt-2 flex items-center justify-between text-[8px] font-bold text-white/45"><span>{cash(Math.min(progress, goal || progress))} spent</span><span>{isMaxRank ? "MAX RANK" : cash(goal) + " goal"}</span></div>
              </div>
            </div>
          </div>
        </div>

        <div className="vip-card-face vip-card-back">
          <div className="vip-dashboard-card vip-glass-card relative flex h-full flex-col justify-between overflow-hidden rounded-[30px] p-5 text-white sm:p-7">
            <div className="vip-grid absolute inset-0 opacity-[.12]" />
            <div className="vip-orb vip-orb-one absolute -right-16 -top-16 h-60 w-60 rounded-full border-[30px] border-amber-100/[.06]" />
            <div className="vip-back-glow absolute -left-16 -bottom-16 h-52 w-52 rounded-full bg-cyan-300/[.12] blur-3xl" />
            <div className="relative z-10 flex items-center justify-between gap-4">
              <div className="flex items-center gap-3"><img src={logoSrc} alt="TG FOX" className="h-11 w-11 rounded-xl border border-white/20 object-cover" /><div><p className="text-[9px] font-black tracking-[.2em] text-white/55">TG FOX API</p><p className="mt-1 text-sm font-bold">VIP MEMBER CARD</p></div></div>
              <span className="rounded-full border border-amber-200/25 bg-amber-200/[.08] px-3 py-1.5 text-[9px] font-black text-amber-100">{currentRank}</span>
            </div>
            <div className="relative z-10 flex flex-col items-center py-1">
              <div className="vip-avatar-wrap relative grid h-24 w-24 place-items-center rounded-full p-[3px]">
                <div className="absolute inset-0 rounded-full border border-amber-100/25" />
                <div className="absolute -inset-2 rounded-full border border-cyan-200/20" />
                <div className="h-full w-full overflow-hidden rounded-full bg-[#09214B] shadow-[0_0_38px_rgba(34,211,238,.18)]">
                  {profilePhoto ? <img src={profilePhoto} alt="Profile" className="h-full w-full object-cover" referrerPolicy="no-referrer" /> : <img src={logoSrc} alt="TG FOX" className="h-full w-full object-cover" />}
                </div>
              </div>
              <h2 className="mt-2 max-w-[90%] truncate text-lg font-extrabold">{profile.user.first_name || "TgFox user"}</h2>
              {profile.user.username && <p className="mt-1 text-xs text-white/50">@{profile.user.username}</p>}
              <p className="mt-2 text-[8px] font-bold tracking-[.16em] text-white/40">YOUR PREMIUM ACCESS</p>
            </div>
            <div className="relative z-10 grid grid-cols-2 gap-3">
              <div className="rounded-2xl border border-white/10 bg-white/[.06] p-3 backdrop-blur-xl"><p className="text-[8px] font-bold tracking-wide text-white/45">TOTAL SPENT</p><p className="mt-1 text-base font-extrabold">{cash(profile.buy_spent)}</p></div>
              <div className="rounded-2xl border border-white/10 bg-white/[.06] p-3 text-right backdrop-blur-xl"><p className="text-[8px] font-bold tracking-wide text-white/45">NEXT RANK</p><p className="mt-1 text-base font-extrabold text-amber-100">{isMaxRank ? "MAX" : nextRank}</p></div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4"><Action icon={<CircleDollarSign size={20} />} title="Add funds" note="Deposit methods" action="Deposit" onClick={() => onNavigate("wallet")} emphasis /><Action icon={<Smartphone size={20} />} title="Buy account" note={`${availableCountries.length} countries live`} action="Browse" onClick={() => onNavigate("buy")} /><Action icon={<HandCoins size={20} />} title="Sell account" note="Secure bot review" action="Sell" onClick={() => onNavigate("sell")} /><Action icon={<ArrowUpRight size={20} />} title="Withdraw" note="Wallet payout" action="Withdraw" onClick={() => onNavigate("wallet")} /></section>

    <section className="mt-5 grid gap-4 lg:grid-cols-[1.1fr_.9fr]">
      <div className="thin-card rounded-[26px] p-5"><div className="flex items-start justify-between gap-4"><div><p className="flex items-center gap-2 text-[10px] font-bold tracking-[.14em] text-[#1769F5]"><WalletCards size={15} /> ACCOUNT SNAPSHOT</p><p className="mt-2 text-xl font-bold text-[#263247]">Control your next move</p><p className="mt-1 text-xs leading-5 text-[#758198]">Every value comes from the verified TgFox server.</p></div><span className="rounded-full bg-[#E8F8EF] px-3 py-1.5 text-[10px] font-bold text-[#278A5C]">{profile.rank || "ACTIVE"}</span></div><div className="mt-5 grid grid-cols-2 gap-3"><Metric metricId="orders" selected={selectedMetric === "orders"} anySelected={selectedMetric !== null} onSelect={setSelectedMetric} icon={<ShoppingBag size={17} />} label="Account orders" value={String(profile.counts.purchases || 0)} note="Recorded purchases" detail="Your completed account purchase orders." /><Metric metricId="sales" selected={selectedMetric === "sales"} anySelected={selectedMetric !== null} onSelect={setSelectedMetric} icon={<HandCoins size={17} />} label="Seller requests" value={String(profile.counts.sales || 0)} note="Recorded sales" detail="Seller requests recorded on your account." /><Metric metricId="spent" selected={selectedMetric === "spent"} anySelected={selectedMetric !== null} onSelect={setSelectedMetric} icon={<CircleDollarSign size={17} />} label="Spent on accounts" value={cash(profile.buy_spent)} note="Purchase total" detail="Total amount spent on account purchases." /><Metric metricId="earnings" selected={selectedMetric === "earnings"} anySelected={selectedMetric !== null} onSelect={setSelectedMetric} icon={<BadgeCheck size={17} />} label="Seller earnings" value={cash(profile.sell_earned)} note="Sale total" detail="Total earnings recorded from your sales." /></div></div>
      <div className="rounded-[26px] bg-[#ECF9F5] p-5"><div className="flex items-center justify-between"><div><p className="flex items-center gap-2 text-[10px] font-bold tracking-[.14em] text-[#198563]"><PackageOpen size={15} /> LIVE MARKET</p><p className="mt-2 text-xl font-bold text-[#24453E]">{availableCountries.length} countries available</p></div><span className="grid h-11 w-11 place-items-center rounded-2xl bg-white text-[#198563]"><Smartphone size={20} /></span></div><div className="mt-5 grid grid-cols-2 gap-3"><MiniMetric label="Available accounts" value={String(liveAccounts)} /><MiniMetric label="Lowest live price" value={availableCountries.length ? cash(Math.min(...availableCountries.map((country) => country.final_price))) : "—"} /></div><div className="mt-4 flex gap-2 overflow-x-auto pb-1">{availableCountries.slice(0, 3).map((country) => <span key={country.code} className="shrink-0 rounded-xl border border-[#CDECE1] bg-white px-3 py-2 text-[10px] font-bold text-[#25735E]">{country.flag} {country.code}</span>)}{!availableCountries.length && <p className="text-xs leading-5 text-[#557C70]">Inventory is temporarily unavailable. Check again later.</p>}</div><button onClick={() => onNavigate("buy")} className="pressable mt-5 flex w-full items-center justify-center gap-2 rounded-2xl border border-[#BCE5D8] bg-white py-3 text-xs font-bold text-[#198563]">Open live account market <ArrowUpRight size={15} /></button></div>
    </section>

    <section className="thin-card mt-5 rounded-[26px] p-5"><div className="flex items-center justify-between gap-3"><div><p className="flex items-center gap-2 text-[10px] font-bold tracking-[.14em] text-[#1769F5]"><History size={15} /> RECENT ACCOUNT ACTIVITY</p><p className="mt-2 text-lg font-bold text-[#263247]">Latest orders</p></div><button onClick={() => onNavigate("transactions")} className="pressable rounded-xl bg-[#EAF2FF] px-3 py-2 text-[10px] font-bold text-[#1769F5]">Open activity</button></div><div className="mt-4 divide-y divide-[#EAF0F6]">{recentOrders.map((order) => <div key={order.id} className="grid grid-cols-[1fr_auto] items-center gap-4 py-3"><div className="min-w-0"><p className="truncate text-xs font-bold text-[#2B374A]">{order.account_id || order.id}</p><p className="mt-1 text-[10px] text-[#7C899A]">{date(order.at)} · {order.id}</p></div><div className="text-right"><p className="text-xs font-bold text-[#2B374A]">{cash(order.amount)}</p><p className="mt-1 text-[9px] font-bold text-[#1769F5]">{(order.status || "pending").toUpperCase()}</p></div></div>)}{!recentOrders.length && <div className="flex items-center gap-3 py-5"><Clock3 size={19} className="text-[#1769F5]" /><p className="text-xs leading-5 text-[#718096]">No account order is recorded yet. Start with the live market when you are ready.</p></div>}</div></section>

    <style>{`
      .vip-dashboard-stage { perspective: 1500px; height: 405px; min-height: 405px; }
      .vip-card-inner { height: 100%; min-height: 0; transform-style: preserve-3d; transition: transform 900ms cubic-bezier(.2,.75,.2,1); }
      .vip-dashboard-stage.is-flipped .vip-card-inner { transform: rotateY(180deg); }
      .vip-card-face { position: absolute; inset: 0; width: 100%; height: 100%; min-height: 0; backface-visibility: hidden; -webkit-backface-visibility: hidden; }
      .vip-card-back { transform: rotateY(180deg); }
      .vip-card-front > .vip-dashboard-card, .vip-card-back > .vip-dashboard-card { height: 100%; min-height: 0; }
      .vip-glass-card { background: linear-gradient(145deg, rgba(30,103,218,.94) 0%, rgba(20,78,165,.93) 48%, rgba(8,38,91,.96) 100%); border: 1px solid rgba(255,255,255,.18); box-shadow: inset 0 1px 0 rgba(255,255,255,.10), 0 24px 60px rgba(12,62,145,.24), 0 0 0 1px rgba(103,190,255,.05); backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px); }
      .vip-glass-noise { opacity: .055; background-image: radial-gradient(rgba(255,255,255,.7) .5px, transparent .5px); background-size: 5px 5px; mix-blend-mode: overlay; }
      .vip-dashboard-card { isolation: isolate; }
      .vip-grid { background-image: linear-gradient(rgba(255,255,255,.18) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.18) 1px, transparent 1px); background-size: 34px 34px; mask-image: linear-gradient(to bottom right, black, transparent 78%); }
      .vip-orb-one { animation: vip-orbit-one 16s ease-in-out infinite; }
      .vip-orb-two { animation: vip-orbit-two 12s ease-in-out infinite; }
      .vip-light-source { animation: vip-light-breathe 7s ease-in-out infinite; }
      .vip-sheen { animation: vip-sheen 7.5s ease-in-out infinite; transform: skewX(-18deg); pointer-events:none; }
      .vip-border-shimmer { border: 1px solid transparent; background: linear-gradient(120deg, transparent 25%, rgba(255,255,255,.28), transparent 55%) border-box; -webkit-mask: linear-gradient(#000 0 0) padding-box, linear-gradient(#000 0 0); -webkit-mask-composite: xor; mask-composite: exclude; animation: vip-border 6.5s linear infinite; pointer-events:none; }
      .vip-gold-sweep { animation: vip-gold-sweep 5.5s ease-in-out infinite; pointer-events:none; }
      .vip-balance-panel { box-shadow: inset 0 1px 0 rgba(255,255,255,.08); }
      .vip-progress-track { box-shadow: inset 0 1px 3px rgba(0,0,0,.22); }
      .vip-progress-fill { position:relative; background: linear-gradient(90deg, #38bdf8, #67e8f9 45%, #d9f99d 100%); box-shadow: 0 0 12px rgba(103,232,249,.48); transition: width 1100ms cubic-bezier(.2,.8,.2,1); }
      .vip-progress-fill::after { content:""; position:absolute; right:0; top:50%; width:7px; height:7px; border-radius:999px; transform:translate(50%,-50%); background:#ecfeff; box-shadow:0 0 5px #fff, 0 0 15px #67e8f9, 0 0 25px rgba(103,232,249,.8); animation: vip-progress-end 2.2s ease-in-out infinite; }
      .vip-card-flip-hint { pointer-events: none; animation: vip-hint-pulse 3.8s ease-in-out infinite; }
      .vip-avatar-wrap { animation: vip-avatar-float 4.5s ease-in-out infinite; }
      .vip-back-glow { animation: vip-back-glow 8s ease-in-out infinite; }
      .metric-pop-wrap { position: relative; min-width: 0; transition: transform 280ms cubic-bezier(.2,.8,.2,1), opacity 220ms ease, filter 220ms ease; transform-origin: center; }
      .metric-pop-wrap.is-dimmed { transform: scale(.92); opacity: .62; filter: saturate(.72); }
      .metric-pop-wrap.is-selected { z-index: 30; transform: scale(1.075); }
      .metric-pop-card { position: relative; overflow: hidden; cursor: pointer; transition: box-shadow 280ms ease, border-color 280ms ease, background 280ms ease; }
      .metric-pop-card::before { content:""; position:absolute; inset:-1px; border-radius:inherit; padding:1.5px; background:conic-gradient(from 0deg, transparent 0deg, transparent 55deg, #38bdf8 85deg, #7dd3fc 125deg, #2563eb 170deg, transparent 215deg, transparent 360deg); -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0); -webkit-mask-composite:xor; mask-composite:exclude; opacity:0; transition:opacity 220ms ease; pointer-events:none; }
      .metric-pop-wrap.is-selected .metric-pop-card { border-color:rgba(56,189,248,.72); background:linear-gradient(145deg,#ffffff 0%,#f4f9ff 100%); box-shadow:0 18px 38px rgba(37,99,235,.22), 0 0 18px rgba(56,189,248,.24), inset 0 1px 0 rgba(255,255,255,.95); }
      .metric-pop-wrap.is-selected .metric-pop-card::before { opacity:1; animation:metric-neon-run 2.15s linear infinite; }
      .metric-pop-wrap.is-selected .metric-pop-icon { box-shadow:0 0 0 5px rgba(56,189,248,.08), 0 0 20px rgba(56,189,248,.42); transform:scale(1.08); }
      .metric-pop-detail { animation:metric-detail-in 260ms cubic-bezier(.2,.8,.2,1) both; }
      @keyframes metric-neon-run { to { transform:rotate(360deg); } }
      @keyframes metric-detail-in { from { opacity:0; transform:translateY(5px) scale(.97); } to { opacity:1; transform:translateY(0) scale(1); } }
      @keyframes vip-sheen { 0%, 55% { transform: translateX(-15%) skewX(-18deg); opacity:0; } 65% { opacity:1; } 82%,100% { transform:translateX(470%) skewX(-18deg); opacity:0; } }
      @keyframes vip-border { 0%,35% { opacity:.2; background-position:-100% 0; } 65% { opacity:.8; } 100% { opacity:.2; background-position:200% 0; } }
      @keyframes vip-gold-sweep { 0%,62% { transform:translateX(-20%); opacity:0; } 72% { opacity:1; } 86%,100% { transform:translateX(360%); opacity:0; } }
      @keyframes vip-orbit-one { 0%,100% { transform:translate3d(0,0,0) rotate(0deg); } 50% { transform:translate3d(-20px,16px,0) rotate(8deg); } }
      @keyframes vip-orbit-two { 0%,100% { transform:translate3d(0,0,0) scale(1); } 50% { transform:translate3d(24px,-16px,0) scale(1.1); } }
      @keyframes vip-light-breathe { 0%,100% { opacity:.35; transform:translateY(0) scale(1); } 50% { opacity:.8; transform:translateY(12px) scale(1.08); } }
      @keyframes vip-progress-end { 0%,100% { opacity:.65; transform:translate(50%,-50%) scale(.9); } 50% { opacity:1; transform:translate(50%,-50%) scale(1.3); } }
      @keyframes vip-hint-pulse { 0%,70%,100% { opacity:.65; transform:translateY(0); } 80% { opacity:1; transform:translateY(-2px); } }
      @keyframes vip-avatar-float { 0%,100% { transform:translateY(0) scale(1); } 50% { transform:translateY(-5px) scale(1.015); } }
      @keyframes vip-back-glow { 0%,100% { transform:translate3d(0,0,0); opacity:.55; } 50% { transform:translate3d(18px,-14px,0) scale(1.12); opacity:1; } }
      @media (prefers-reduced-motion: reduce) { .vip-orb-one,.vip-orb-two,.vip-light-source,.vip-sheen,.vip-border-shimmer,.vip-gold-sweep,.vip-progress-fill::after,.vip-card-flip-hint,.vip-avatar-wrap,.vip-back-glow { animation:none !important; } }
    `}</style>
  </section>;
}

function Action({ icon, title, note, action, onClick, emphasis = false }: { icon: React.ReactNode; title: string; note: string; action: string; onClick: () => void; emphasis?: boolean }) { return <button onClick={onClick} className={`pressable group thin-card min-h-[132px] rounded-[22px] border border-[#E2EAF4] p-4 text-left shadow-[0_8px_24px_rgba(24,55,100,.06)] transition-all duration-200 hover:-translate-y-1 hover:border-[#9FC4FF] hover:shadow-[0_14px_32px_rgba(23,105,245,.15)] active:translate-y-0 active:scale-[.975] ${emphasis ? "border-[#BFD8FF] bg-gradient-to-br from-[#F4F8FF] to-white" : "bg-white"}`}><span className="grid h-10 w-10 place-items-center rounded-xl bg-[#EAF2FF] text-[#1769F5] transition-transform duration-200 group-hover:scale-110 group-hover:-rotate-2 group-active:scale-95">{icon}</span><p className="mt-4 text-sm font-bold text-[#263247]">{title}</p><p className="mt-1 text-[10px] text-[#7A879A]">{note}</p><span className="mt-3 inline-flex items-center gap-1 text-[10px] font-bold text-[#1769F5] transition-transform duration-200 group-hover:translate-x-0.5">{action} <ArrowUpRight size={13} /></span></button>; }
function Metric({ metricId, selected, anySelected, onSelect, icon, label, value, note, detail }: { metricId: string; selected: boolean; anySelected: boolean; onSelect: (id: string | null) => void; icon: React.ReactNode; label: string; value: string; note: string; detail: string }) { return <div className={`metric-pop-wrap ${selected ? "is-selected" : anySelected ? "is-dimmed" : ""}`}><button type="button" aria-expanded={selected} onClick={() => onSelect(selected ? null : metricId)} className="metric-pop-card block w-full rounded-2xl border border-[#E3EAF4] bg-[#FBFDFF] p-4 text-left"><span className="metric-pop-icon inline-grid h-9 w-9 place-items-center rounded-xl bg-[#EAF2FF] text-[#1769F5] transition-all duration-300">{icon}</span><p className="mt-3 text-[10px] font-bold text-[#728096]">{label.toUpperCase()}</p><p className="mt-1 truncate text-base font-bold text-[#263247]">{value}</p><p className="mt-1 text-[9px] text-[#97A1AF]">{note}</p>{selected && <span className="metric-pop-detail mt-3 block rounded-xl border border-[#D8ECFF] bg-[#F1F8FF] px-2.5 py-2 text-[9px] font-semibold leading-4 text-[#52708F]">{detail}</span>}</button></div>; }
function MiniMetric({ label, value }: { label: string; value: string }) { return <div className="rounded-2xl border border-[#D0EDE3] bg-white/80 p-3"><p className="text-[9px] font-bold tracking-wide text-[#6B9388]">{label.toUpperCase()}</p><p className="mt-2 text-base font-bold text-[#1D6452]">{value}</p></div>; }
