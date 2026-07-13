export type ChannelProvider = "grok_web" | "grok_build" | "grok_console";

export const CHANNEL_SUFFIX: Record<ChannelProvider, "-web" | "-build" | "-console"> = {
  grok_web: "-web",
  grok_build: "-build",
  grok_console: "-console",
};

export function channelSuffixForProvider(provider: string): string {
  if (provider === "grok_web") return "-web";
  if (provider === "grok_build") return "-build";
  if (provider === "grok_console") return "-console";
  return "";
}

export function validatePublicSuffix(publicId: string, provider: string): boolean {
  const suffix = channelSuffixForProvider(provider);
  if (!publicId || !suffix) return false;
  if (!publicId.endsWith(suffix)) return false;
  // Longest-suffix wins: -console before -build before -web (no overlap in practice).
  if (publicId.endsWith("-console")) return provider === "grok_console";
  if (publicId.endsWith("-build")) return provider === "grok_build";
  if (publicId.endsWith("-web")) return provider === "grok_web";
  return false;
}

export function ensurePublicSuffix(publicId: string, provider: string): string {
  const suffix = channelSuffixForProvider(provider);
  if (!publicId || !suffix) return publicId;
  if (publicId.endsWith(suffix)) return publicId;
  return `${publicId}${suffix}`;
}

export function channelFromPublicId(publicId: string): ChannelProvider | null {
  if (publicId.endsWith("-console")) return "grok_console";
  if (publicId.endsWith("-build")) return "grok_build";
  if (publicId.endsWith("-web")) return "grok_web";
  return null;
}

export function channelBadgeClass(provider: string): string {
  switch (provider) {
    case "grok_web":
      return "border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300";
    case "grok_build":
      return "border-violet-500/40 bg-violet-500/10 text-violet-700 dark:text-violet-300";
    case "grok_console":
      return "border-amber-500/40 bg-amber-500/10 text-amber-800 dark:text-amber-300";
    default:
      return "";
  }
}

export function channelLabelKey(provider: string): string {
  if (provider === "grok_web") return "models.providerGrokWeb";
  if (provider === "grok_console") return "models.providerGrokConsole";
  return "models.providerGrokBuild";
}

export async function copyText(value: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    try {
      const area = document.createElement("textarea");
      area.value = value;
      area.style.position = "fixed";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(area);
      return ok;
    } catch {
      return false;
    }
  }
}
