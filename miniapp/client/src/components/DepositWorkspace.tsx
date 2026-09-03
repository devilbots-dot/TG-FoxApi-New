/**
 * TgFox Deposit UX: mobile-first, white/blue method-first journeys.
 * Provider details replace the picker in a focused page instead of appending
 * instructions underneath a long generic deposit screen.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ChevronRight, CircleDollarSign, Copy, ExternalLink, Link2, LoaderCircle, RefreshCw, ShieldCheck, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { type Deposit, type WalletOverview, tgfoxApi } from "@/lib/api";
import { openTelegramLink } from "@/lib/tg";

type Method = WalletOverview["deposit_methods"][number];
type Screen = "methods" | "method";

const money = (value: number) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 }).format(value || 0);
const stamp = (value?: string) => value ? new Intl.DateTimeFormat("en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value)) : "—";
const isChain = (method: string) => method === "bep20_scan" || method === "trc20_scan";

function guide(id: string) {
  if (id === "oxapay") return { eyebrow: "HOSTED CHECKOUT", title: "Pay safely in OxaPay", text: "Create an invoice, then choose the available network inside OxaPay’s secure checkout.", action: "Create OxaPay invoice", Icon: CircleDollarSign };
  if (id === "bep20_scan") return { eyebrow: "DIRECT BSC PAYMENT", title: "Send USDT on BEP20", text: "Use the shown BSC address, then submit the transaction hash for on-chain verification.", action: "Create BEP20 instruction", Icon: ShieldCheck };
  if (id === "trc20_scan") return { eyebrow: "DIRECT TRON PAYMENT", title: "Send USDT on TRC20", text: "Use the shown TRON address, then submit the transaction ID for on-chain verification.", action: "Create TRC20 instruction", Icon: ShieldCheck };
  if (id === "binance_pay_tx") return { eyebrow: "BINANCE PAY", title: "Pay by Binance Order ID", text: "Send the exact amount to the TG FOX Binance Pay ID and verify with your completed Order ID.", action: "Create Binance instruction", Icon: Link2 };
  if (id === "telegram_stars") return { eyebrow: "TELEGRAM STARS", title: "Pay with Telegram Stars", text: "Create an official Stars instruction and continue in Telegram’s payment flow.", action: "Create Stars instruction", Icon: Sparkles };
  return { eyebrow: "SECURE PAYMENT", title: "Create payment instruction", text: "Create a live provider instruction and follow it exactly.", action: "Create instruction", Icon: ShieldCheck };
}

export function DepositWorkspace({ onBack }: { onBack: () => void }) {
  const [wallet, setWallet] = useState<WalletOverview | null>(null);
  const [deposits, setDeposits] = useState<Deposit[]>([]);
  const [screen, setScreen] = useState<Screen>("methods");
  const [methodId, setMethodId] = useState("");
  const [deposit, setDeposit] = useState<Deposit | null>(null);
  const [amount, setAmount] = useState("");
  const [hash, setHash] = useState("");
  const [orderId, setOrderId] = useState("");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const createInFlight = useRef(false);
  const verifyInFlight = useRef(false);

  const load = async (canUpdate: () => boolean = () => true) => {
    const [summary, history] = await Promise.allSettled([tgfoxApi.wallet(), tgfoxApi.deposits()]);
    if (!canUpdate()) return;
    if (summary.status === "rejected") throw summary.reason;
    setWallet(summary.value);
    if (history.status === "fulfilled") setDeposits(history.value.items);
    else toast.error("Deposit history is temporarily unavailable. Live methods are still available.");
  };

  useEffect(() => {
    let mounted = true;
    load(() => mounted).catch((error: Error) => { if (mounted) toast.error(error.message); }).finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
  }, []);

  const method = useMemo(() => wallet?.deposit_methods.find((item) => item.id === methodId) || null, [wallet, methodId]);
  const minimum = method?.min_amount ?? wallet?.deposit_minimum ?? 0;
  const refresh = () => { setLoading(true); load().catch((error: Error) => toast.error(error.message)).finally(() => setLoading(false)); };
  const copy = (value: string, label: string) => navigator.clipboard.writeText(value).then(() => toast.success(`${label} copied.`)).catch(() => toast.error("Copy is unavailable in this browser."));

  const openMethod = (next: Method) => {
    setMethodId(next.id);
    setAmount(String(next.min_amount));
    setHash(""); setOrderId("");
    setDeposit(deposits.find((item) => item.method === next.id && item.status === "pending") || null);
    setScreen("method");
  };
  const returnToMethods = () => { setDeposit(null); setHash(""); setOrderId(""); setScreen("methods"); };
  const create = async () => {
    const value = Number(amount);
    if (!method) return;
    if (!value || value < minimum) return toast.error(`Minimum deposit is ${money(minimum)}.`);
    if (createInFlight.current) return;
    createInFlight.current = true; setCreating(true);
    try {
      // Direct methods are themselves network-specific. OxaPay's hosted page
      // chooses its available routing after invoice creation, so no network is sent.
      const result = await tgfoxApi.createDeposit({ method: method.id, amount: value });
      setDeposit(result.deposit); setHash(""); setOrderId("");
      toast.success("Live payment instruction created.");
      await load();
    } catch (error) { toast.error(error instanceof Error ? error.message : "Deposit instruction could not be created."); }
    finally { createInFlight.current = false; setCreating(false); }
  };
  const verifyHash = async () => {
    if (!deposit || !hash.trim() || verifyInFlight.current) return;
    verifyInFlight.current = true; setVerifying(true);
    try {
      const result = await tgfoxApi.submitDepositTransactionHash(deposit.deposit_id, hash.trim());
      setDeposit(result.deposit);
      if (result.outcome === "completed") toast.success("Payment verified. Your wallet balance was credited.");
      else if (result.outcome === "pending") toast.message("Transaction is waiting for chain visibility or confirmations.");
      else toast.error("This transaction does not match the instruction. No balance was credited.");
      await load();
    } catch (error) { toast.error(error instanceof Error ? error.message : "Transaction verification could not be completed."); }
    finally { verifyInFlight.current = false; setVerifying(false); }
  };
  const verifyOrder = async () => {
    if (!deposit || !orderId.trim() || verifyInFlight.current) return;
    verifyInFlight.current = true; setVerifying(true);
    try {
      const result = await tgfoxApi.submitBinanceOrderId(deposit.deposit_id, orderId.trim());
      setDeposit(result.deposit); toast.success("Binance payment verified. Your wallet balance was credited."); await load();
    } catch (error) { toast.error(error instanceof Error ? error.message : "Binance Order ID verification could not be completed."); }
    finally { verifyInFlight.current = false; setVerifying(false); }
  };

  if (loading || !wallet) return <Loading />;
  if (screen === "methods") return <Picker methods={wallet.deposit_methods} deposits={deposits} onBack={onBack} onRefresh={refresh} onOpen={openMethod} />;
  if (!method) return <Loading />;
  return <MethodPage method={method} deposit={deposit} amount={amount} minimum={minimum} creating={creating} verifying={verifying} hash={hash} orderId={orderId} onBack={returnToMethods} onAmount={setAmount} onCreate={create} onHash={setHash} onOrder={setOrderId} onVerifyHash={verifyHash} onVerifyOrder={verifyOrder} onCopy={copy} />;
}

function Picker({ methods, deposits, onBack, onRefresh, onOpen }: { methods: Method[]; deposits: Deposit[]; onBack: () => void; onRefresh: () => void; onOpen: (method: Method) => void }) {
  return <section className="panel-enter max-w-4xl"><Header eyebrow="ADD FUNDS" title="Choose a payment method" text="Every method opens as its own focused payment page—no mixed instructions hidden below." onBack={onBack} onRefresh={onRefresh} /><div className="mt-5 grid gap-3 md:grid-cols-2">{methods.map((method) => { const item = guide(method.id); const pending = deposits.some((deposit) => deposit.method === method.id && deposit.status === "pending"); const Icon = item.Icon; return <button key={method.id} onClick={() => onOpen(method)} className="pressable thin-card min-h-[178px] rounded-[24px] p-5 text-left transition hover:-translate-y-0.5 hover:border-[#AFCBFF]"><div className="flex items-start justify-between gap-4"><span className="grid h-11 w-11 place-items-center rounded-2xl bg-[#EAF2FF] text-[#1769F5]"><Icon size={21} /></span><span className={`rounded-full px-2.5 py-1 text-[10px] font-bold ${pending ? "bg-[#FFF3DD] text-[#A76700]" : "bg-[#E7F8EF] text-[#278A5C]"}`}>{pending ? "RESUME" : "LIVE"}</span></div><p className="mt-5 text-[10px] font-bold tracking-[.14em] text-[#1769F5]">{item.eyebrow}</p><h2 className="mt-1 text-lg font-bold tracking-[-.03em] text-[#263247]">{method.name}</h2><p className="mt-2 text-xs leading-5 text-[#718097]">{item.text}</p><p className="mt-4 flex items-center justify-between text-xs font-bold text-[#1769F5]">From {money(method.min_amount)} <span className="flex items-center gap-1">Open <ChevronRight size={15} /></span></p></button>; })}</div>{!methods.length ? <p className="thin-card mt-5 rounded-[24px] border-[#FFD8D8] bg-[#FFF8F8] p-5 text-sm text-[#B54857]">No payment method is enabled. Do not send funds until a live method appears here.</p> : null}</section>;
}

function MethodPage({ method, deposit, amount, minimum, creating, verifying, hash, orderId, onBack, onAmount, onCreate, onHash, onOrder, onVerifyHash, onVerifyOrder, onCopy }: { method: Method; deposit: Deposit | null; amount: string; minimum: number; creating: boolean; verifying: boolean; hash: string; orderId: string; onBack: () => void; onAmount: (value: string) => void; onCreate: () => void; onHash: (value: string) => void; onOrder: (value: string) => void; onVerifyHash: () => void; onVerifyOrder: () => void; onCopy: (value: string, label: string) => void }) {
  const item = guide(method.id); const Icon = item.Icon;
  return <section className="panel-enter max-w-3xl"><Header eyebrow={item.eyebrow} title={item.title} text={item.text} onBack={onBack} /><div className="thin-card mt-5 overflow-hidden rounded-[26px]"><div className="bg-gradient-to-br from-[#1769F5] via-[#2877F4] to-[#6DA4FF] p-5 text-white"><div className="flex items-start justify-between gap-4"><div><p className="text-[10px] font-bold tracking-[.16em] text-white/70">{deposit ? "PAYMENT INSTRUCTION READY" : "STEP 1 OF 2"}</p><h2 className="mt-2 text-xl font-bold tracking-[-.03em]">{deposit ? `${money(deposit.amount)} via ${method.name}` : "Set your amount"}</h2></div><span className="grid h-11 w-11 place-items-center rounded-2xl bg-white/15"><Icon size={22} /></span></div></div>{deposit ? <Instruction deposit={deposit} hash={hash} orderId={orderId} verifying={verifying} onHash={onHash} onOrder={onOrder} onVerifyHash={onVerifyHash} onVerifyOrder={onVerifyOrder} onCopy={onCopy} /> : <Amount method={method} amount={amount} minimum={minimum} creating={creating} action={item.action} onAmount={onAmount} onCreate={onCreate} />}</div></section>;
}

function Amount({ method, amount, minimum, creating, action, onAmount, onCreate }: { method: Method; amount: string; minimum: number; creating: boolean; action: string; onAmount: (value: string) => void; onCreate: () => void }) { const oxapay = method.id === "oxapay"; return <div className="p-5"><div className="rounded-2xl border border-[#DFE8F6] bg-[#FAFCFF] p-4"><div className="flex items-center justify-between gap-3"><label className="text-sm font-bold text-[#273144]" htmlFor="deposit-amount">Deposit amount</label><span className="text-[10px] font-semibold text-[#6F7D92]">Minimum {money(minimum)}</span></div><div className="mt-3 flex items-center rounded-2xl border border-[#D4E0F1] bg-white px-4 focus-within:border-[#1769F5] focus-within:ring-4 focus-within:ring-[#1769F5]/10"><span className="text-lg font-bold text-[#1769F5]">$</span><input id="deposit-amount" value={amount} onChange={(event) => onAmount(event.target.value)} inputMode="decimal" className="h-14 min-w-0 flex-1 bg-transparent px-3 text-xl font-bold tabular-nums outline-none" placeholder="0.00" /><span className="text-xs font-bold text-[#7B879A]">USD</span></div><div className="mt-3 flex flex-wrap gap-2">{[minimum, 25, 50, 100].filter((value, index, all) => value > 0 && all.indexOf(value) === index).map((value) => <button key={value} onClick={() => onAmount(String(value))} className="pressable rounded-lg bg-[#EAF2FF] px-3 py-1.5 text-[11px] font-bold text-[#1769F5]">{money(value)}</button>)}</div></div><p className="mt-4 rounded-2xl border border-[#DCE8FA] bg-[#F6FAFF] p-4 text-xs leading-5 text-[#60728D]">{oxapay ? <><b className="text-[#1769F5]">No network selector here.</b> OxaPay shows its available networks only inside the secure hosted checkout.</> : <>This is the dedicated {method.name} page. Follow only the instruction shown after you continue.</>}</p><button disabled={creating} onClick={onCreate} className="blue-button pressable mt-5 flex h-12 w-full items-center justify-center gap-2 rounded-2xl text-sm font-bold disabled:opacity-40">{creating ? <LoaderCircle size={17} className="animate-spin" /> : <ChevronRight size={17} />}{action}</button></div>; }

function Instruction({ deposit, hash, orderId, verifying, onHash, onOrder, onVerifyHash, onVerifyOrder, onCopy }: { deposit: Deposit; hash: string; orderId: string; verifying: boolean; onHash: (value: string) => void; onOrder: (value: string) => void; onVerifyHash: () => void; onVerifyOrder: () => void; onCopy: (value: string, label: string) => void }) { const done = deposit.status === "completed"; const chain = isChain(deposit.method); const binance = deposit.method === "binance_pay_tx"; const gateway = deposit.method === "oxapay" || deposit.method === "telegram_stars"; const network = deposit.method === "bep20_scan" ? "BEP20" : "TRC20"; const state = done ? "VERIFIED" : deposit.transaction_hash_state === "pending" ? "AWAITING CONFIRMATIONS" : deposit.transaction_hash_state === "rejected" ? "HASH REJECTED" : "INSTRUCTION ACTIVE"; return <div className="p-5"><div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-[10px] font-bold tracking-[.15em] text-[#1769F5]">FOLLOW THIS METHOD ONLY</p><p className="mt-1 text-sm font-bold text-[#263247]">{done ? "Payment verified and wallet credited" : "Complete the instruction below"}</p></div><span className={`rounded-full px-3 py-1.5 text-[10px] font-bold ${done ? "bg-[#E7F8EF] text-[#228656]" : "bg-[#EAF2FF] text-[#1769F5]"}`}>{state}</span></div>{deposit.address ? <CopyField label="Payment address" value={deposit.address} onCopy={onCopy} /> : null}{deposit.payment_reference ? <CopyField label="Binance Pay ID" value={deposit.payment_reference} onCopy={onCopy} /> : null}{deposit.memo ? <CopyField label="Memo / reference" value={deposit.memo} onCopy={onCopy} /> : null}{deposit.instructions ? <p className="mt-4 rounded-2xl border border-[#DCE7F8] bg-[#F8FBFF] p-4 text-xs leading-5 text-[#5F718C]">{deposit.instructions}</p> : null}{deposit.expires_at ? <p className="mt-3 text-[11px] font-semibold text-[#8A6270]">Instruction expiry: {stamp(deposit.expires_at)}</p> : null}{gateway && !done ? <OpenButton label={deposit.method === "oxapay" ? "Open secure OxaPay checkout" : "Continue with Telegram Stars"} url={deposit.payment_url} /> : null}{chain && !done ? <HashBox network={network} value={hash} working={verifying} confirmations={deposit.transaction_confirmations} onChange={onHash} onSubmit={onVerifyHash} /> : null}{binance && !done ? <OrderBox value={orderId} working={verifying} onChange={onOrder} onSubmit={onVerifyOrder} /> : null}{done ? <p className="mt-5 rounded-2xl bg-[#E7F8EF] p-4 text-xs font-semibold leading-5 text-[#228656]">This payment is confirmed by the server. Your wallet balance has been credited.</p> : null}</div>; }

function CopyField({ label, value, onCopy }: { label: string; value: string; onCopy: (value: string, label: string) => void }) { return <div className="mt-4 rounded-2xl border border-[#DCE7F8] bg-white p-4"><p className="text-[10px] font-bold uppercase tracking-wide text-[#7C899B]">{label}</p><div className="mt-1 flex items-center gap-2"><p className="min-w-0 flex-1 break-all font-mono text-xs font-semibold text-[#344057]">{value}</p><button onClick={() => onCopy(value, label)} className="pressable grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-[#EAF2FF] text-[#1769F5]" aria-label={`Copy ${label}`}><Copy size={14} /></button></div></div>; }
function OpenButton({ label, url }: { label: string; url: string }) { return <button disabled={!url} onClick={() => openTelegramLink(url)} className="blue-button pressable mt-5 flex h-12 w-full items-center justify-center gap-2 rounded-2xl text-sm font-bold disabled:opacity-40"><ExternalLink size={17} />{label}</button>; }
function HashBox({ network, value, working, confirmations, onChange, onSubmit }: { network: string; value: string; working: boolean; confirmations: number | null; onChange: (value: string) => void; onSubmit: () => void }) { return <div className="mt-5 border-t border-[#E3EBF6] pt-5"><p className="text-[10px] font-bold tracking-[.15em] text-[#1769F5]">FINAL STEP · ON-CHAIN CHECK</p><h3 className="mt-1 text-sm font-bold text-[#263247]">Submit your {network} transaction hash</h3><p className="mt-1 text-xs leading-5 text-[#65748A]">The server checks USDT, receiver, amount, success status, and confirmations before crediting.</p><div className="mt-3 flex flex-col gap-2 sm:flex-row"><div className="flex min-w-0 flex-1 items-center rounded-xl border border-[#C8DAF2] bg-white px-3 focus-within:border-[#1769F5]"><Link2 size={16} className="shrink-0 text-[#1769F5]" /><input value={value} onChange={(event) => onChange(event.target.value)} autoCapitalize="none" autoCorrect="off" spellCheck={false} className="h-12 min-w-0 flex-1 bg-transparent px-3 font-mono text-xs outline-none" placeholder={network === "BEP20" ? "0x… transaction hash" : "64-character transaction ID"} /></div><button disabled={working || !value.trim()} onClick={onSubmit} className="blue-button pressable h-12 rounded-xl px-5 text-xs font-bold disabled:opacity-40">{working ? <LoaderCircle size={16} className="animate-spin" /> : "Verify on-chain"}</button></div>{confirmations !== null && confirmations !== undefined ? <p className="mt-3 text-[11px] font-semibold text-[#65748A]">Observed confirmations: {confirmations}</p> : null}</div>; }
function OrderBox({ value, working, onChange, onSubmit }: { value: string; working: boolean; onChange: (value: string) => void; onSubmit: () => void }) { return <div className="mt-5 border-t border-[#E3EBF6] pt-5"><p className="text-[10px] font-bold tracking-[.15em] text-[#1769F5]">FINAL STEP · BINANCE CHECK</p><h3 className="mt-1 text-sm font-bold text-[#263247]">Submit your Binance Order ID</h3><p className="mt-1 text-xs leading-5 text-[#65748A]">Find this Order ID in your Binance Pay transaction history after payment.</p><div className="mt-3 flex flex-col gap-2 sm:flex-row"><input value={value} onChange={(event) => onChange(event.target.value)} autoCapitalize="none" autoCorrect="off" spellCheck={false} className="h-12 min-w-0 flex-1 rounded-xl border border-[#C8DAF2] bg-white px-4 font-mono text-sm outline-none focus:border-[#1769F5]" placeholder="Binance Order ID" /><button disabled={working || !value.trim()} onClick={onSubmit} className="blue-button pressable h-12 rounded-xl px-5 text-xs font-bold disabled:opacity-40">{working ? <LoaderCircle size={16} className="animate-spin" /> : "Verify order"}</button></div></div>; }
function Header({ eyebrow, title, text, onBack, onRefresh }: { eyebrow: string; title: string; text: string; onBack: () => void; onRefresh?: () => void }) { return <div className="flex items-start justify-between gap-3"><div className="flex min-w-0 gap-3"><button onClick={onBack} className="pressable grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-[#E1E8F3] bg-white text-[#1769F5]" aria-label="Back"><ArrowLeft size={18} /></button><div><p className="text-[10px] font-bold tracking-[.16em] text-[#1769F5]">{eyebrow}</p><h1 className="mt-1 text-[23px] font-bold tracking-[-.04em] text-[#202633]">{title}</h1><p className="mt-1 max-w-xl text-xs leading-5 text-[#7B8799]">{text}</p></div></div>{onRefresh ? <button onClick={onRefresh} className="pressable grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-[#E1E8F3] bg-white text-[#1769F5]" aria-label="Refresh"><RefreshCw size={16} /></button> : null}</div>; }
function Loading() { return <div className="grid min-h-[380px] place-items-center"><div className="text-center"><LoaderCircle className="mx-auto animate-spin text-[#1769F5]" /><p className="mt-3 text-xs font-medium text-[#7B8799]">Loading live deposit methods</p></div></div>; }
