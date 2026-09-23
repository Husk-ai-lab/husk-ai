// Studio-side brand mark — visually identical to the marketing site's BrandName
// + logo combo. Kept here so the studio doesn't depend on marketing source.

import logoMark from "@/assets/husk-logo.png";

interface BrandMarkProps {
  className?: string;
}

export function BrandMark({ className = "" }: BrandMarkProps) {
  return (
    <div className={`flex items-center gap-2.5 ${className}`}>
      <img src={logoMark} alt="" className="size-8 object-contain" />
      <div className="text-sm font-bold tracking-[-0.02em]">
        <span className="text-foreground">HUSK</span>{" "}
        <span className="text-accent">AI</span>
      </div>
    </div>
  );
}
