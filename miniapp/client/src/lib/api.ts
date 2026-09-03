/**
 * TgFox production rule: every Mini App value comes from the verified TgFox
 * server; an unsigned browser session must never receive synthetic data.
 */

import { getVerifiedInitData } from "@/lib/tg";

export type Country = {
  code: string;
  name: string;
  flag: string;
  idc: string;
  price: number;
  final_price: number;
  discounted: boolean;
  stock: number;
  is_full: boolean;
  disabled: boolean;
};

export type Profile = {
  user: { id: number; first_name: string; username: string; language_code: string };
  balance: number;
  buy_spent: number;
  sell_earned: number;
  rank: string;
  next_reward: { rank: string | null; goal: number; progress: number; remaining: number };
  counts: { purchases: number; sales: number };
  prefs: { theme: string; owned_themes: string[] };
  bot_username: string;
};

export type Order = {
  id: string;
  amount: number;
  status: string;
  account_id: string;
  at: string;
  completed_at?: string;
};

export type RecordResponse = {
  purchases: Order[];
  sales: Array<{ id: string; country: string; code: string; phone: string; price: number; status: string; at: string }>;
  totals: { purchases_count: number; sales_count: number };
};

export type BuyResponse = {
  order_id: string;
  phone: string;
  country: string;
  country_code: string;
  price_paid: number;
  new_balance: number;
  otp_status: string;
  order_status?: string;
  delivery_status?: string;
  accounting_status?: string;
};

export type WalletOverview = {
  balance: number;
  reserved: number;
  net_spendable: number;
  currency: string;
  addresses: Record<string, string>;
  deposit_methods: Array<{ id: string; name: string; networks: string[]; min_amount: number }>;
  deposit_minimum: number;
  withdrawal: { networks: string[]; minimum: number; maximum: number; fee_percent: number; fee_fixed: number };
};

export type Deposit = {
  deposit_id: string;
  status: string;
  amount: number;
  method: string;
  currency: string;
  network: string;
  address: string;
  payment_url: string;
  qr_url: string;
  memo: string;
  phone: string;
  payment_reference: string;
  instructions: string;
  expires_at: string;
  created_at: string;
  confirmed_at: string;
  transaction_hash_required: boolean;
  transaction_hash_state: string;
  transaction_hash_code: string;
  transaction_confirmations: number | null;
};

export type DepositHashVerification = {
  outcome: "completed" | "pending" | "rejected";
  deposit: Deposit;
  verification: { state: string; code: string; confirmations: number | null; received_amount: number | null };
};

export type DepositOrderVerification = {
  outcome: "completed";
  deposit: Deposit;
};

export type LedgerItem = { id: string; type: string; amount: number; status: string; note: string; reference_id: string; created_at: string };
export type Referral = { code: string; count: number; earnings: number; link: string };
export type Support = { support_group: string; support_channel: string; updates_channel: string; support_link: string };
export type SellRequest = { id: string; country: string; code: string; phone: string; offer_price: number; final_price: number | null; pending_amount: number; status: string; lifecycle_status: string; admin_note: string; submitted_at: string; reviewed_at: string; payment_release_at: string };
export type Withdrawal = { withdrawal_id: string; status: string; status_label: string; amount: number; fee_amount: number; net_amount: number; network: string; wallet_address: string; created_at: string; completed_at: string; reason: string };

const API_BASE = (import.meta.env.VITE_TGFOX_API_BASE || "/webapp/api").replace(/\/$/, "");
const API_KEY_STORAGE = "tgfox.miniapp.api-key";
const API_KEY_ACTIVITY_STORAGE = "tgfox.miniapp.api-key-last-activity";
const API_KEY_IDLE_TIMEOUT_MS = 15 * 60 * 1000;

export class TgFoxApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    readonly requestId?: string,
  ) {
    super(message);
    this.name = "TgFoxApiError";
  }
}

export function hasStoredApiKey() {
  return Boolean(getActiveApiKey());
}

export function clearStoredApiKey() {
  window.sessionStorage.removeItem(API_KEY_STORAGE);
  window.sessionStorage.removeItem(API_KEY_ACTIVITY_STORAGE);
}

export function storeApiKey(apiKey: string) {
  const normalized = apiKey.trim();
  if (!normalized.startsWith("tg_")) {
    throw new TgFoxApiError("Enter a valid TgFox API key beginning with tg_.", 401);
  }
  window.sessionStorage.setItem(API_KEY_STORAGE, normalized);
  window.sessionStorage.setItem(API_KEY_ACTIVITY_STORAGE, String(Date.now()));
}

