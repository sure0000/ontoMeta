export function resolveDataHubDatasetUrl(
  sourceRef?: string,
  datahubUrl?: string,
  datahubBase?: string,
): string | undefined {
  if (datahubUrl) return datahubUrl;
  if (!sourceRef) return undefined;

  const base = (datahubBase || "http://localhost:9002").replace(/\/$/, "");
  const urn = sourceRef.startsWith("urn:")
    ? sourceRef
    : `urn:li:dataset:(urn:li:dataPlatform:hive,${sourceRef},PROD)`;
  return `${base}/dataset/${encodeURIComponent(urn)}`;
}

export function extractDataHubBase(domainDatahubUrl?: string): string | undefined {
  if (!domainDatahubUrl) return undefined;
  const idx = domainDatahubUrl.indexOf("/domain/");
  return idx > 0 ? domainDatahubUrl.slice(0, idx) : domainDatahubUrl;
}
