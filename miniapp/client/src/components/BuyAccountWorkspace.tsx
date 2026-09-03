/**
 * TgFox Buy Account UX: a full-page real-inventory journey from country
 * selection to explicit balance review, order creation, and live OTP delivery.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, BadgeCheck, CheckCircle2, ChevronRight, CircleDollarSign, Copy, CreditCard, LoaderCircle, Search, ShieldCheck, Smartphone } from "lucide-react";
import { toast } from "sonner";
import { TgFoxApiError, type BuyResponse, type Country, tgfoxApi } from "@/lib/api";

type Screen = "countries" | "review" | "delivery";
const cash = (n: number) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 }).format(n || 0);

export function BuyAccountWorkspace({ countries, balance, onBack, onOrderCreated }: { countries: Country[]; balance: number; onBack: () => void; onOrderCreated: (order: BuyResponse) => Promise<void> | void }) {
  const [screen, setScreen] = useState<Screen>("countries");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Country | null>(null);
  const [order, setOrder] = useState<BuyResponse | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [otp, setOtp] = useState<{ status: string; code?: string; password?: string | null; completed?: boolean }>({ status: "waiting" });
  const [terminating, setTerminating] = useState(false);
  const [terminated, setTerminated] = useState(false);
  const inFlight = useRef(false);
  const filtered = useMemo(() => countries.filter((country) => `${country.name} ${country.code}`.toLowerCase().includes(search.toLowerCase())), [countries, search]);

  useEffect(() => {
    if (!order || screen !== "delivery") return;
    let active = true; let pollId: number | undefined; let polling = false;
    const stop = () => { if (pollId !== undefined) window.clearInterval(pollId); };
    const poll = async () => {
      if (!active || polling) return;
      polling = true;
      try {
        const result = await tgfoxApi.orderOtp(order.order_id);
        if (!active) return;
         setOtp({ status: result.otp_status, code: result.otp, password: result.password, completed: result.otp_status === "ready" });
        if (["ready", "timeout", "expired"].includes(result.otp_status)) stop();
      } catch (error) {
        if (!active) return;
        const terminal = error instanceof TgFoxApiError ? ({ 404: "unknown", 408: "timeout", 410: "expired" } as Record<number, string>)[error.status || 0] : undefined;
        if (terminal) { setOtp({ status: terminal }); stop(); }
        else toast.error(error instanceof Error ? error.message : "OTP status is temporarily unavailable.");
      } finally { polling = false; }
    };
    void poll(); pollId = window.setInterval(() => { void poll(); }, 8000);
    return () => { active = false; stop(); };
  }, [order, screen]);

  const copy = (value: string, label: string) => navigator.clipboard.writeText(value).then(() => toast.success(`${label} copied.`)).catch(() => toast.error("Copy is unavailable in this browser."));
  const choose = (country: Country) => { if (!country.disabled && !country.is_full && country.stock > 0) { setSelected(country); setScreen("review"); } };
  const buy = async () => {
    if (!selected || inFlight.current) return;
    inFlight.current = true; setSubmitting(true);
    try {
      const result = await tgfoxApi.buy(selected.code);
       setOrder(result); setOtp({ status: "waiting" }); setTerminated(false); setScreen("delivery");
      toast.success("Order created. Preparing secure delivery.");
      await onOrderCreated(result);
    } catch (error) { toast.error(error instanceof Error ? error.message : "Account order could not be created."); }
    finally { inFlight.current = false; setSubmitting(false); }
  };
  const back = () => { if (screen === "countries") onBack(); else if (screen === "review") { setSelected(null); setScreen("countries"); } else { setOrder(null); setSelected(null); setScreen("countries"); } };
  if (screen === "countries") return <CountryPicker countries={filtered} search={search} onSearch={setSearch} onBack={back} onChoose={choose} />;
  if (screen === "review" && selected) return <Review country={selected} balance={balance} submitting={submitting} onBack={back} onConfirm={buy} />;
   const terminate = async () => {
     if (!order || terminating || terminated || otp.status !== "ready") return;
     if (!window.confirm("Terminate the platform session for this purchased account? This does not log out your own device.")) return;
     setTerminating(true);
     try {
       const result = await tgfoxApi.terminateOrder(order.order_id);
       if (result.terminated) { setTerminated(true); toast.success("Platform session terminated."); }
     } catch (error) { toast.error(error instanceof Error ? error.message : "Could not terminate the platform session."); }
     finally { setTerminating(false); }
   };
   if (screen === "delivery" && order) return <Delivery order={order} otp={otp} terminating={terminating} terminated={terminated} onTerminate={terminate} onBack={back} onCopy={copy} />;
  return null;
}

function CountryPicker({ countries, search, onSearch, onBack, onChoose }: { countries: Country[]; search: string; onSearch: (value: string) => void; onBack: () => void; onChoose: (country: Country) => void }) { return <section className="panel-enter max-w-4xl"><Header eyebrow="BUY ACCOUNT" title="Choose an available country" text="Select from live inventory. Price and stock are checked again by the server when you confirm." onBack={onBack} /><label className="thin-card mt-5 flex h-12 items-center gap-3 rounded-2xl px-4"><Search size={17} className="text-[#1769F5]" /><input value={search} onChange={(event) => onSearch(event.target.value)} placeholder="Search country or ISO code" className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-[#9DA6B5]" /></label><div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{countries.map((country) => <CountryCard key={country.code} country={country} onChoose={onChoose} />)}{!countries.length ? <p className="thin-card py-12 text-center text-sm text-[#8792A3] sm:col-span-2">No live country matches your search.</p> : null}</div></section>; }
function CountryCard({ country, onChoose }: { country: Country; onChoose: (country: Country) => void }) { const unavailable = country.disabled || country.is_full || country.stock <= 0; return <button disabled={unavailable} onClick={() => onChoose(country)} className="pressable thin-card min-h-[168px] rounded-[24px] p-5 text-left transition enabled:hover:-translate-y-0.5 enabled:hover:border-[#AFCBFF] disabled:cursor-not-allowed disabled:opacity-60"><div className="flex items-start justify-between gap-3"><span className="text-3xl">{country.flag}</span><span className={`rounded-full px-2.5 py-1 text-[10px] font-bold ${unavailable ? "bg-[#FFF0F1] text-[#C84C5D]" : country.stock < 25 ? "bg-[#FFF4DF] text-[#A86E09]" : "bg-[#E7F8EF] text-[#278A5C]"}`}>{unavailable ? "UNAVAILABLE" : `${country.stock} LIVE`}</span></div><p className="mt-5 text-base font-bold text-[#263247]">{country.name}</p><p className="mt-1 text-[10px] font-semibold text-[#7C899A]">{country.code} · {country.idc}</p><div className="mt-5 flex items-center justify-between"><p className="text-lg font-bold text-[#1769F5]">{cash(country.final_price)}</p><span className="flex items-center gap-1 text-xs font-bold text-[#1769F5]">Review <ChevronRight size={15} /></span></div></button>; }
function Review({ country, balance, submitting, onBack, onConfirm }: { country: Country; balance: number; submitting: boolean; onBack: () => void; onConfirm: () => void }) { const shortfall = Math.max(0, country.final_price - balance); return <section className="panel-enter max-w-2xl"><Header eyebrow="BUY ACCOUNT · REVIEW" title={`Review ${country.name}`} text="This order spends verified wallet balance. The server rechecks country availability and final price before charging." onBack={onBack} /><div className="thin-card mt-5 overflow-hidden rounded-[26px]"><div className="bg-gradient-to-br from-[#1769F5] via-[#2877F4] to-[#6DA4FF] p-5 text-white"><div className="flex items-center gap-3"><span className="grid h-12 w-12 place-items-center rounded-2xl bg-white/15 text-2xl">{country.flag}</span><div><p className="text-[10px] font-bold tracking-[.15em] text-white/70">LIVE INVENTORY REVIEW</p><p className="mt-1 text-xl font-bold">{country.name} · {country.code}</p></div></div></div><div className="p-5"><div className="divide-y divide-[#E8EEF6] rounded-2xl border border-[#DEE7F3] bg-[#FBFDFF] px-4">{[["Country prefix", country.idc], ["Live listed price", cash(country.final_price)], ["Your wallet balance", cash(balance)]].map(([label, value]) => <div key={label} className="flex justify-between gap-4 py-4 text-sm"><span className="text-[#718096]">{label}</span><b className="text-right text-[#263247]">{value}</b></div>)}</div>{shortfall > 0 ? <p className="mt-4 rounded-2xl bg-[#FFF1F2] p-4 text-xs leading-5 text-[#C44759]">Add {cash(shortfall)} to your wallet before placing this order. No order will be created until sufficient balance is available.</p> : <p className="mt-4 rounded-2xl bg-[#E8F9F0] p-4 text-xs leading-5 text-[#287C57]">Balance is currently sufficient. Inventory and pricing will still be checked live when you confirm.</p>}<button disabled={submitting || shortfall > 0} onClick={onConfirm} className="blue-button pressable mt-5 flex h-12 w-full items-center justify-center gap-2 rounded-2xl text-sm font-bold disabled:opacity-40">{submitting ? <LoaderCircle size={17} className="animate-spin" /> : <CreditCard size={17} />}Confirm and create order</button></div></div></section>; }
function Delivery({ order, otp, terminating, terminated, onTerminate, onBack, onCopy }: { order: BuyResponse; otp: { status: string; code?: string; password?: string | null }; terminating: boolean; terminated: boolean; onTerminate: () => void; onBack: () => void; onCopy: (value: string, label: string) => void }) { const terminal = ["ready", "timeout", "expired", "unknown"].includes(otp.status); return <section className="panel-enter max-w-2xl"><Header eyebrow="BUY ACCOUNT · DELIVERY" title="Your order is active" text="Keep this page open while TgFox monitors delivery. The status below is read from the live order service." onBack={onBack} /><div className="thin-card mt-5 overflow-hidden rounded-[26px]"><div className="bg-[#EAF2FF] p-5"><div className="flex items-start gap-3"><span className="grid h-11 w-11 place-items-center rounded-2xl bg-[#1769F5] text-white"><CheckCircle2 size={22} /></span><div><p className="text-[10px] font-bold tracking-[.14em] text-[#1769F5]">ORDER CREATED</p><p className="mt-1 text-lg font-bold text-[#253148]">{order.country} · {order.phone}</p><p className="mt-1 text-xs text-[#6E7F97]">Paid {cash(order.price_paid)} from verified wallet balance.</p></div></div></div><div className="p-5"><CopyLine label="Order ID" value={order.order_id} onCopy={onCopy} /><CopyLine label="Phone number" value={order.phone} onCopy={onCopy} /><div className={`mt-4 rounded-2xl p-4 ${otp.status === "ready" ? "bg-[#E8F9F0]" : terminal ? "bg-[#FFF4E5]" : "bg-[#F6FAFF]"}`}><p className="text-[10px] font-bold tracking-[.14em] text-[#1769F5]">LIVE OTP STATUS</p><p className="mt-2 text-sm font-bold text-[#273144]">{otp.status === "ready" ? "OTP received · Order completed" : terminal ? `Delivery ${otp.status}` : "Waiting for Telegram OTP"}</p>{otp.code ? <CopyLine label="Latest OTP" value={otp.code} onCopy={onCopy} /> : <p className="mt-2 text-xs leading-5 text-[#65748A]">{terminal ? "This delivery state is final. Open Activity & orders or contact support if you need help." : "We check automatically; no refresh button is needed."}</p>}{otp.password ? <CopyLine label="2FA password" value={otp.password} onCopy={onCopy} /> : null}{otp.status === "ready" ? <div className="mt-4 border-t border-[#CFEBDD] pt-4"><p className="text-xs leading-5 text-[#4D7867]">{terminated ? "The platform session has been terminated. Your own logged-in device is not affected." : "After you log in, you can optionally terminate the platform session from here."}</p>{!terminated ? <button disabled={terminating} onClick={onTerminate} className="pressable mt-3 flex h-11 w-full items-center justify-center gap-2 rounded-xl border border-[#E6B7B7] bg-[#FFF7F7] text-xs font-bold text-[#BE4A56] disabled:opacity-50">{terminating ? <LoaderCircle size={15} className="animate-spin" /> : null}{terminating ? "Terminating platform session…" : "Terminate Bot Session"}</button> : null}</div> : null}</div></div></div></section>; }
function CopyLine({ label, value, onCopy }: { label: string; value: string; onCopy: (value: string, label: string) => void }) { return <div className="mt-3 flex items-center gap-3 rounded-2xl border border-[#DCE7F5] bg-white p-4"><div className="min-w-0 flex-1"><p className="text-[10px] font-bold uppercase tracking-wide text-[#7C899B]">{label}</p><p className="mt-1 break-all font-mono text-xs font-bold text-[#344057]">{value}</p></div><button onClick={() => onCopy(value, label)} className="pressable grid h-9 w-9 place-items-center rounded-xl bg-[#EAF2FF] text-[#1769F5]" aria-label={`Copy ${label}`}><Copy size={15} /></button></div>; }
function Header({ eyebrow, title, text, onBack }: { eyebrow: string; title: string; text: string; onBack: () => void }) { return <div className="flex items-start gap-3"><button onClick={onBack} className="pressable grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-[#E1E8F3] bg-white text-[#1769F5]" aria-label="Back"><ArrowLeft size={18} /></button><div><p className="text-[10px] font-bold tracking-[.16em] text-[#1769F5]">{eyebrow}</p><h1 className="mt-1 text-[23px] font-bold tracking-[-.04em] text-[#202633]">{title}</h1><p className="mt-1 max-w-xl text-xs leading-5 text-[#7B8799]">{text}</p></div></div>; }
