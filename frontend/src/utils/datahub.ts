/** 本体里没有 DataHub 数据集与之对应的 source_ref 形态。 */
const NON_DATASET_PREFIXES = ["manual:", "derived:"];

export function resolveDataHubDatasetUrl(
  sourceRef?: string,
  datahubUrl?: string,
  datahubBase?: string,
): string | undefined {
  if (datahubUrl) return datahubUrl;
  if (!sourceRef) return undefined;
  // 人工建模与派生对象在 DataHub 里没有对应数据集：下面的兜底会把 `manual:mysql:foo`
  // 当表名拼成一个 hive URN，链接点开必然是空页面。宁可不给链接。
  if (NON_DATASET_PREFIXES.some((prefix) => sourceRef.startsWith(prefix))) return undefined;

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
