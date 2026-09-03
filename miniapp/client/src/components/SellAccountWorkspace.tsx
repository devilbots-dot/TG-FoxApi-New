/**
 * TgFox Sell Account UX: a full-page seller control centre backed by real request
 * statuses. Secure phone/OTP/2FA verification remains exclusively in the bot.
 */
import { useEffect, useState } from "react";
import { ArrowLeft, ArrowUpRight, ChevronRight, Copy, LoaderCircle, RefreshCw, Store } from "lucide-react";
import { toast } from "sonner";
import { type SellRequest, tgfoxApi } from "@/lib/api";
import { openTelegramLink } from "@/lib/tg";

const money = (amount: number) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 }).format(amount || 0);
const stamp = (value: string) => value ? new Intl.DateTimeFormat("en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value)) : "—";
const label = (value: string) => (value || "pending").replaceAll("_", " ").toUpperCase();
const tone = (value: string) => value.includes("reject") || value.includes("fail") ? "bg-[#FFF0F1] text-[#C44B5D]" : value.includes("released") || value.includes("completed") ? "bg-[#E7F8EF] text-[#278A5C]" : "bg-[#FFF4DF] text-[#A76D08]";

export function SellAccountWorkspace({ botUsername, onBack }: { botUsername: string; onBack: () => void }) {
  const [requests, setRequests] = useState<SellRequest[]>([]);
  const [busy, setBusy] = useState(true);
  const [selected, setSelected] = useState<SellRequest | null>(null);
  const load = async (canUpdate: () => boolean = () => true) => {
    const data = await tgfoxApi.sellRequests();
    if (canUpdate()) setRequests(data.items);
  };
  useEffect(() => {
    let active = true;
    load(() => active).catch((error: Error) => { if (active) toast.error(error.message); }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, []);
  const refresh = () => { setBusy(true); load().catch((error: Error) => toast.error(error.message)).finally(() => setBusy(false)); };
  const begin = () => {
    if (!botUsername) return toast.error("The TG FOX bot link is not configured yet.");
    openTelegramLink(`https://t.me/${botUsername}?start=sell`);
  };
  const copy = (value: string, name: string) => navigator.clipboard.writeText(value).then(() => toast.success(`${name} copied.`)).catch(() => toast.error("Copy is unavailable in this browser."));
  if (busy) return <Loading />;
  if (selected) return <RequestDetail request={selected} onBack={() => setSelected(null)} onCopy={copy} />;
  return <section className="panel-enter max-w-4xl">
    <Header eyebrow="SELL ACCOUNT" title="Your seller control center" text="Start secure account verification in the bot, then track every real seller request here." onBack={onBack} onRefresh={refresh} />
    <section className="thin-card mt-5 overflow-hidden rounded-[26px]">
      <div className="bg-gradient-to-br from-[#203E70] via-[#1769F5] to-[#6DA4FF] p-5 text-white">
        <p className="text-[10px] font-bold tracking-[.16em] text-white/70">SECURE SELLER FLOW</p>
        <h2 className="mt-2 text-xl font-bold tracking-[-.03em]">Verify once. Track everything here.</h2>
        <p className="mt-2 max-w-xl text-xs leading-5 text-white/80">Phone, OTP, and 2FA are entered only through the protected TG FOX bot flow. This Mini App never asks you to paste a Telegram session.</p>
        <button onClick={begin} className="pressable mt-5 flex h-12 items-center gap-2 rounded-2xl bg-white px-5 text-sm font-bold text-[#1769F5]"><ArrowUpRight size={17} />Start secure sell verification</button>
      </div>
      <div className="grid gap-3 p-5 sm:grid-cols-3"><Step number="1" title="Verify in bot" text="Phone, OTP and 2FA are checked securely." /><Step number="2" title="Review request" text="Eligibility, account health and pricing are validated." /><Step number="3" title="Track release" text="Follow pending, review, and payment status here." /></div>
    </section>
    <section className="mt-5">
      <div className="flex items-center justify-between"><div><p className="text-[10px] font-bold tracking-[.16em] text-[#1769F5]">LIVE SELLER STATUS</p><h2 className="mt-1 text-lg font-bold text-[#263247]">My seller requests</h2></div><span className="rounded-full bg-[#EAF2FF] px-3 py-1.5 text-[10px] font-bold text-[#1769F5]">{requests.length} REQUEST{requests.length === 1 ? "" : "S"}</span></div>
      <div className="mt-3 grid gap-3">{requests.map((request) => <RequestCard key={request.id} request={request} onOpen={() => setSelected(request)} />)}{requests.length === 0 && <Empty onBegin={begin} />}</div>
    </section>
  </section>;
}

function RequestCard({ request, onOpen }: { request: SellRequest; onOpen: () => void }) { const state = request.lifecycle_status || request.status; const amount = request.final_price ?? request.pending_amount ?? request.offer_price; return <button onClick={onOpen} className="pressable thin-card flex items-center gap-4 rounded-[22px] p-4 text-left transition hover:border-[#B8D1F8]"><span className="grid h-11 w-11 shrink-0 place-items-center rounded-2xl bg-[#EAF2FF] text-[#1769F5]"><Store size={19} /></span><div className="min-w-0 flex-1"><p className="text-sm font-bold text-[#263247]">{request.country || request.code || "Seller request"}</p><p className="mt-1 truncate text-[10px] font-semibold text-[#7A879A]">{request.phone || request.id} · submitted {stamp(request.submitted_at)}</p></div><div className="text-right"><p className="text-sm font-bold text-[#263247]">{money(amount)}</p><span className={`mt-1 inline-block rounded-full px-2 py-1 text-[9px] font-bold ${tone(state)}`}>{label(state)}</span></div><ChevronRight size={17} className="shrink-0 text-[#1769F5]" /></button>; }
function Empty({ onBegin }: { onBegin: () => void }) { return <div className="thin-card rounded-[22px] p-7 text-center"><Store className="mx-auto text-[#1769F5]" size={26} /><p className="mt-3 text-sm font-bold text-[#263247]">No seller requests yet</p><p className="mx-auto mt-2 max-w-sm text-xs leading-5 text-[#758198]">Start the secure bot verification when you are ready. Only submitted real requests will appear here.</p><button onClick={onBegin} className="blue-button pressable mt-5 h-11 rounded-xl px-5 text-xs font-bold">Open secure seller flow</button></div>; }
function RequestDetail({ request, onBack, onCopy }: { request: SellRequest; onBack: () => void; onCopy: (value: string, label: string) => void }) { const state = request.lifecycle_status || request.status; const amount = request.final_price ?? request.pending_amount ?? request.offer_price; return <section className="panel-enter max-w-2xl"><Header eyebrow="SELL ACCOUNT · REQUEST" title={request.country || request.code || "Seller request"} text="This page displays the real lifecycle data recorded for your request." onBack={onBack} /><div className="thin-card mt-5 overflow-hidden rounded-[26px]"><div className="bg-[#EAF2FF] p-5"><div className="flex items-start justify-between gap-3"><div><p className="text-[10px] font-bold tracking-[.15em] text-[#1769F5]">REQUEST STATUS</p><p className="mt-1 text-xl font-bold text-[#263247]">{label(state)}</p></div><span className={`rounded-full px-3 py-1.5 text-[10px] font-bold ${tone(state)}`}>{label(state)}</span></div></div><div className="p-5"><CopyRow label="Request ID" value={request.id} onCopy={onCopy} /><CopyRow label="Account phone" value={request.phone || "Not available"} onCopy={onCopy} /><Info label="Quoted amount" value={money(amount)} /><Info label="Submitted" value={stamp(request.submitted_at)} />{request.reviewed_at ? <Info label="Reviewed" value={stamp(request.reviewed_at)} /> : null}{request.payment_release_at ? <Info label="Payment release" value={stamp(request.payment_release_at)} /> : null}{request.admin_note ? <div className="mt-4 rounded-2xl border border-[#DCE7F5] bg-[#FBFDFF] p-4"><p className="text-[10px] font-bold uppercase tracking-wide text-[#7C899B]">Admin note</p><p className="mt-2 text-xs leading-5 text-[#5F7088]">{request.admin_note}</p></div> : null}<p className="mt-4 rounded-2xl bg-[#F6FAFF] p-4 text-xs leading-5 text-[#60728D]">Seller request status is read from the server. Payment is never estimated or shown as completed until the real lifecycle reaches its final state.</p></div></div></section>; }
function Info({ label: title, value }: { label: string; value: string }) { return <div className="flex justify-between gap-4 border-b border-[#E9EFF6] py-4 text-sm"><span className="text-[#718096]">{title}</span><b className="text-right text-[#263247]">{value}</b></div>; }
function CopyRow({ label: title, value, onCopy }: { label: string; value: string; onCopy: (value: string, label: string) => void }) { return <div className="mt-3 flex items-center gap-3 rounded-2xl border border-[#DCE7F5] bg-white p-4"><div className="min-w-0 flex-1"><p className="text-[10px] font-bold uppercase tracking-wide text-[#7C899B]">{title}</p><p className="mt-1 break-all font-mono text-xs font-bold text-[#344057]">{value}</p></div><button onClick={() => onCopy(value, title)} className="pressable grid h-9 w-9 place-items-center rounded-xl bg-[#EAF2FF] text-[#1769F5]" aria-label={`Copy ${title}`}><Copy size={15} /></button></div>; }
function Step({ number, title, text }: { number: string; title: string; text: string }) { return <div className="rounded-2xl border border-[#E1EAF6] bg-[#FBFDFF] p-4"><span className="grid h-7 w-7 place-items-center rounded-lg bg-[#EAF2FF] text-[11px] font-bold text-[#1769F5]">{number}</span><p className="mt-3 text-xs font-bold text-[#263247]">{title}</p><p className="mt-1 text-[11px] leading-5 text-[#718096]">{text}</p></div>; }
function Header({ eyebrow, title, text, onBack, onRefresh }: { eyebrow: string; title: string; text: string; onBack: () => void; onRefresh?: () => void }) { return <div className="flex items-start justify-between gap-3"><div className="flex min-w-0 gap-3"><button onClick={onBack} className="pressable grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-[#E1E8F3] bg-white text-[#1769F5]" aria-label="Back"><ArrowLeft size={18} /></button><div><p className="text-[10px] font-bold tracking-[.16em] text-[#1769F5]">{eyebrow}</p><h1 className="mt-1 text-[23px] font-bold tracking-[-.04em] text-[#202633]">{title}</h1><p className="mt-1 max-w-xl text-xs leading-5 text-[#7B8799]">{text}</p></div></div>{onRefresh ? <button onClick={onRefresh} className="pressable grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-[#E1E8F3] bg-white text-[#1769F5]" aria-label="Refresh"><RefreshCw size={16} /></button> : null}</div>; }
function Loading() { return <div className="grid min-h-[380px] place-items-center"><div className="text-center"><LoaderCircle className="mx-auto animate-spin text-[#1769F5]" /><p className="mt-3 text-xs font-medium text-[#7B8799]">Loading seller requests</p></div></div>; }