function getActiveApiKey() {
  const apiKey = window.sessionStorage.getItem(API_KEY_STORAGE)?.trim() || "";
  const lastActivity = Number(window.sessionStorage.getItem(API_KEY_ACTIVITY_STORAGE) || "0");
  if (!apiKey || !lastActivity || Date.now() - lastActivity > API_KEY_IDLE_TIMEOUT_MS) {
    clearStoredApiKey();
    return "";
  }
  window.sessionStorage.setItem(API_KEY_ACTIVITY_STORAGE, String(Date.now()));
  return apiKey;
}

function errorMessage(body: unknown) {
  if (typeof body === "object" && body !== null) {
    const payload = body as { detail?: unknown; message?: unknown; error?: { message?: unknown } };
    if (typeof payload.detail === "string") return payload.detail;
    if (typeof payload.message === "string") return payload.message;
    if (typeof payload.error?.message === "string") return payload.error.message;
  }
  return "Request could not be completed.";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const apiKey = getActiveApiKey();
  const initData = apiKey ? "" : await getVerifiedInitData();
  const requestId = crypto.randomUUID();
  const headers = new Headers(init?.headers);
  if (!apiKey && !initData) {
    throw new TgFoxApiError("Sign in with your TgFox API key to open your account.", 401, requestId);
  }
  headers.set("Accept", "application/json");
  if (typeof init?.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("X-Request-Id", requestId);
  if (apiKey) headers.set("X-Api-Key", apiKey);
  else headers.set("X-Telegram-Init-Data", initData);
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const fallback = `Request failed with HTTP ${response.status}.`;
    throw new TgFoxApiError(
      errorMessage(body) === "Request could not be completed." ? fallback : errorMessage(body),
      response.status,
      response.headers.get("X-Request-Id") || requestId,
    );
  }
  return body as T;
}

export const tgfoxApi = {
  async login(apiKey: string) {
    storeApiKey(apiKey);
    try {
      return await request<Profile>("/me");
    } catch (error) {
      clearStoredApiKey();
      throw error;
    }
  },
  async profile() {
    return request<Profile>("/me");
  },
  async countries() {
    return request<{ countries: Country[] }>("/countries");
  },
  async records() {
    return request<RecordResponse>("/record");
  },
  async buy(countryCode: string) {
    return request<BuyResponse>("/buy", { method: "POST", body: JSON.stringify({ country_code: countryCode }) });
  },
  async orderOtp(orderId: string) {
    return request<{ order_id: string; otp?: string; password?: string | null; otp_status: string; order_status?: string; delivery_status?: string; accounting_status?: string }>(`/order/${orderId}/otp`);
  },
  async terminateOrder(orderId: string) {
    return request<{ order_id: string; ok: boolean; terminated: boolean; already_terminated?: boolean }>(
      `/order/${encodeURIComponent(orderId)}/terminate`,
      { method: "POST" },
    );
  },
  async wallet() {
    return request<WalletOverview>("/wallet");
  },
  async createDeposit(input: { method: string; amount: number; network?: string }) {
    return request<{ deposit: Deposit }>("/deposit", { method: "POST", body: JSON.stringify(input) });
  },
  async deposits(page = 1) {
    return request<{ page: number; limit: number; total: number; items: Deposit[] }>(`/deposits?page=${page}`);
  },
  async deposit(depositId: string) {
    return request<{ deposit: Deposit }>(`/deposit/${encodeURIComponent(depositId)}`);
  },
  async submitDepositTransactionHash(depositId: string, transactionHash: string) {
    return request<DepositHashVerification>(`/deposit/${encodeURIComponent(depositId)}/transaction-hash`, {
      method: "POST",
      body: JSON.stringify({ transaction_hash: transactionHash }),
    });
  },
  async submitBinanceOrderId(depositId: string, orderId: string) {
    return request<DepositOrderVerification>(`/deposit/${encodeURIComponent(depositId)}/binance-order`, {
      method: "POST",
      body: JSON.stringify({ order_id: orderId }),
    });
  },
  async transactions(page = 1, type?: string) {
    const params = new URLSearchParams({ page: String(page) });
    if (type) params.set("txn_type", type);
    return request<{ page: number; limit: number; total: number; items: LedgerItem[] }>(`/transactions?${params.toString()}`);
  },
  async referral() {
    return request<Referral>("/referral");
  },
  async support() {
    return request<Support>("/support");
  },
  async sellRequests() {
    return request<{ items: SellRequest[] }>("/sell-requests");
  },
  async saveWithdrawalAddress(input: { network: string; address: string }) {
    return request<{ network: string; address: string }>("/withdraw/address", { method: "POST", body: JSON.stringify(input) });
  },
  async withdraw(input: { network: string; amount: number }) {
    return request<{ withdrawal_id: string; status: string; amount: number; fee: number; net_amount: number; network: string; wallet_address: string }>("/withdraw", { method: "POST", body: JSON.stringify(input) });
  },
  async withdrawals(page = 1) {
    return request<{ page: number; limit: number; total: number; items: Withdrawal[] }>(`/withdrawals?page=${page}`);
  },
};
