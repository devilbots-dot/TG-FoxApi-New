/**
 * TgFox Secure Withdrawal Flow: deliberate live-balance withdrawal journey
 * built around saved-address verification, server-configured fees, and clear confirmation.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowLeft, ArrowUpRight, CheckCircle2, ChevronRight, LoaderCircle, RefreshCw, Save, ShieldCheck, WalletCards } from "lucide-react";
import { toast } from "sonner";
import { type WalletOverview, type Withdrawal, tgfoxApi } from "@/lib/api";

const money = (value: number) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 }).format(value || 0);
const stamp = (date?: string) => date ? new Intl.DateTimeFormat("en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(date)) : "—";

export function WithdrawalWorkspace({ onBack }: { onBack: () => void }) {
  const [wallet, setWallet] = useState<WalletOverview | null>(null);
  const [withdrawals, setWithdrawals] = useState<Withdrawal[]>([]);
  const [network, setNetwork] = useState("");
  const [address, setAddress] = useState("");
  const [amount, setAmount] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(true);
  const [working, setWorking] = useState(false);
  const actionInFlight = useRef(false);

  const load = async (canUpdate: () => boolean = () => true) => {
    const [walletResult, historyResult] = await Promise.allSettled([tgfoxApi.wallet(), tgfoxApi.withdrawals()]);
    if (!canUpdate()) return;
    if (walletResult.status === "rejected") throw walletResult.reason;
    const summary = walletResult.value;
    setWallet(summary);
    const nextNetwork = network || summary.withdrawal.networks[0] || "";
    setNetwork(nextNetwork);
    setAddress(summary.addresses[nextNetwork] || "");
    if (historyResult.status === "fulfilled") setWithdrawals(historyResult.value.items);
    else toast.error("Withdrawal history is temporarily unavailable. Live balance data is still shown.");
  };

  useEffect(() => {
    let active = true;
    load(() => active).catch((error: Error) => { if (active) toast.error(error.message); }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, []);

  const savedAddress = wallet?.addresses[network] || "";
  const gross = Number(amount) || 0;
  const feePreview = useMemo(() => wallet ? Math.max(0, gross * (wallet.withdrawal.fee_percent / 100) + wallet.withdrawal.fee_fixed) : 0, [gross, wallet]);
  const netPreview = Math.max(0, gross - feePreview);
  const exceedsMaximum = Boolean(wallet?.withdrawal.maximum && gross > wallet.withdrawal.maximum);
  const saveAddress = async () => {
    if (!network || !address.trim()) return toast.error("Choose a network and enter a destination address.");
    if (actionInFlight.current) return;
    actionInFlight.current = true;
    setWorking(true);
    try {
      await tgfoxApi.saveWithdrawalAddress({ network, address: address.trim() });
      toast.success("Withdrawal address saved.");
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Address could not be saved.");
    } finally {
      actionInFlight.current = false;
      setWorking(false);
    }
  };
  const requestWithdrawal = async () => {
    if (!confirmed) return toast.error("Confirm the saved address and network first.");
    if (!gross || gross < (wallet?.withdrawal.minimum || 0)) return toast.error(`Minimum withdrawal is ${money(wallet?.withdrawal.minimum || 0)}.`);
    if (exceedsMaximum) return toast.error(`Maximum withdrawal is ${money(wallet?.withdrawal.maximum || 0)}.`);
    if (!savedAddress || address.trim() !== savedAddress) return toast.error("Save this destination address before requesting withdrawal.");
    if (gross > (wallet?.net_spendable || 0)) return toast.error("Withdrawal amount exceeds your verified spendable balance.");
    if (actionInFlight.current) return;
    actionInFlight.current = true;
    setWorking(true);
    try {
      const result = await tgfoxApi.withdraw({ network, amount: gross });
      toast.success(`Withdrawal ${result.withdrawal_id} submitted.`);
      setAmount("");
      setConfirmed(false);
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Withdrawal could not be created.");
    } finally {
      actionInFlight.current = false;
      setWorking(false);
    }
  };

  if (busy || !wallet) return <WithdrawalLoading />;
  return <section className="panel-enter max-w-4xl">
    <WithdrawalHeader onBack={onBack} onRefresh={() => { setBusy(true); load().catch((error: Error) => toast.error(error.message)).finally(() => setBusy(false)); }} />
    <div className="mt-4 grid gap-4 lg:grid-cols-[1.35fr_.65fr]">
      <div className="thin-card overflow-hidden rounded-[24px] bg-white"><div className="border-b border-[#F1DFE1] bg-gradient-to-br from-[#1B315F] via-[#23477E] to-[#3074B9] p-5 text-white"><div className="flex items-start justify-between gap-4"><div><p className="text-[10px] font-bold tracking-[.16em] text-[#BBD8FF]">VERIFY BEFORE REQUESTING</p><h2 className="mt-2 text-xl font-bold tracking-[-.03em]">Send USDT with confidence</h2><p className="mt-1 text-xs leading-5 text-white/75">Your network and saved address are checked before a request reaches the server.</p></div><div className="grid h-11 w-11 place-items-center rounded-2xl bg-white/15"><ArrowUpRight size={21} /></div></div></div><div className="p-4 sm:p-5">
        <p className="text-[11px] font-bold tracking-[.12em] text-[#748197]">STEP 1 · NETWORK</p><div className="mt-3 flex flex-wrap gap-2">{wallet.withdrawal.networks.map((item) => <button key={item} onClick={() => { setNetwork(item); setAddress(wallet.addresses[item] || ""); setConfirmed(false); }} className={`pressable rounded-xl border px-3 py-2 text-xs font-bold ${network === item ? "border-[#1769F5] bg-[#1769F5] text-white" : "border-[#D9E4F4] bg-white text-[#546176]"}`}>{item}</button>)}</div>
        <p className="mt-6 text-[11px] font-bold tracking-[.12em] text-[#748197]">STEP 2 · DESTINATION</p><div className="mt-3 rounded-2xl border border-[#DFE8F6] bg-[#FAFCFF] p-4"><div className="flex items-center justify-between gap-3"><div><p className="text-sm font-bold text-[#273144]">Saved USDT address</p><p className="mt-1 text-[11px] text-[#7B8799]">Save the exact address before sending a withdrawal request.</p></div><span className={`rounded-full px-2.5 py-1 text-[10px] font-bold ${savedAddress && address.trim() === savedAddress ? "bg-[#EAF8F1] text-[#1E8B63]" : "bg-[#FFF4E6] text-[#C87911]"}`}>{savedAddress && address.trim() === savedAddress ? "SAVED" : "NOT SAVED"}</span></div><input value={address} onChange={(event) => { setAddress(event.target.value); setConfirmed(false); }} className="field mt-3" placeholder="USDT wallet address" /><button disabled={working || !network || !address.trim()} onClick={saveAddress} className="pressable mt-3 flex h-10 w-full items-center justify-center gap-2 rounded-xl border border-[#BFD8FF] bg-[#F2F7FF] text-xs font-bold text-[#1769F5] disabled:opacity-40">{working ? <LoaderCircle size={15} className="animate-spin" /> : <Save size={15} />} Save this address</button></div>
        <p className="mt-6 text-[11px] font-bold tracking-[.12em] text-[#748197]">STEP 3 · AMOUNT</p><div className="mt-3 rounded-2xl border border-[#DFE8F6] bg-[#FAFCFF] p-4"><div className="flex items-center justify-between gap-3"><label htmlFor="withdrawal-amount" className="text-sm font-bold text-[#273144]">Withdrawal amount</label><span className="text-[10px] font-semibold text-[#6F7D92]">Available {money(wallet.net_spendable)}</span></div><div className="mt-3 flex items-center rounded-2xl border border-[#D4E0F1] bg-white px-4 shadow-sm focus-within:border-[#1769F5] focus-within:ring-4 focus-within:ring-[#1769F5]/10"><span className="text-lg font-bold text-[#1769F5]">$</span><input id="withdrawal-amount" value={amount} onChange={(event) => { setAmount(event.target.value); setConfirmed(false); }} inputMode="decimal" className="h-14 min-w-0 flex-1 bg-transparent px-3 text-xl font-bold tabular-nums outline-none" placeholder="0.00" /><span className="text-xs font-bold text-[#7B879A]">USD</span></div><div className="mt-3 grid grid-cols-3 gap-2 text-center"><Metric label="Minimum" value={money(wallet.withdrawal.minimum)} /><Metric label="Estimated fee" value={money(feePreview)} /><Metric label="You receive" value={money(netPreview)} emphasis /></div>{exceedsMaximum ? <p className="mt-3 text-[11px] font-semibold text-[#C25161]">Amount exceeds the configured maximum of {money(wallet.withdrawal.maximum)}.</p> : null}</div>
        <label className="mt-5 flex items-start gap-3 rounded-2xl border border-[#E4EAF3] bg-white p-3.5 text-xs leading-5 text-[#647289]"><input checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} type="checkbox" className="mt-0.5 h-4 w-4 accent-[#1769F5]" /><span><b className="block text-[#354056]">I confirm this {network} address is mine.</b>Crypto transfers can be irreversible. I have verified the selected network, saved address, amount, and estimated fee.</span></label>
        <button disabled={working || !confirmed || !savedAddress || address.trim() !== savedAddress} onClick={requestWithdrawal} className="blue-button pressable mt-4 flex h-12 w-full items-center justify-center gap-2 rounded-2xl text-sm font-bold disabled:opacity-40">{working ? <LoaderCircle size={17} className="animate-spin" /> : <ArrowUpRight size={17} />} Request withdrawal <ChevronRight size={17} /></button>
      </div></div>
      <WithdrawalSafety wallet={wallet} />
    </div>
    <RecentWithdrawals items={withdrawals} />
  </section>;
}

function WithdrawalHeader({ onBack, onRefresh }: { onBack: () => void; onRefresh: () => void }) { return <div className="flex items-start justify-between gap-3"><div className="flex min-w-0 gap-3"><button onClick={onBack} aria-label="Back to wallet" className="pressable grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-[#E1E8F3] bg-white text-[#1769F5]"><ArrowLeft size={18} /></button><div><p className="text-[10px] font-bold tracking-[.16em] text-[#1769F5]">WITHDRAW USDT</p><h1 className="mt-1 text-[23px] font-bold tracking-[-.04em] text-[#202633]">Move funds with clarity</h1><p className="mt-1 max-w-xl text-xs leading-5 text-[#7B8799]">Save your destination, review the real fee estimate, and explicitly confirm before you request.</p></div></div><button onClick={onRefresh} aria-label="Refresh withdrawal information" className="pressable grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-[#E1E8F3] bg-white text-[#1769F5]"><RefreshCw size={16} /></button></div>; }
function Metric({ label, value, emphasis = false }: { label: string; value: string; emphasis?: boolean }) { return <div className={`rounded-xl px-2 py-2.5 ${emphasis ? "bg-[#EAF3FF]" : "bg-white"}`}><p className="text-[9px] font-semibold text-[#7D899B]">{label}</p><p className={`mt-1 text-[11px] font-bold ${emphasis ? "text-[#1769F5]" : "text-[#354056]"}`}>{value}</p></div>; }
function WithdrawalSafety({ wallet }: { wallet: WalletOverview }) { return <aside className="space-y-4"><div className="thin-card rounded-[24px] bg-[#152E5E] p-5 text-white"><p className="flex items-center gap-2 text-[10px] font-bold tracking-[.14em] text-[#AFCBFF]"><WalletCards size={14} /> VERIFIED SPENDABLE</p><p className="mt-3 text-3xl font-bold tabular-nums">{money(wallet.net_spendable)}</p><p className="mt-2 text-xs leading-5 text-white/65">Reserved funds are excluded from the amount available to withdraw.</p></div><div className="thin-card rounded-[24px] border-[#F0D9AD] bg-[#FFFBF2] p-5"><div className="flex gap-3"><span className="grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-[#FFF0CF] text-[#C87806]"><AlertTriangle size={17} /></span><div><p className="text-sm font-bold text-[#654A1B]">Before you continue</p><ul className="mt-2 space-y-2 text-[11px] leading-5 text-[#80683B]"><li>• Use only the selected {wallet.withdrawal.networks.join(" or ")} network.</li><li>• Save and re-check the destination address.</li><li>• Fee and limits below are supplied by the live server configuration.</li></ul></div></div></div><div className="thin-card rounded-[24px] p-5"><p className="text-sm font-bold text-[#273144]">Live limits</p><div className="mt-4 space-y-3 text-xs"><LimitRow label="Minimum" value={money(wallet.withdrawal.minimum)} /><LimitRow label="Maximum" value={wallet.withdrawal.maximum ? money(wallet.withdrawal.maximum) : "Provider limit"} /><LimitRow label="Fee" value={`${wallet.withdrawal.fee_percent}% + ${money(wallet.withdrawal.fee_fixed)}`} /></div></div></aside>; }
function LimitRow({ label, value }: { label: string; value: string }) { return <div className="flex items-center justify-between gap-3"><span className="text-[#7B8799]">{label}</span><b className="text-right text-[#354056]">{value}</b></div>; }
function RecentWithdrawals({ items }: { items: Withdrawal[] }) { return <div className="thin-card mt-5 rounded-[24px] p-5"><div className="flex items-center justify-between"><div><p className="text-sm font-bold text-[#263247]">Recent withdrawals</p><p className="mt-1 text-[11px] text-[#7B8799]">Live request status from your account.</p></div><ChevronRight size={17} className="text-[#8A97AA]" /></div><div className="mt-4 divide-y divide-[#EBEFF5]">{items.slice(0, 4).map((item) => <div key={item.withdrawal_id} className="flex items-center justify-between gap-3 py-3"><div className="min-w-0"><p className="truncate text-xs font-bold text-[#344057]">{item.network} withdrawal</p><p className="mt-1 text-[10px] text-[#8792A3]">{stamp(item.created_at)}</p></div><div className="text-right"><p className="text-xs font-bold text-[#273144]">{money(item.net_amount || item.amount)}</p><p className="mt-1 text-[10px] font-bold uppercase text-[#1769F5]">{item.status_label || item.status}</p></div></div>)}{!items.length ? <p className="py-8 text-center text-sm text-[#8792A3]">No withdrawal requests yet.</p> : null}</div></div>; }
function WithdrawalLoading() { return <div className="grid min-h-[380px] place-items-center"><div className="text-center"><LoaderCircle className="mx-auto animate-spin text-[#1769F5]" /><p className="mt-3 text-xs font-medium text-[#7B8799]">Loading verified withdrawal details</p></div></div>; }
