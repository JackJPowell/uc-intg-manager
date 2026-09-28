export function inplaceUpgradeReason(firmwareVersion: string | null, available: boolean | null): string {
  if (available === null) return 'Remote firmware support has not been checked yet. In-place upgrades are unavailable until the check succeeds.'
  const version = firmwareVersion ? ` (currently ${firmwareVersion})` : ''
  return `Due to the firmware on this remote ${version} integrations cannot be upgraded in place. Upgrade the remote's firmware to 2.9.3 or newer (enable beta updates) or downgrade to Integration Manager `
}
