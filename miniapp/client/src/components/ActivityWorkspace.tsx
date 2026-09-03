/** TgFox Blue Console: real order, OTP and transaction history workspace. */
import { useEffect, useState } from "react";
import { BadgeCheck, Copy, KeyRound, LoaderCircle, RefreshCw, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import { TgFoxApiError, type BuyResponse, type LedgerItem, type RecordResponse, tgfoxApi } from "@/lib/api";

const cash = (n: number) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 }).format(n || 0);
const date = (value: string) => value ? new Intl.DateTimeFormat("en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value)) : "—";

export function ActivityWorkspace({ records, liveOrder, onCopy }: { records: RecordResponse | null; liveOrder: BuyResponse | null; onCopy: (value: string) => void }) {
  const [ledger, setLedger] = useState<LedgerItem[]>([]);
  const [busy, setBusy] = useState(true);
  const [otp, setOtp] = useState<{ status: string; code?: string; password?: string | null } | null>(null);
  const load = async (canUpdate: () => boolean = () => true) => {
    const data = await tgfoxApi.transactions();
    if (canUpdate()) setLedger(data.items);
  };
  useEffect(() => {
    let active = true;
    load(() => active)
      .catch((error: Error) => { if (active) toast.error(error.message); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (!liveOrder) return;
    let active = true;
    let polling = false;
    let pollId: number | undefined;
    const stop = () => { if (pollId !== undefined) window.clearInterval(pollId); };
    const poll = async () => {
      if (!active || polling) return;
      polling = true;
      try {
        const result = await tgfoxApi.orderOtp(liveOrder.order_id);
        if (!active) return;
        setOtp({ status: result.otp_status, code: result.otp, password: result.password });
        if (["ready", "timeout", "expired"].includes(result.otp_status)) stop();
      } catch (error) {
        if (!active) return;
        const terminalStatus = error instanceof TgFoxApiError
          ? ({ 404: "unknown", 408: "timeout", 410: "expired" } as Record<number, string>)[error.status || 0]
          : undefined;
        if (terminalStatus) {
          setOtp({ status: terminalStatus });
          stop();
          return;
        }
        toast.error(error instanceof Error ? error.message : "OTP status unavailable.");
      } finally {
        polling = false;
      }
    };
    pollId = window.setInterval(() => { void poll(); }, 8000);
    void poll();
    return () => { active = false; stop(); };
  }, [liveOrder]);
  return <section className="panel-enter"><div className="flex justify-between gap-4"><div><h1 className="text-[22px] font-bold tracking-[-.03em]">Activity & orders</h1><p className="mt-1 text-xs text-[#8A93A4]">Track real number orders, OTP state and wallet ledger entries.</p></div><button onClick={() => { setBusy(true); load().catch((e: Error) => toast.error(e.message)).finally(() => setBusy(false)); }} className="grid h-9 w-9 place-items-center rounded-lg border border-[#E6EAF1] bg-white text-[#1769F5]"><RefreshCw size={15} /></button></div>
    {liveOrder && <OrderCard order={liveOrder} otp={otp} onCopy={onCopy} />}
    <div className="mt-4 grid gap-4 lg:grid-cols-2"><div className="thin-card rounded-2xl p-4"><p className="flex items-center gap-2 text-sm font-bold"><BadgeCheck size={16} className="text-[#1769F5]" /> Account orders</p><div className="mt-3 divide-y divide-[#EDF0F5]">{(records?.purchases || []).map(item => <div key={item.id} className="grid grid-cols-[1.1fr_.7fr_.6fr] items-center gap-2 py-3"><div><p className="text-xs font-semibold">{item.id}</p><p className="mt-0.5 text-[9px] text-[#8791A1]">{date(item.at)}</p></div><p className="text-xs font-bold">{cash(item.amount)}</p><p className="text-[9px] font-bold text-[#218762]">{item.status.toUpperCase()}</p></div>)}{!records?.purchases.length && <p className="py-6 text-center text-xs text-[#8A93A4]">No account orders yet.</p>}</div></div>
      <div className="thin-card rounded-2xl p-4"><p className="flex items-center gap-2 text-sm font-bold"><ShieldCheck size={16} className="text-[#1769F5]" /> Wallet ledger</p><div className="mt-3 divide-y divide-[#EDF0F5]">{ledger.slice(0,7).map(item => <div key={item.id} className="flex items-center justify-between gap-3 py-3"><div className="min-w-0"><p className="text-xs font-semibold capitalize">{item.type}</p><p className="mt-0.5 truncate text-[9px] text-[#8791A1]">{item.note || item.reference_id || date(item.created_at)}</p></div><div className="text-right"><p className={`text-xs font-bold ${["deposit", "sale", "refund", "bonus", "referral"].includes(item.type) ? "text-[#218762]" : "text-[#263044]"}`}>{["deposit", "sale", "refund", "bonus", "referral"].includes(item.type) ? "+" : "−"}{cash(item.amount)}</p><p className="mt-0.5 text-[9px] text-[#8791A1]">{item.status}</p></div></div>)}{busy && <p className="py-6 text-center text-xs text-[#8A93A4]">Loading activity…</p>}{!busy && !ledger.length && <p className="py-6 text-center text-xs text-[#8A93A4]">No wallet entries yet.</p>}</div></div></div>
  </section>;
}
function OrderCard({ order, otp, onCopy }: { order: BuyResponse; otp: { status: string; code?: string; password?: string | null } | null; onCopy: (value: string) => void }) { return <div className="thin-card mt-4 rounded-2xl border-[#C7DBFF] bg-[#F4F8FF] p-4"><div className="flex justify-between gap-4"><div><p className="text-[10px] font-bold text-[#1769F5]">LIVE PURCHASE DELIVERY</p><p className="mt-1 text-sm font-bold">{order.country} · {order.phone}</p><p className="mt-1 text-[10px] text-[#71809A]">Order {order.order_id}</p></div><button onClick={() => onCopy(order.order_id)} className="text-[#1769F5]"><Copy size={16} /></button></div><div className="mt-4 grid gap-3 sm:grid-cols-2"><OtpCell label="OTP status" value={otp?.status || "waiting"} /><OtpCell label="Latest OTP" value={otp?.code || "Waiting for Telegram code"} copy={otp?.code} onCopy={onCopy} />{otp?.password && <OtpCell label="2FA password" value={otp.password} copy={otp.password} onCopy={onCopy} />}</div></div>; }
function OtpCell({ label, value, copy, onCopy }: { label: string; value: string; copy?: string; onCopy?: (value: string) => void }) { return <div className="rounded-xl bg-white p-3"><p className="text-[9px] font-semibold text-[#8791A1]">{label.toUpperCase()}</p><div className="mt-1 flex items-center gap-2"><p className="min-w-0 flex-1 truncate text-xs font-bold">{value}</p>{copy && onCopy && <button onClick={() => onCopy(copy)} className="text-[#1769F5]"><Copy size={14} /></button>}</div></div>; }
