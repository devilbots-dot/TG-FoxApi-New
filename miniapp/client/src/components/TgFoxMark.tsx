/** TgFox Blue Console: render the user-supplied TG FOX API wolf logo exactly as provided. */

const LOGO_URL = "/app/branding/tgfox-api-logo.jpg";

export function TgFoxMark({ size = 38, label = true, className = "" }: { size?: number; label?: boolean; className?: string }) {
  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <img src={LOGO_URL} alt="TG FOX API logo" className="shrink-0 rounded-full border border-[#DDE7F7] bg-white object-cover" style={{ width: size, height: size }} />
      {label ? <div className="leading-none"><p className="text-[12px] font-extrabold tracking-[.04em] text-[#1769F5]">TG FOX API</p><p className="mt-1 text-[8px] font-bold tracking-[.1em] text-[#808A9C]">ACCOUNT CONTROL PANEL</p></div> : null}
    </div>
  );
}
