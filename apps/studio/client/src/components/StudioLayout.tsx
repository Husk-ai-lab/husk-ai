// Studio header + footer chrome: sticky nav with backdrop-blur, brand mark left,
// links center. Local-first single-user — no account menu, no project switcher.

import { BrandMark } from "@/components/BrandMark";
import { Button } from "@/components/ui/button";
import { useTheme } from "@/lib/theme";
import { ExternalLink, Moon, Sun } from "lucide-react";
import { type ReactNode } from "react";
import { Link, useLocation } from "wouter";

interface StudioLayoutProps {
  children: ReactNode;
}

const NAV = [
  { href: "/dashboard", label: "Overview" },
  { href: "/runs", label: "Runs" },
  { href: "/onboarding", label: "Connect" },
  { href: "/settings", label: "Settings" },
];

export function StudioLayout({ children }: StudioLayoutProps) {
  const [location] = useLocation();
  const [theme, toggleTheme] = useTheme();

  return (
    <div className="min-h-screen bg-background text-foreground flex flex-col">
      <nav className="border-b border-border/30 px-4 py-4 sm:px-6 sm:py-5 md:px-12 md:py-6 flex items-center justify-between sticky top-0 bg-background/80 backdrop-blur-sm z-40 gap-3">
        <Link href="/dashboard" className="hover:opacity-80 transition-opacity">
          <BrandMark />
        </Link>

        <div className="hidden md:flex gap-8 text-sm">
          {NAV.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              className={
                location === l.href ||
                (l.href !== "/dashboard" && location.startsWith(l.href))
                  ? "text-accent transition-colors"
                  : "text-muted-foreground hover:text-foreground transition-colors"
              }
            >
              {l.label}
            </Link>
          ))}
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={toggleTheme}
            aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
            title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
          >
            {theme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </Button>
          <span
            className="inline-flex items-center gap-1.5 rounded-full border border-border/60 px-2.5 py-1 text-[10px] uppercase tracking-[0.14em] text-muted-foreground"
            title="Husk Studio debugs your agent during development. It runs only on this machine — never in production."
          >
            <span className="inline-block size-1.5 rounded-full bg-accent" />
            <span className="hidden sm:inline">Development · local only</span>
            <span className="sm:hidden">Dev</span>
          </span>
        </div>
      </nav>

      <main className="flex-1">{children}</main>

      <footer className="border-t border-border/30 px-6 md:px-12 py-8">
        <div className="max-w-6xl mx-auto flex flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground">
          <div className="flex items-center gap-4">
            <BrandMark className="opacity-70" />
            <span>
              Local-first development debugger. Your data never leaves this machine —
              Husk Studio runs while you build, never in production.
            </span>
          </div>
          <a
            href="https://github.com/husk-ai-lab/husk-ai"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 hover:text-foreground transition-colors"
          >
            GitHub <ExternalLink className="size-3" />
          </a>
        </div>
      </footer>
    </div>
  );
}
