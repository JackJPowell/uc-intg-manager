export function inplaceUpgradeReason(firmwareVersion: string | null, available: boolean | null): string {
  if (available === null) return 'Remote firmware support has not been checked yet. In-place upgrades are unavailable until the check succeeds.'
  const version = firmwareVersion ? ` (currently ${firmwareVersion})` : ''
  return `This Remote cannot upgrade integrations in place${version}. Upgrade its firmware to 2.9.3 or newer (enable beta updates), or install Integration Manager v2.0.6.`
}
