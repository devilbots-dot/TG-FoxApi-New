/**
 * TgFox production bridge: Telegram-native behavior must preserve the verified
 * user session and open payment/support links without synthetic data.
 */

export type TelegramWebApp = {
  initData: string;
  initDataUnsafe?: { user?: { id?: number; first_name?: string; username?: string } };
  colorScheme?: "light" | "dark";
  platform?: string;
  ready?: () => void;
  expand?: () => void;
  close?: () => void;
  openLink?: (url: string) => void;
  HapticFeedback?: {
    impactOccurred: (style: "light" | "medium" | "heavy" | "rigid" | "soft") => void;
    notificationOccurred: (type: "error" | "success" | "warning") => void;
  };
};

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp };
  }
}

export function getTelegram() {
  return window.Telegram?.WebApp;
}

export function prepareTelegram() {
  const app = getTelegram();
  app?.ready?.();
  app?.expand?.();
  return app;
}

/**
 * Telegram injects WebApp data into the webview bridge. On some Android
 * clients the React entry module can run a moment before `initData` becomes
 * readable, so wait briefly instead of issuing an unsigned first request.
 */
export async function getVerifiedInitData(timeoutMs = 2_500): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() <= deadline) {
    const app = prepareTelegram();
    const initData = app?.initData?.trim();
    if (initData) return initData;
    await new Promise((resolve) => window.setTimeout(resolve, 80));
  }
  return "";
}

export function isTelegramMiniApp() {
  return Boolean(getTelegram()?.initData);
}

export function haptic(type: "light" | "medium" | "heavy" = "light") {
  getTelegram()?.HapticFeedback?.impactOccurred(type);
}

export function hapticNotice(type: "success" | "warning" | "error") {
  getTelegram()?.HapticFeedback?.notificationOccurred(type);
}

export function openTelegramLink(url: string) {
  if (!url) return false;
  const app = getTelegram();
  if (app?.openLink) {
    app.openLink(url);
    return true;
  }
  window.open(url, "_blank", "noopener,noreferrer");
  return true;
}
